"""周期自動決定・並列 R/W・短周期のループ試験 (モック)。"""
import multiprocessing as mp
import time

from twin.config import load_config, parse_config
from twin.core.loop import run_loop, measure_cycle
from twin.drivers.mock import MockPLC


def _run(raw, seconds=2.0):
    ctx = mp.get_context("spawn")
    q = ctx.Queue(); pc, cc = ctx.Pipe(duplex=False); ev = ctx.Event()
    p = ctx.Process(target=run_loop, args=(raw, q, cc, ev)); p.start()
    snaps, logs = [], []
    t0 = time.time()
    while time.time() - t0 < seconds:
        if pc.poll(0.05):
            m = pc.recv()
            (snaps if m["type"] == "snap" else logs).append(m)
    ev.set(); p.join(3)
    return snaps, logs


def test_measure_cycle_mock():
    cfg = parse_config({"plc": {"type": "mock"}, "tags": {}, "objects": []})
    d = MockPLC(); d.connect()
    m = measure_cycle(d, cfg, 50)
    assert m["cycle_ms"] >= 0.5 and m["p99_ms"] < 5 and m["cycle_ms"] % 0.5 == 0


def test_auto_cycle_and_parallel_rw():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = load_config(os.path.join(root, "configs", "demo_line.yaml"))
    raw = dict(cfg.raw); raw["cycle_ms"] = "auto"; raw["plc"] = dict(raw["plc"]); raw["plc"]["parallel_rw"] = True
    snaps, logs = _run(raw, 2.5)
    msgs = " ".join(l["msg"] for l in logs)
    assert "周期自動決定" in msgs and "並列R/W=on" in msgs
    s = snaps[-1]
    cyc = s["stat"]["cycle_ms"]
    assert 0.5 <= cyc <= 5
    periods = [x["stat"]["period_ms"] for x in snaps[-200:]]
    assert abs(sum(periods) / len(periods) - cyc) < cyc * 0.2
    assert s["out"][0] & 1 == 1          # モックロジックが回っている (CONV1_RUN)
    assert s["stat"]["status"] == "connected"


def test_fixed_2ms_cycle():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = load_config(os.path.join(root, "configs", "demo_line.yaml"))
    raw = dict(cfg.raw); raw["cycle_ms"] = 2
    snaps, logs = _run(raw, 2.0)
    periods = [x["stat"]["period_ms"] for x in snaps[-300:]]
    assert abs(sum(periods) / len(periods) - 2.0) < 0.3


def test_bad_watch_entry_is_disabled_without_disconnect():
    """E0 (デバイス番号異常) を返す監視エントリだけ無効化し、本体は接続を保つ。"""
    import socket
    import threading
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(2)
    port = srv.getsockname()[1]

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(c,), daemon=True).start()

    def handle(c):
        buf = b""
        while True:
            try:
                d = c.recv(4096)
            except OSError:
                return
            if not d:
                return
            buf += d
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                p = line.decode().split()
                if p[0] == "RDS":
                    dev = p[1].split(".")[0]
                    if dev.startswith("T"):
                        c.sendall(b"E0\r\n")
                    else:
                        c.sendall((" ".join("7" for _ in range(int(p[2]))) + "\r\n").encode())
                else:
                    c.sendall(b"OK\r\n")

    threading.Thread(target=serve, daemon=True).start()
    raw = {"plc": {"type": "keyence", "ip": "127.0.0.1", "port": port, "out": {"dev": "DM", "addr": 1000, "words": 8},
                   "in": {"dev": "DM", "addr": 1100, "words": 8}},
           "cycle_ms": 10, "watch": [{"dev": "DM", "addr": 100, "words": 2}, {"dev": "T", "addr": 0, "words": 2},
                                     {"dev": "MR", "addr": 100, "words": 1}],
           "tags": {}, "objects": []}
    snaps, logs = _run(raw, 1.5)
    msgs = [l["msg"] for l in logs]
    assert any("無効化" in m and "T0" in m and "E0" in m for m in msgs), msgs
    assert sum(1 for m in msgs if m.startswith("PLC接続")) == 1          # 再接続していない
    s = snaps[-1]
    assert s["stat"]["status"] == "connected"
    assert s["out"] == [7] * 8 and s["watch"][0] == 7 and s["watch"][4] == 7   # 他の監視は読めている
    srv.close()


def test_watch_bit_write_uses_set_reset_and_verifies():
    """監視ビットのトグルは ST/RS 1 点書込み。ラダーに上書きされる場合は確認ログが出る。"""
    import socket
    import threading
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(4)
    port = srv.getsockname()[1]
    mem = {"MR100": 0, "LR0": 0}
    cmds = []

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(c,), daemon=True).start()

    def handle(c):
        buf = b""
        while True:
            try:
                d = c.recv(4096)
            except OSError:
                return
            if not d:
                return
            buf += d
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                p = line.decode().split()
                cmds.append(line.decode())
                if p[0] == "RDS":
                    dev = p[1].split(".")[0]
                    n = int(p[2])
                    base = mem.get(dev, 0)
                    c.sendall((" ".join(str(base if i == 0 else 0) for i in range(n)) + "\r\n").encode())
                elif p[0] in ("ST", "RS"):
                    dev = p[1]
                    if dev.startswith("LR"):            # LR は保持される (TP フラグ想定)
                        mem["LR0"] = 1 if p[0] == "ST" else 0
                    # MR105 はラダーが上書きする想定: 変えない
                    c.sendall(b"OK\r\n")
                else:
                    c.sendall(b"OK\r\n")

    threading.Thread(target=serve, daemon=True).start()
    raw = {"plc": {"type": "keyence", "ip": "127.0.0.1", "port": port, "out": {"dev": "DM", "addr": 1000, "words": 4},
                   "in": {"dev": "DM", "addr": 1100, "words": 4}},
           "cycle_ms": 10, "watch_every": 1,
           "watch": [{"dev": "MR", "addr": 100, "words": 1}, {"dev": "LR", "addr": 0, "words": 1}],
           "tags": {}, "objects": []}
    ctx = mp.get_context("spawn")
    q = ctx.Queue(); pc, cc = ctx.Pipe(duplex=False); ev = ctx.Event()
    p = ctx.Process(target=run_loop, args=(raw, q, cc, ev)); p.start()
    time.sleep(1.0)
    q.put({"type": "write_watch_bit", "word": 0, "bit": 5, "value": 1})      # MR105 (ラダーが上書き)
    q.put({"type": "write_watch_bit", "word": 1, "bit": 0, "value": 1})      # LR000 (保持される)
    snaps, logs = [], []
    t0 = time.time()
    while time.time() - t0 < 2.5:
        if pc.poll(0.05):
            m = pc.recv()
            (snaps if m["type"] == "snap" else logs).append(m)
    ev.set(); p.join(3); srv.close()
    msgs = [l["msg"] for l in logs]
    assert "ST MR105" in cmds and "ST LR000" in cmds
    assert not any(c.startswith("WRS MR") for c in cmds)                      # ワードごと書いていない
    assert any("MR105" in m and "ON のまま" not in m and "OFF のままです" in m for m in msgs), msgs
    assert not any("LR000" in m and "のままです" in m for m in msgs)
    assert snaps[-1]["watch"][1] & 1 == 1
