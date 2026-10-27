"""挙動ライブラリの単体試験 (10ms 刻みで World.step)。"""
from twin.config import parse_config
from twin.sim import World, IOBus
from twin.sim.kernel import to_signed


def make(objects, extra_tags=None):
    tags = {"out": [{"name": "RUN", "bit": "0.0"}, {"name": "EXT", "bit": "0.1"}, {"name": "UP", "bit": "0.2"},
                    {"name": "START", "bit": "0.3"}, {"name": "TGT", "word": 10, "type": "s32"},
                    {"name": "HZ", "word": 16}],
            "in": [{"name": "END", "bit": "0.0"}, {"name": "HOME", "bit": "0.1"}, {"name": "PE1", "bit": "0.2"},
                   {"name": "BUSY", "bit": "0.4"}, {"name": "INPOS", "bit": "0.5"}, {"name": "POS", "word": 10, "type": "s32"},
                   {"name": "ENC", "word": 20, "type": "u32"}]}
    raw = {"plc": {"type": "mock"}, "tags": tags, "objects": objects}
    cfg = parse_config(raw)
    world = World(cfg)
    out, inp, watch = [0] * 64, [0] * 64, []
    return cfg, world, IOBus(out, inp, watch), out, inp


def run(world, io, seconds, dt=0.01):
    for _ in range(int(round(seconds / dt))):
        world.step(dt, io)


def test_iobus_types():
    io = IOBus([0] * 4, [0] * 4, [])
    from twin.config import Ref
    io.set(Ref("in", "word", 0, 0, "s32"), -2)
    assert io.blocks["in"][:2] == [0xFFFE, 0xFFFF]
    assert io.get(Ref("in", "word", 0, 0, "s32")) == -2
    assert io.get(Ref("in", "word", 0, 0, "u32")) == 0xFFFFFFFE
    io.set(Ref("in", "bit", 2, 5), 1)
    assert io.blocks["in"][2] == 0x20
    io.set(Ref("in", "bit", 2, 5), 0)
    assert io.blocks["in"][2] == 0
    assert to_signed(0x8000, 16) == -32768


def test_conveyor_sensor_timing():
    cfg, world, io, out, inp = make([
        {"name": "c1", "type": "conveyor", "run": "out.RUN", "length": 2000, "speed": 300},
        {"name": "pe", "type": "sensor", "on": "c1", "at": 1500, "width": 10, "output": "in.PE1"},
    ])
    world.spawn("c1", 60, 60)
    out[0] = 1  # RUN
    # 前端 60mm -> 1495mm まで 1435mm / 300mm/s = 4.78s
    run(world, io, 4.70)
    assert io.get(cfg.tag("in", "PE1").ref) == 0
    run(world, io, 0.15)
    assert io.get(cfg.tag("in", "PE1").ref) == 1
    run(world, io, 0.5)   # 後端が通過 (60mm + 10mm = 0.23s)
    assert io.get(cfg.tag("in", "PE1").ref) == 0
    run(world, io, 2.0)
    assert world.works == []           # 末端で消滅


def test_conveyor_stop_when_run_off_and_stopper():
    cfg, world, io, out, inp = make([
        {"name": "c1", "type": "conveyor", "run": "out.RUN", "length": 2000, "speed": 300},
        {"name": "st", "type": "stopper", "on": "c1", "at": 1000, "up": "out.UP"},
    ])
    w = world.spawn("c1", 100, 50)
    run(world, io, 1.0)
    assert w.pos == 100                 # RUN OFF
    out[0] = 0b101                      # RUN + UP
    run(world, io, 5.0)
    assert abs(w.pos - 1000) < 1e-6 and w.blocked
    out[0] = 0b001                      # UP OFF -> 通過
    run(world, io, 0.5)
    assert w.pos > 1100 and not w.blocked


def test_side_by_side_works_do_not_block():
    cfg, world, io, out, inp = make([
        {"name": "c1", "type": "conveyor", "run": "out.RUN", "length": 3000, "width": 300, "speed": 300},
    ])
    a = world.spawn("c1", 500, 50, y=50)
    b = world.spawn("c1", 480, 50, y=200)   # X は重なるが Y は離れている -> 並走できる
    out[0] = 1
    run(world, io, 1.0)
    assert abs(a.pos - 800) < 1e-6 and abs(b.pos - 780) < 1e-6 and not b.blocked


def test_workpieces_do_not_overlap():
    cfg, world, io, out, inp = make([
        {"name": "c1", "type": "conveyor", "run": "out.RUN", "length": 3000, "speed": 300},
        {"name": "st", "type": "stopper", "on": "c1", "at": 1000, "up": "out.UP"},
    ])
    a = world.spawn("c1", 500, 50)
    b = world.spawn("c1", 300, 50)
    out[0] = 0b101
    run(world, io, 4.0)
    assert abs(a.pos - 1000) < 1e-6
    assert abs(b.pos - 950) < 1e-6 and b.blocked


def test_conveyor_inverter_and_encoder():
    cfg, world, io, out, inp = make([
        {"name": "c1", "type": "conveyor", "run": "out.RUN", "length": 5000, "speed_ref": "out.HZ",
         "rated_hz": 60, "rated_speed": 300, "hz_scale": 0.01, "accel": 0.5, "encoder": "in.ENC", "pulses_per_mm": 10},
    ])
    conv = world.by_name["c1"]
    out[0] = 1
    out[16] = 3000   # 30.00Hz -> 150mm/s
    run(world, io, 0.10)
    assert 55 < conv.v < 65              # 加速途中 (300/0.5 = 600mm/s^2 x 0.1s = 60)
    run(world, io, 1.0)
    assert abs(conv.v - 150) < 1e-6
    enc = io.get(cfg.tag("in", "ENC").ref)
    assert enc > 0 and enc == conv.state()["pulses"]


def test_cylinder_sensors_and_push():
    cfg, world, io, out, inp = make([
        {"name": "c1", "type": "conveyor", "run": "out.RUN", "length": 2000, "speed": 300},
        {"name": "cyl", "type": "cylinder", "extend": "out.EXT", "stroke": 100, "time": 0.4,
         "end_sensor": "in.END", "home_sensor": "in.HOME", "push": {"conv": "c1", "at": 1500, "width": 100, "to": None}},
    ])
    end, home = cfg.tag("in", "END").ref, cfg.tag("in", "HOME").ref
    run(world, io, 0.05)
    assert io.get(home) == 1 and io.get(end) == 0
    w = world.spawn("c1", 1520, 50)
    out[0] = 0b010   # EXT (RUN off)
    run(world, io, 0.2)
    assert io.get(home) == 0 and io.get(end) == 0
    run(world, io, 0.25)
    assert io.get(end) == 1
    assert w not in world.works          # 押し出された
    out[0] = 0
    run(world, io, 0.45)
    assert io.get(home) == 1


def test_axis_trapezoid():
    cfg, world, io, out, inp = make([
        {"name": "ax", "type": "axis", "target": "out.TGT", "start": "out.START", "speed": 400, "accel": 0.2,
         "pos": "in.POS", "busy": "in.BUSY", "inpos": "in.INPOS", "unit_scale": 100, "tol": 0.5},
    ])
    ax = world.by_name["ax"]
    pos, busy, inpos = (cfg.tag("in", n).ref for n in ("POS", "BUSY", "INPOS"))
    io.set(cfg.tag("out", "TGT").ref, 500 * 100)
    run(world, io, 0.05)
    assert io.get(inpos) == 1 and io.get(busy) == 0
    out[0] |= 0b1000      # START 立上り
    run(world, io, 0.05)
    assert io.get(busy) == 1 and io.get(inpos) == 0
    # 500mm: 加速 0.2s (40mm) + 定速 + 減速 0.2s (40mm) -> 約 1.45s
    run(world, io, 1.2)
    assert io.get(busy) == 1
    run(world, io, 0.5)
    assert io.get(busy) == 0 and io.get(inpos) == 1
    assert abs(io.get(pos) - 50000) <= 50
    assert abs(ax.pos - 500) < 1e-6
    # 負方向
    out[0] &= ~0b1000
    io.set(cfg.tag("out", "TGT").ref, -100 * 100)
    run(world, io, 0.02)
    out[0] |= 0b1000
    run(world, io, 2.5)
    assert abs(ax.pos + 100) < 1e-6
    assert io.get(pos) == -10000


def test_emitter_and_reset():
    cfg, world, io, out, inp = make([
        {"name": "c1", "type": "conveyor", "run": "out.RUN", "length": 2000, "speed": 300},
        {"name": "em", "type": "emitter", "onto": "c1", "every": 1.0, "length": 50, "initial": 2},
    ])
    out[0] = 1
    run(world, io, 0.01)
    assert len(world.works) == 2
    run(world, io, 2.5)
    assert len(world.works) == 4
    world.reset()
    assert world.works == [] and world.by_name["em"].total == 0


def test_sensor_delay_and_invert():
    cfg, world, io, out, inp = make([
        {"name": "c1", "type": "conveyor", "run": "out.RUN", "length": 2000, "speed": 300},
        {"name": "pe", "type": "sensor", "on": "c1", "at": 100, "width": 10, "output": "in.PE1", "delay": 0.05, "invert": True},
    ])
    ref = cfg.tag("in", "PE1").ref
    run(world, io, 0.01)
    assert io.get(ref) == 1         # 非検出で ON (invert)
    world.spawn("c1", 100, 50)       # 検出域に置く
    run(world, io, 0.03)
    assert io.get(ref) == 1         # 遅れ中
    run(world, io, 0.03)
    assert io.get(ref) == 0


def make2(objects, tags_in_extra):
    tags = {"out": [{"name": "RUN", "bit": "0.0"}, {"name": "TRIG", "bit": "0.1"}, {"name": "ACK", "bit": "0.2"},
                    {"name": "BLOW", "word": 2}],
            "in": [{"name": "DONE", "bit": "0.0"}, {"name": "COUNT", "word": 30}, {"name": "ENC", "word": 20, "type": "u32"},
                   {"name": "CENC", "word": 22, "type": "u32"}] + tags_in_extra}
    cfg = parse_config({"plc": {"type": "mock"}, "tags": tags, "objects": objects, "seed": 3})
    world = World(cfg)
    out, inp = [0] * 64, [0] * 64
    return cfg, world, IOBus(out, inp, []), out, inp


def test_camera_outputs_ng_xy_and_handshake():
    cfg, world, io, out, inp = make2([
        {"name": "c1", "type": "conveyor", "run": "out.RUN", "length": 2000, "width": 300, "speed": 300, "encoder": "in.ENC"},
        {"name": "cam", "type": "camera", "on": "c1", "at": 600, "fov": 300, "delay": 0.05, "slots": 4,
         "done": "in.DONE", "ack": "out.ACK", "count": "in.COUNT", "enc": "in.CENC", "results": "in.w32"},
    ], [])
    # 視野内に OK 1 個, NG 2 個 (y 違い)。前端が 600 を通過した瞬間に撮像
    ok = world.spawn("c1", 580, 50, y=150, ng=False)
    ng1 = world.spawn("c1", 500, 50, y=40, ng=True)
    ng2 = world.spawn("c1", 460, 50, y=250, ng=True)
    out[0] = 1
    run(world, io, 0.10)        # ok 前端 580 -> 610 で通過 -> 撮像 (t≈0.07)
    assert world.by_name["cam"].captures == 1
    run(world, io, 0.06)        # delay 50ms 後に出力
    assert io.get(cfg.tag("in", "DONE").ref) == 1
    assert io.get(cfg.tag("in", "COUNT").ref) == 2
    from twin.config import Ref
    x1 = io.get(Ref("in", "word", 32, 0, "s16")); y1 = io.get(Ref("in", "word", 33))
    x2 = io.get(Ref("in", "word", 34, 0, "s16")); y2 = io.get(Ref("in", "word", 35))
    # 撮像時 (t≈0.07) ng1 中心 = 500+21-25 ≈ 496 -> X ≈ -104, ng2 中心 ≈ 456 -> X ≈ -144
    assert -110 <= x1 <= -98 and y1 == 40
    assert -150 <= x2 <= -138 and y2 == 250
    assert io.get(cfg.tag("in", "CENC").ref) > 0
    # ack するまで done 保持。その間に ng1/ng2 もトリガ線を通過して撮像されるが、結果はキューで待つ
    run(world, io, 0.5)
    assert io.get(cfg.tag("in", "DONE").ref) == 1 and io.get(cfg.tag("in", "COUNT").ref) == 2
    cam = world.by_name["cam"]
    assert cam.captures == 3 and len(cam.pending) == 2
    out[0] |= 0b100                      # ack 立上り -> done OFF -> 次の結果 (報告済みなので NG 0 個) が出る
    run(world, io, 0.02)
    assert io.get(cfg.tag("in", "DONE").ref) == 1 and io.get(cfg.tag("in", "COUNT").ref) == 0
    out[0] &= ~0b100
    run(world, io, 0.01)
    out[0] |= 0b100
    run(world, io, 0.02)
    out[0] &= ~0b100
    run(world, io, 0.01)
    out[0] |= 0b100
    run(world, io, 0.05)
    assert io.get(cfg.tag("in", "DONE").ref) == 0 and len(cam.pending) == 0
    # 同じワークは二度報告されない
    assert ng1.reported and ng2.reported and ok.reported


def test_airblow_ejects_only_matching_nozzle():
    cfg, world, io, out, inp = make2([
        {"name": "c1", "type": "conveyor", "run": "out.RUN", "length": 2000, "width": 300, "speed": 300},
        {"name": "blow", "type": "airblow", "on": "c1", "at": 1000, "nozzles": 4, "blow": "out.BLOW", "reach": 60},
    ], [])
    ng = world.spawn("c1", 1020, 40, y=40, ng=True)     # 中心 1000, Y 40 -> ノズル1
    ok = world.spawn("c1", 1020, 40, y=260, ng=False)   # 同じ X, Y 260 -> ノズル4
    out[2] = 0b0001                                     # ノズル1 だけ
    run(world, io, 0.01)
    assert ng not in world.works and ok in world.works
    assert world.stats == {"ng_total": 0, "ng_ejected": 1, "ok_ejected": 0, "ng_passed": 0, "ok_passed": 0}
    out[2] = 0b1000
    run(world, io, 0.01)
    assert ok not in world.works and world.stats["ok_ejected"] == 1


def test_demo_line_tracking_rejects_ng():
    """デモ設定 + デモロジック (モックPLC) を 40 秒回し、NG が排出され OK が通過することを確認。"""
    import os
    from twin.config import load_config
    from twin.drivers.mock import MockPLC
    cfg = load_config(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "configs", "demo_line.yaml"))
    world = World(cfg)
    out, inp, watch = [0] * 64, [0] * 64, [0] * cfg.watch_words
    io = IOBus(out, inp, watch)
    plc = MockPLC(logic="twin.drivers.demo_logic")
    plc.connect()
    plc._last = None
    for _ in range(4000):
        # PLC スキャン (dt 固定 10ms)
        plc.logic.scan(plc.mem, 0.01)
        out[:] = plc.mem.words("DM", 1000, 64)
        world.step(0.01, io)
        plc.mem.set_words("DM", 1100, inp)
    st = world.stats
    assert st["ng_total"] >= 5
    assert st["ng_ejected"] >= st["ng_total"] - 3          # 流れ途中の分を除きほぼ全数排出
    assert st["ng_passed"] == 0
    assert st["ok_ejected"] <= 2          # 隣接ワークへの巻き込み (ノズル帯 75mm に対しワーク幅 60mm)
    assert st["ok_passed"] >= 5
