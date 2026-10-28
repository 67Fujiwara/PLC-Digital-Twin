"""Twin Core 本体: ループプロセス起動、スナップショット受信、WebSocket配信、記録。"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import multiprocessing as mp
import os
import threading
import time
from collections import deque

from ..config import TwinConfig, parse_config, save_watch, tags_sidecar_path
from ..core.loop import run_loop
from .http import WebServer, WSClient, Request

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")


class TwinApp:
    def __init__(self, cfg: TwinConfig, host: str = "0.0.0.0", port: int = 8080, fps: float = 30.0,
                 config_path: str | None = None):
        self.cfg = cfg
        self.config_path = config_path
        self.host, self.port, self.fps = host, port, fps
        self.server = WebServer(host, port, WEB_DIR)
        self.server.on_ws_connect = self._on_connect
        self.server.on_ws_message = self._on_message
        self.server.route("GET", "/api/config")(self._api_config)
        self.server.route("GET", "/api/record.csv")(self._api_record_csv)
        self.server.route("GET", "/api/stat")(self._api_stat)
        self.server.route("POST", "/api/cmd")(self._api_cmd)
        self.server.route("POST", "/api/import")(self._api_import)
        self.server.route("POST", "/api/scan")(self._api_scan_start)
        self.server.route("GET", "/api/scan")(self._api_scan_get)
        self.scan_status: dict = {"busy": False, "has_prev": False}
        self.scan_rows: list = []
        self.scan_mode: str = ""

        maxlen = int(cfg.record_seconds * 1000 / cfg.cycle_ms) + 10
        self.ring: deque[dict] = deque(maxlen=maxlen)
        self.latest: dict | None = None
        self.logs: deque[dict] = deque(maxlen=200)
        self.lock = threading.Lock()
        self.started = time.time()
        self.ws_frames = 0

        self.ctx = mp.get_context("spawn")   # fork はスレッドと相性が悪いので全 OS で spawn
        self.cmd_q = self.ctx.Queue()
        self.parent_conn, self.child_conn = self.ctx.Pipe(duplex=False)
        self.stop_event = self.ctx.Event()
        self.proc: mp.Process | None = None
        self._reader: threading.Thread | None = None
        self._stopping = False

    # --- ループプロセス --------------------------------------------------
    def start_loop(self) -> None:
        self.proc = self.ctx.Process(target=run_loop, args=(self.cfg.raw, self.cmd_q, self.child_conn, self.stop_event),
                                     name="twin-loop", daemon=True)
        self.proc.start()
        self._reader = threading.Thread(target=self._read_snapshots, name="snap-reader", daemon=True)
        self._reader.start()

    def _read_snapshots(self) -> None:
        conn = self.parent_conn
        while not self._stopping:
            try:
                if not conn.poll(0.5):
                    continue
                msg = conn.recv()
            except (EOFError, OSError):
                break
            if msg.get("type") == "snap":
                c = _compact(msg)
                cyc = msg["stat"].get("cycle_ms")
                with self.lock:
                    if cyc and cyc != self.cfg.cycle_ms:          # 自動決定された周期に合わせて記録長を調整
                        self.cfg.cycle_ms = cyc
                        maxlen = int(self.cfg.record_seconds * 1000 / cyc) + 10
                        self.ring = deque(self.ring, maxlen=maxlen)
                    self.ring.append(c)
                    self.latest = msg
            elif msg.get("type") == "scan_progress":
                with self.lock:
                    self.scan_status = msg["status"]
            elif msg.get("type") == "scan_result":
                with self.lock:
                    self.scan_status = msg["status"]
                    self.scan_rows = msg["rows"]
                    self.scan_mode = msg["mode"]
            elif msg.get("type") == "log":
                entry = {"t": time.time(), "level": msg.get("level", "info"), "msg": msg.get("msg", "")}
                with self.lock:
                    self.logs.append(entry)
                print(f"[{entry['level']}] {entry['msg']}", flush=True)

    def send_cmd(self, cmd: dict) -> None:
        self.cmd_q.put(cmd)

    # --- WebSocket ------------------------------------------------------
    async def _on_connect(self, client: WSClient) -> None:
        print(f"[info] ブラウザ接続 {client.peer}", flush=True)
        hello = {"type": "hello", "config": self.cfg.to_public(), "server_time": time.time()}
        await client.send_text(json.dumps(hello, ensure_ascii=False))
        # 接続直後に履歴 (波形用) をまとめて送る (監視ワードが多いときはサイズで打ち切り: 約 2MB まで)
        per_sample = 6 * (self.cfg.out.words + self.cfg.inp.words + self.cfg.watch_words) + 60
        n_hist = max(100, min(3000, 2_000_000 // per_sample))
        with self.lock:
            hist = list(self.ring)[-n_hist:]
        if hist:
            await client.send_text(json.dumps({"type": "history", "samples": _dedup_watch(hist)}, separators=(",", ":")))

    async def _on_message(self, client: WSClient, text: str) -> None:
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            return
        t = msg.get("type")
        if t in ("force_bit", "force_word", "release_all", "write_dev", "write_watch", "write_watch_bit",
                 "sim_reset", "sim_enable", "emit", "remove_works"):
            self.send_cmd(msg)
        elif t == "ping":
            await client.send_text(json.dumps({"type": "pong", "t": msg.get("t"), "server_time": time.time()}))
        elif t == "set_watch":
            await self._set_watch(client, msg.get("watch") or [], bool(msg.get("save")))

    async def _set_watch(self, client: WSClient, watch: list, save: bool) -> None:
        """監視デバイス一覧を差し替える (検証 -> ループへ通知 -> 全クライアントに hello 再送)。"""
        raw = dict(self.cfg.raw)
        raw["watch"] = watch
        try:
            new_cfg = parse_config(raw)
        except Exception as e:  # noqa: BLE001
            await client.send_text(json.dumps({"type": "error", "msg": f"監視デバイス設定エラー: {e}"}, ensure_ascii=False))
            return
        self.cfg = new_cfg
        self.send_cmd({"type": "set_watch", "watch": watch})
        msg = "監視デバイスを更新: " + (", ".join(f"{w['dev']}{w['addr']}x{w.get('words', 1)}" for w in watch) or "なし")
        if save and self.config_path:
            try:
                sc = save_watch(self.config_path, watch)
                msg += f"  (保存: {sc})"
            except OSError as e:
                msg += f"  (保存失敗: {e})"
        with self.lock:
            self.logs.append({"t": time.time(), "level": "info", "msg": msg})
        print(f"[info] {msg}", flush=True)
        hello = json.dumps({"type": "hello", "config": self.cfg.to_public(), "server_time": time.time()}, ensure_ascii=False)
        await self.server.broadcast(hello)

    async def _broadcast_task(self) -> None:
        interval = 1.0 / self.fps
        last_seq = -1
        last_log = 0
        next_t = time.perf_counter()
        while not self._stopping:
            next_t += interval
            delay = next_t - time.perf_counter()
            if delay < -interval:        # 遅れが大きければ追いつく
                next_t = time.perf_counter()
            await asyncio.sleep(max(0.0, delay))
            if not self.server.clients:
                continue
            with self.lock:
                latest = self.latest
                new = [s for s in self.ring if s["seq"] > last_seq] if latest else []
                logs = [l for l in self.logs if l["t"] > last_log]
            if not latest:
                continue
            if new:
                last_seq = new[-1]["seq"]
            if logs:
                last_log = logs[-1]["t"]
            frame = {
                "type": "frame",
                "samples": _dedup_watch(new[-max(50, int(interval * 1000 / max(0.5, self.cfg.cycle_ms) * 2)):]),
                "sim": latest["sim"],
                "stat": latest["stat"],
                "forces": latest["forces"],
                "wall": latest["wall"],
                "logs": logs,
                "loop_alive": self.proc.is_alive() if self.proc else False,
                "scan": self.scan_status,
                "clients": len(self.server.clients),
            }
            self.ws_frames += 1
            await self.server.broadcast(json.dumps(frame, separators=(",", ":")))

    # --- REST -----------------------------------------------------------
    async def _api_config(self, req: Request):
        return 200, "application/json; charset=utf-8", json.dumps(self.cfg.to_public(), ensure_ascii=False).encode()

    async def _api_stat(self, req: Request):
        with self.lock:
            stat = self.latest["stat"] if self.latest else {}
        return 200, "application/json", json.dumps({"stat": stat, "samples": len(self.ring)}).encode()

    async def _api_cmd(self, req: Request):
        self.send_cmd(req.json())
        return 200, "application/json", b'{"ok":true}'

    async def _api_scan_start(self, req: Request):
        mode = "diff" if req.query.get("mode") == "diff" else "full"
        with self.lock:
            if self.scan_status.get("busy"):
                return 409, "application/json; charset=utf-8", json.dumps({"error": "スキャン実行中"}, ensure_ascii=False).encode()
            self.scan_status = {"busy": True, "mode": mode, "progress": 0.0, "rows": 0, "errors": [], "done": False,
                                "has_prev": self.scan_status.get("has_prev", False)}
        self.send_cmd({"type": "scan", "mode": mode})
        return 200, "application/json", json.dumps({"ok": True, "mode": mode}).encode()

    async def _api_scan_get(self, req: Request):
        with self.lock:
            body = {"status": self.scan_status, "mode": self.scan_mode,
                    "rows": self.scan_rows if req.query.get("rows", "1") != "0" else []}
        return 200, "application/json; charset=utf-8", json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()

    async def _api_import(self, req: Request):
        """PLC のラベル/デバイスコメント CSV を取り込み、<設定>.tags.yaml に保存して設定を読み直す。"""
        from ..importer import import_text, to_yaml, ImportResult
        from ..config import load_config
        import yaml as _yaml
        raw = req.body
        text = None
        for enc in ("utf-8-sig", "cp932", "utf-8", "utf-16"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            return 400, "application/json", json.dumps({"error": "文字コードを判別できません"}).encode()
        include_system = req.query.get("system") == "1"
        replace = req.query.get("replace") == "1"
        ts = tags_sidecar_path(self.config_path) if self.config_path else None
        old: dict = {}
        if ts and os.path.isfile(ts):
            with open(ts, "r", encoding="utf-8") as f:
                old = _yaml.safe_load(f) or {}
        old_names = {t["name"] for b in ("out", "in") for t in ((old.get("tags") or {}).get(b) or [])}
        old_names |= {n for w in (old.get("watch") or []) for n in (w.get("names") or []) if n}
        res = import_text(text, self.cfg, include_system=include_system, replaceable=old_names)
        if res.count == 0:
            return 400, "application/json; charset=utf-8", json.dumps(
                {"error": "CSV からデバイスを読み取れませんでした", "skipped": res.skipped[:20]}, ensure_ascii=False).encode()
        if not self.config_path:
            return 400, "application/json; charset=utf-8", json.dumps({"error": "設定ファイルのパスが無いため保存できません"}, ensure_ascii=False).encode()
        merged = ImportResult()
        if old and not replace:                            # 前回の取込と結合 (同名タグ・同じ範囲は新しい方)
            for b in ("out", "in"):
                names = {t["name"] for t in res.tags[b]}
                merged.tags[b] = [t for t in ((old.get("tags") or {}).get(b) or []) if t["name"] not in names]
            new_keys = {(w["dev"], w["addr"]) for w in res.watch}
            merged.watch = [w for w in (old.get("watch") or []) if (w["dev"], w["addr"]) not in new_keys]
        for b in ("out", "in"):
            merged.tags[b] += res.tags[b]
        merged.watch += res.watch
        with open(ts, "w", encoding="utf-8") as f:
            f.write(to_yaml(merged, req.query.get("name", "upload.csv")))
        try:
            new_cfg = load_config(self.config_path)
        except Exception as e:  # noqa: BLE001
            return 400, "application/json; charset=utf-8", json.dumps({"error": f"取込後の設定エラー: {e}"}, ensure_ascii=False).encode()
        self.cfg = new_cfg
        self.send_cmd({"type": "set_watch", "watch": new_cfg.raw["watch"]})
        msg = (f"CSV 取込: tags.out {len(res.tags['out'])}, tags.in {len(res.tags['in'])}, "
               f"watch {len(res.watch)} エントリ ({sum(w['words'] for w in res.watch)} ワード), 除外 {len(res.skipped)}  保存: {ts}")
        with self.lock:
            self.logs.append({"t": time.time(), "level": "info", "msg": msg})
        print(f"[info] {msg}", flush=True)
        hello = json.dumps({"type": "hello", "config": self.cfg.to_public(), "server_time": time.time()}, ensure_ascii=False)
        await self.server.broadcast(hello)
        return 200, "application/json; charset=utf-8", json.dumps(
            {"ok": True, "out": len(res.tags["out"]), "in": len(res.tags["in"]), "watch": len(res.watch),
             "words": sum(w["words"] for w in res.watch), "skipped": res.skipped[:40], "saved": ts}, ensure_ascii=False).encode()

    async def _api_record_csv(self, req: Request):
        with self.lock:
            rows = list(self.ring)
        buf = io.StringIO()
        w = csv.writer(buf)
        cols = ["seq", "wall", "period_ms"]
        tags = [(b, t) for b in ("out", "in", "watch") for t in self.cfg.tags.get(b, [])]
        cols += [f"{b}.{t.name}" for b, t in tags]
        cols += [f"out.w{i}" for i in range(self.cfg.out.words)] + [f"in.w{i}" for i in range(self.cfg.inp.words)]
        w.writerow(cols)
        from ..sim.kernel import IOBus
        for s in rows:
            bus = IOBus(s["out"], s["in"], s["watch"])
            row = [s["seq"], f"{s['wall']:.3f}", s["p"]]
            row += [bus.get(t.ref) for _, t in tags]
            row += s["out"] + s["in"]
            w.writerow(row)
        name = time.strftime("twin_%Y%m%d_%H%M%S.csv")
        return 200, "text/csv; charset=utf-8", buf.getvalue().encode("utf-8-sig")

    # --- 起動/停止 --------------------------------------------------------
    async def run(self) -> None:
        self.start_loop()
        await self.server.start()
        print(f"[info] http://localhost:{self.port}/  (WebSocket {self.fps:g}fps, 周期 {self.cfg.cycle_ms}ms)", flush=True)
        task = asyncio.create_task(self._broadcast_task())
        try:
            while True:
                await asyncio.sleep(1)
                if self.proc and not self.proc.is_alive():
                    print("[error] ループプロセスが終了しました", flush=True)
                    break
        finally:
            await self.shutdown()
            task.cancel()

    async def shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self.stop_event.set()
        await self.server.stop()
        if self.proc:
            self.proc.join(timeout=2)
            if self.proc.is_alive():
                self.proc.terminate()


def _dedup_watch(samples: list) -> list:
    """連続するサンプルで watch が同じなら 2 個目以降は省く (監視ワードは周期より遅く更新されるため)。
    ブラウザ側は前のサンプルの watch を引き継ぐ。先頭は必ず持つ。"""
    out = []
    prev = None
    for s in samples:
        if prev is not None and s["watch"] == prev:
            c = dict(s)
            del c["watch"]
            out.append(c)
        else:
            out.append(s)
            prev = s["watch"]
    return out


def _compact(s: dict) -> dict:
    return {"seq": s["seq"], "wall": s["wall"], "out": s["out"], "in": s["in"], "watch": s["watch"],
            "p": s["stat"]["period_ms"], "r": s["stat"]["read_ms"], "w": s["stat"]["write_ms"]}
