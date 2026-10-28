"""10ms ループ (専用プロセス)。

1周期:
  out   = plc.read_words(out block)         # PLC -> PC
  (watch_every 周期に1回) watch を 1 エントリ読む (ラウンドロビン)
  コマンド処理 (強制/書込み/リセット)
  sim.step(dt)  -> in ブロック更新
  強制値を in へ上書き
  plc.write_words(in block)                 # PC -> PLC
  スナップショットを親プロセスへ送る (毎周期)

親とのやり取り:
  cmd_q  : multiprocessing.Queue  親 -> ループ  dict コマンド
  snap   : multiprocessing.Pipe   ループ -> 親  毎周期の状態
"""
from __future__ import annotations

import math
import multiprocessing as mp
import threading
import time
import traceback

from ..config import TwinConfig, parse_config, Ref
from ..drivers import make_driver, DriverError
from ..sim import World, IOBus
from ..scanner import Scanner
from .timing import raise_priority, sleep_until, JitterStats


class LoopState:
    def __init__(self, cfg: TwinConfig):
        self.cfg = cfg
        self.out = [0] * cfg.out.words
        self.inp_sim = [0] * cfg.inp.words   # 挙動シミュレーションが書く素の値
        self.inp = [0] * cfg.inp.words       # 強制を重ねて PLC へ送る値
        self.watch = [0] * cfg.watch_words
        self.force_bits: dict[tuple[str, int, int], int] = {}   # (block, word, bit) -> 0/1
        self.force_words: dict[tuple[str, int, str], int] = {}  # (block, word, dtype) -> value
        self.pending_writes: list[tuple[str, int, list[int]]] = []  # (dev, addr, values) 直接書込み
        self.sim_enabled = True
        self.world = World(cfg)
        self.io = IOBus(self.out, self.inp_sim, self.watch)   # sim は inp_sim に書く
        self.io_plc = IOBus(self.out, self.inp, self.watch)   # 強制の適用先
        self.status = "init"
        self.error = ""
        self.seq = 0
        self.watch_idx = 0
        self.scanner: Scanner | None = None
        self.scan_thread: threading.Thread | None = None
        self.snap_conn = None
        self.drv = None

    # --- コマンド -------------------------------------------------------
    def handle(self, cmd: dict) -> None:
        t = cmd.get("type")
        if t == "force_bit":
            key = (str(cmd.get("block", "in")), int(cmd["word"]), int(cmd["bit"]))
            v = cmd.get("value")
            if v is None:
                self.force_bits.pop(key, None)
            else:
                self.force_bits[key] = 1 if v else 0
        elif t == "force_word":
            key = (str(cmd.get("block", "in")), int(cmd["word"]), str(cmd.get("dtype", "u16")))
            v = cmd.get("value")
            if v is None:
                self.force_words.pop(key, None)
            else:
                self.force_words[key] = int(v)
        elif t == "release_all":
            self.force_bits.clear()
            self.force_words.clear()
        elif t == "write_dev":
            # PLC の任意デバイスへ直接書く (次周期で 1 回だけ追加送信)
            dev, addr = str(cmd["dev"]).upper(), int(cmd["addr"])
            vals = cmd.get("values")
            if vals is None:
                vals = [int(cmd["value"])]
            self.pending_writes.append((dev, addr, [int(v) & 0xFFFF for v in vals]))
        elif t == "write_watch":
            # watch ワード index へ書く -> 対応デバイスへ変換
            idx = int(cmd["word"])
            v = int(cmd["value"])
            for w in self.cfg.watch:
                if w.offset <= idx < w.offset + w.words:
                    self.pending_writes.append((w.dev, self._watch_addr(w, idx - w.offset), [v & 0xFFFF]))
                    break
        elif t == "write_watch_bit":
            idx, bit, v = int(cmd["word"]), int(cmd["bit"]), 1 if cmd.get("value") else 0
            for w in self.cfg.watch:
                if w.offset <= idx < w.offset + w.words:
                    cur = self.watch[idx]
                    nv = (cur | (1 << bit)) if v else (cur & ~(1 << bit) & 0xFFFF)
                    self.pending_writes.append((w.dev, self._watch_addr(w, idx - w.offset), [nv]))
                    break
        elif t == "sim_reset":
            self.world.reset()
        elif t == "sim_enable":
            self.sim_enabled = bool(cmd.get("value", True))
        elif t == "emit":
            o = self.world.by_name.get(str(cmd.get("name", "")))
            if o is not None and hasattr(o, "emit"):
                o.emit(self.world)  # type: ignore[attr-defined]
        elif t == "remove_works":
            self.world.works.clear()
        elif t == "scan":
            self.start_scan(str(cmd.get("mode", "full")))
        elif t == "set_watch":
            raw = dict(self.cfg.raw)
            raw["watch"] = list(cmd.get("watch") or [])
            cfg = parse_config(raw)
            self.cfg = cfg
            self.watch = [0] * cfg.watch_words
            self.io = IOBus(self.out, self.inp_sim, self.watch)
            self.io_plc = IOBus(self.out, self.inp, self.watch)

    def start_scan(self, mode: str) -> None:
        """全デバイス読込 / 差分読込を別スレッド + 別接続で実行 (周期ループは止めない)。"""
        if self.scan_thread is not None and self.scan_thread.is_alive():
            self.snap_conn.send({"type": "log", "level": "error", "msg": "スキャン実行中です"})
            return
        if self.scanner is None:
            def make_conn():
                d = self.drv.clone()
                if d is self.drv:
                    raise DriverError("このドライバはスキャン用の 2 本目の接続を作れません")
                d.connect()
                return d
            self.scanner = Scanner(self.cfg, make_conn)
        self.scanner.cfg = self.cfg
        last_sent = [0.0]

        def progress(stt: dict) -> None:
            now = time.perf_counter()
            if now - last_sent[0] > 0.2:
                last_sent[0] = now
                self.snap_conn.send({"type": "scan_progress", "status": stt})

        def work() -> None:
            res = self.scanner.run(mode, progress)
            self.snap_conn.send({"type": "scan_result", "status": self.scanner.status(), "rows": res.rows, "mode": mode})
            self.snap_conn.send({"type": "log", "msg": f"スキャン完了 ({'差分' if mode == 'diff' else '全'}): "
                                 f"{res.read_words} ワード, 該当 {len(res.rows)} 件, {round(res.finished - res.started, 2)} 秒"
                                 + (f", エラー {len(res.errors)} 件" if res.errors else "")})

        self.scan_thread = threading.Thread(target=work, name="scan", daemon=True)
        self.scan_thread.start()

    def _watch_addr(self, w, off: int) -> int:
        # キーエンスのリレーは ch 番号が 100 刻み
        if self.cfg.plc_type == "keyence" and w.bits and w.dev in ("MR", "R", "LR", "CR", "VB"):
            return w.addr + off * 100
        if w.bits and self.cfg.plc_type == "melsec":
            return w.addr + off * 16
        return w.addr + off

    def apply_forces(self) -> None:
        """inp = inp_sim + 強制。解除すればシミュレーション値 (未駆動ビットは 0) に戻る。"""
        self.inp[:] = self.inp_sim
        for (block, word, bit), v in self.force_bits.items():
            arr = self.io_plc.blocks.get(block)
            if arr is None or word >= len(arr):
                continue
            arr[word] = (arr[word] | (1 << bit)) if v else (arr[word] & ~(1 << bit) & 0xFFFF)
        for (block, word, dtype), v in self.force_words.items():
            self.io_plc.set(Ref(block, "word", word, 0, dtype), v)

    def forces_public(self) -> dict:
        return {
            "bits": [{"block": b, "word": w, "bit": bi, "value": v} for (b, w, bi), v in self.force_bits.items()],
            "words": [{"block": b, "word": w, "dtype": d, "value": v} for (b, w, d), v in self.force_words.items()],
        }


def measure_cycle(drv, cfg: TwinConfig, n: int = 100, parallel: bool = False, wdrv=None) -> dict:
    """往復時間を実測して最速周期 (ms) を決める。read+write (並列なら max) の p99 に余裕 0.3ms、0.5ms 刻みに切上げ。"""
    reads, writes, totals = [], [], []
    zeros = [0] * cfg.inp.words
    for _ in range(n):
        t0 = time.perf_counter()
        if parallel and wdrv is not None:
            th = threading.Thread(target=wdrv.write_words, args=(cfg.inp.dev, cfg.inp.addr, zeros))
            th.start()
            drv.read_words(cfg.out.dev, cfg.out.addr, cfg.out.words)
            t1 = time.perf_counter()
            th.join()
            t2 = time.perf_counter()
            reads.append(t1 - t0); writes.append(t2 - t1); totals.append(t2 - t0)
        else:
            drv.read_words(cfg.out.dev, cfg.out.addr, cfg.out.words)
            t1 = time.perf_counter()
            drv.write_words(cfg.inp.dev, cfg.inp.addr, zeros)
            t2 = time.perf_counter()
            reads.append(t1 - t0); writes.append(t2 - t1); totals.append(t2 - t0)
    totals.sort()
    p99 = totals[min(len(totals) - 1, int(len(totals) * 0.99))] * 1000
    avg = sum(totals) / len(totals) * 1000
    cycle = max(0.5, math.ceil((p99 + 0.3) / 0.5) * 0.5)
    return {"cycle_ms": cycle, "avg_ms": round(avg, 3), "p99_ms": round(p99, 3), "max_ms": round(totals[-1] * 1000, 3),
            "read_avg_ms": round(sum(reads) / len(reads) * 1000, 3), "write_avg_ms": round(sum(writes) / len(writes) * 1000, 3)}


def run_loop(raw_cfg: dict, cmd_q: mp.Queue, snap_conn, stop_event) -> None:
    """ループプロセスのエントリ。"""
    cfg = parse_config(raw_cfg)
    st = LoopState(cfg)
    prio_msg = raise_priority()
    period = cfg.cycle_ms / 1000.0
    drv = make_driver(cfg.plc)
    st.drv = drv
    st.snap_conn = snap_conn
    parallel = bool(cfg.plc.get("parallel_rw", False))
    wdrv = drv.clone() if parallel else None          # 書込み専用の 2 本目の接続
    if parallel and wdrv is drv:
        parallel, wdrv = False, None
    jit = JitterStats(200)
    measured: dict | None = None
    snap_conn.send({"type": "log", "msg": f"ループ開始 周期 {'auto' if cfg.cycle_auto else cfg.cycle_ms}ms, {prio_msg}, "
                                          f"driver={drv.name}/{cfg.plc.get('protocol', 'tcp')}, 並列R/W={'on' if parallel else 'off'}"})
    write_thread: threading.Thread | None = None
    write_err: list = []

    def do_write(vals: list[int]) -> None:
        try:
            wdrv.write_words(cfg.inp.dev, cfg.inp.addr, vals)
        except DriverError as e:
            write_err.append(e)

    next_t = time.perf_counter()
    last_t = next_t
    reconnect_at = 0.0
    read_ms = write_ms = step_ms = 0.0
    cycle = 0
    while not stop_event.is_set():
        next_t += period
        sleep_until(next_t, cfg.spin_ms / 1000.0)
        now = time.perf_counter()
        actual_ms = (now - last_t) * 1000.0
        last_t = now
        jit.push(actual_ms, cfg.cycle_ms)
        if now - next_t > period * 5:       # 大幅遅延時は追いつき (バースト防止)
            next_t = now
        cycle += 1

        # --- コマンド ---------------------------------------------------
        while True:
            try:
                cmd = cmd_q.get_nowait()
            except Exception:  # queue.Empty
                break
            try:
                st.handle(cmd)
            except Exception as e:  # noqa: BLE001
                snap_conn.send({"type": "log", "level": "error", "msg": f"コマンド失敗 {cmd}: {e}"})

        # --- 接続 -------------------------------------------------------
        if not drv.connected:
            if now >= reconnect_at:
                try:
                    drv.connect()
                    if wdrv is not None:
                        wdrv.connect()
                    st.status, st.error = "connected", ""
                    snap_conn.send({"type": "log", "msg": f"PLC接続 {cfg.plc_type} {cfg.plc.get('ip', '')}"})
                    if cfg.cycle_auto and measured is None:
                        measured = measure_cycle(drv, cfg, 100, parallel, wdrv)
                        period = measured["cycle_ms"] / 1000.0
                        cfg.cycle_ms = measured["cycle_ms"]
                        st.cfg.cycle_ms = measured["cycle_ms"]
                        next_t = time.perf_counter()
                        snap_conn.send({"type": "log", "msg": (
                            f"周期自動決定: {measured['cycle_ms']}ms  (往復 avg {measured['avg_ms']}ms / p99 {measured['p99_ms']}ms / "
                            f"max {measured['max_ms']}ms, 読み {measured['read_avg_ms']}ms 書き {measured['write_avg_ms']}ms, 100回実測)")})
                except DriverError as e:
                    st.status, st.error = "disconnected", str(e)
                    drv.close()
                    if wdrv is not None:
                        wdrv.close()
                    reconnect_at = now + 1.0
            st.seq += 1
            if cycle % 10 == 0:
                snap_conn.send(_snapshot(st, now, actual_ms, read_ms, write_ms, step_ms, jit, drv))
            continue

        # --- 読み (並列 R/W なら前周期の in ブロック書込みを同時に走らせる) ----------
        t0 = time.perf_counter()
        if parallel:
            write_thread = threading.Thread(target=do_write, args=(list(st.inp),), daemon=True)
            write_thread.start()
        try:
            vals = drv.read_words(cfg.out.dev, cfg.out.addr, cfg.out.words)
            st.out[:] = vals
            if st.cfg.watch and cycle % st.cfg.watch_every == 0:
                # 監視デバイスは 1 周期に 1 エントリずつ順番に読む (多数登録しても周期を崩さない)
                w = st.cfg.watch[st.watch_idx % len(st.cfg.watch)]
                st.watch_idx += 1
                v = drv.read_words(w.dev, w.addr, w.words)
                st.watch[w.offset:w.offset + w.words] = v
        except DriverError as e:
            st.status, st.error = "disconnected", str(e)
            drv.close()
            if wdrv is not None:
                wdrv.close()
            if write_thread is not None:
                write_thread.join(timeout=1.0)
            reconnect_at = time.perf_counter() + 1.0
            snap_conn.send({"type": "log", "level": "error", "msg": f"読出し失敗: {e}"})
            continue
        t1 = time.perf_counter()

        # --- シミュレーション -------------------------------------------
        try:
            if st.sim_enabled:
                st.world.step(period, st.io)
            st.apply_forces()
        except Exception as e:  # noqa: BLE001
            snap_conn.send({"type": "log", "level": "error", "msg": f"sim エラー: {e}\n{traceback.format_exc()}"})
            st.sim_enabled = False
        t2 = time.perf_counter()

        # --- 書き -------------------------------------------------------
        try:
            if parallel:
                write_thread.join()
                if write_err:
                    raise write_err.pop()
            else:
                drv.write_words(cfg.inp.dev, cfg.inp.addr, st.inp)
            while st.pending_writes:
                dev, addr, vals = st.pending_writes.pop(0)
                drv.write_words(dev, addr, vals)
                snap_conn.send({"type": "log", "msg": f"書込み {dev}{addr} <- {vals}"})
        except DriverError as e:
            st.status, st.error = "disconnected", str(e)
            drv.close()
            if wdrv is not None:
                wdrv.close()
            reconnect_at = time.perf_counter() + 1.0
            snap_conn.send({"type": "log", "level": "error", "msg": f"書込み失敗: {e}"})
            continue
        t3 = time.perf_counter()
        read_ms, step_ms, write_ms = (t1 - t0) * 1000, (t2 - t1) * 1000, (t3 - t2) * 1000
        st.status = "connected"
        st.seq += 1
        snap_conn.send(_snapshot(st, now, actual_ms, read_ms, write_ms, step_ms, jit, drv))

    drv.close()
    if wdrv is not None:
        wdrv.close()
    snap_conn.send({"type": "log", "msg": "ループ終了"})


def _snapshot(st: LoopState, now: float, actual_ms: float, read_ms: float, write_ms: float,
              step_ms: float, jit: JitterStats, drv) -> dict:
    return {
        "type": "snap",
        "seq": st.seq,
        "t": now,
        "wall": time.time(),
        "out": list(st.out),
        "in": list(st.inp),
        "watch": list(st.watch),
        "sim": st.world.state(),
        "stat": {
            "status": st.status,
            "error": st.error,
            "period_ms": round(actual_ms, 3),
            "read_ms": round(read_ms, 3),
            "write_ms": round(write_ms, 3),
            "step_ms": round(step_ms, 3),
            "jitter": jit.summary(),
            "sim_enabled": st.sim_enabled,
            "cycle_ms": st.cfg.cycle_ms,
        },
        "forces": st.forces_public(),
    }
