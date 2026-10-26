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
