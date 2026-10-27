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
