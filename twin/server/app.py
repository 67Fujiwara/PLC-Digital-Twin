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

from ..config import TwinConfig, parse_config, save_watch
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

        maxlen = int(cfg.record_seconds * 1000 / cfg.cycle_ms) + 10
        self.ring: deque[dict] = deque(maxlen=maxlen)
        self.latest: dict | None = None
        self.logs: deque[dict] = deque(maxlen=200)
        self.lock = threading.Lock()
        self.started = time.time()
        self.ws_frames = 0

        self.ctx = mp.get_context("spawn") if os.name == "nt" else mp.get_context()
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
                with self.lock:
                    self.ring.append(c)
                    self.latest = msg
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
        # 接続直後に履歴 (波形用) をまとめて送る
        with self.lock:
            hist = list(self.ring)[-3000:]
        if hist:
            await client.send_text(json.dumps({"type": "history", "samples": hist}, separators=(",", ":")))

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
                "samples": new[-50:],
                "sim": latest["sim"],
                "stat": latest["stat"],
                "forces": latest["forces"],
                "wall": latest["wall"],
                "logs": logs,
                "loop_alive": self.proc.is_alive() if self.proc else False,
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


def _compact(s: dict) -> dict:
    return {"seq": s["seq"], "wall": s["wall"], "out": s["out"], "in": s["in"], "watch": s["watch"],
            "p": s["stat"]["period_ms"], "r": s["stat"]["read_ms"], "w": s["stat"]["write_ms"]}
