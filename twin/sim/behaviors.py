"""挙動ライブラリ 第1世代: conveyor / cylinder / sensor / stopper / emitter / axis / blinker。

YAML 例は configs/demo_line.yaml を参照。各クラスの docstring に受け付けるキーを記す。
"""
from __future__ import annotations

import math

from ..config import TwinConfig, Ref
from .kernel import Behavior, IOBus, World, Workpiece


def _f(d: dict, key: str, default: float) -> float:
    v = d.get(key, default)
    return float(v) if v is not None else default


class Conveyor(Behavior):
    """コンベヤ。
    run: out.X (運転ビット)        speed: 定速 mm/s (既定 300)
    speed_ref: out.wN (周波数指令) rated_hz: 60  rated_speed: 300  hz_scale: 0.01 (ワード値→Hz)
    accel/decel: 加減速時間 s (0 なら即時)   length: mm   next: 次コンベヤ名 (無ければ末端で消滅)
    encoder: in.wN:u32 (パルス出力)  pulses_per_mm: 10   reverse: out.X (逆転ビット)
    """
    type = "conveyor"

    def __init__(self, cfg, d):
        super().__init__(cfg, d)
        self.run = self.ref("run")
        self.reverse = self.ref("reverse")
        self.speed_ref = self.ref("speed_ref")
        self.encoder = self.ref("encoder")
        self.length = _f(d, "length", 2000.0)
        self.speed_set = _f(d, "speed", 300.0)
        self.rated_hz = _f(d, "rated_hz", 60.0)
        self.rated_speed = _f(d, "rated_speed", self.speed_set)
        self.hz_scale = _f(d, "hz_scale", 0.01)
        self.accel = _f(d, "accel", 0.0)
        self.decel = _f(d, "decel", self.accel)
        self.next = d.get("next")
        self.ppm = _f(d, "pulses_per_mm", 10.0)
        self.v = 0.0
        self.pulses = 0.0
        self.stoppers: list["Stopper"] = []

    def reset(self):
        self.v = 0.0
        self.pulses = 0.0

    def target_speed(self, io: IOBus) -> float:
        if self.run is not None and not io.bit(self.run):
            return 0.0
        if self.speed_ref is not None:
            hz = io.get(self.speed_ref) * self.hz_scale
            v = hz / self.rated_hz * self.rated_speed if self.rated_hz else 0.0
        else:
            v = self.speed_set
        if self.reverse is not None and io.bit(self.reverse):
            v = -v
        return v

    def step(self, dt, io, world):
        tgt = self.target_speed(io)
        if self.accel > 0 and abs(tgt) > abs(self.v) and tgt != 0:
            rate = self.rated_speed / self.accel
            self.v = min(tgt, self.v + rate * dt) if tgt > 0 else max(tgt, self.v - rate * dt)
        elif self.decel > 0 and tgt != self.v:
            rate = self.rated_speed / self.decel
            self.v = max(tgt, self.v - rate * dt) if self.v > tgt else min(tgt, self.v + rate * dt)
        else:
            self.v = tgt
        if self.v == 0:
            for w in world.works_on(self.name):
                w.blocked = False
            return
        self.pulses += self.v * dt * self.ppm
        if self.encoder is not None:
            io.set(self.encoder, int(self.pulses) & 0xFFFF_FFFF)
        # 前方のワークから順に進める (前詰め)。
        works = sorted(world.works_on(self.name), key=lambda w: -w.pos)
        stops = [s.pos for s in self.stoppers if s.is_up()]
        front_limit = math.inf
        for w in works:
            new = w.pos + self.v * dt
            w.blocked = False
            if self.v > 0:
                for sp in stops:
                    if w.pos <= sp + 1e-6 < new:
                        new, w.blocked = sp, True
                if new > front_limit:
                    new, w.blocked = front_limit, True
                w.pos = new
                front_limit = w.pos - w.length
                if w.pos - w.length >= self.length:        # 後端が末端を越えた
                    if self.next and self.next in world.by_name:
                        w.conveyor, w.pos = self.next, w.pos - self.length
                    else:
                        world.remove(w)
            else:
                w.pos = new
                if w.pos <= 0:
                    world.remove(w)

    def state(self):
        return {"name": self.name, "type": self.type, "length": self.length,
                "v": round(self.v, 1), "running": self.v != 0, "next": self.next,
                "pulses": int(self.pulses)}


class Stopper(Behavior):
    """ストッパ。on: コンベヤ名  at: 位置mm  up: out.X (ON で上昇=停止) / down: out.X (ON で下降)
    既定は up 指定なしなら常時上昇。time: 動作時間 s"""
    type = "stopper"

    def __init__(self, cfg, d):
        super().__init__(cfg, d)
        self.conv = str(d["on"])
        self.pos = _f(d, "at", 0.0)
        self.up_ref = self.ref("up")
        self.down_ref = self.ref("down")
        self.time = _f(d, "time", 0.0)
        self.level = 1.0
        self.cmd_up = True

    def reset(self):
        self.level = 1.0

    def is_up(self) -> bool:
        return self.level >= 0.5

    def step(self, dt, io, world):
        if self.up_ref is not None:
            self.cmd_up = io.bit(self.up_ref)
        elif self.down_ref is not None:
            self.cmd_up = not io.bit(self.down_ref)
        tgt = 1.0 if self.cmd_up else 0.0
        if self.time > 0:
            step = dt / self.time
            self.level = min(tgt, self.level + step) if tgt > self.level else max(tgt, self.level - step)
        else:
            self.level = tgt

    def state(self):
        return {"name": self.name, "type": self.type, "conv": self.conv, "at": self.pos,
                "up": self.is_up(), "level": round(self.level, 2)}


class Sensor(Behavior):
    """光電/近接センサ。on: コンベヤ名  at: 位置mm  width: 検出幅mm (既定 10)
    output: in.X  delay: 応答遅れ s  invert: true で検出時OFF (ダークON/ライトON)
    cylinder: シリンダ名 + min/max: ストローク範囲で検出 (コンベヤの代わり)"""
    type = "sensor"

    def __init__(self, cfg, d):
        super().__init__(cfg, d)
        self.conv = d.get("on")
        self.cyl = d.get("cylinder")
        self.pos = _f(d, "at", 0.0)
        self.width = _f(d, "width", 10.0)
        self.output = self.ref("output")
        self.delay = _f(d, "delay", 0.0)
        self.invert = bool(d.get("invert", False))
        self.raw = False
        self.on = False
        self._pending: list[tuple[float, bool]] = []
        self.t = 0.0

    def reset(self):
        self.raw = self.on = False
        self._pending.clear()

    def detect(self, world: World) -> bool:
        if self.cyl:
            c = world.by_name.get(self.cyl)
            if isinstance(c, Cylinder):
                lo, hi = _f(self.d, "min", 0.0), _f(self.d, "max", 0.0)
                return lo <= c.pos <= hi
            return False
        lo, hi = self.pos - self.width / 2, self.pos + self.width / 2
        for w in world.works_on(self.conv):
            if w.pos >= lo and (w.pos - w.length) <= hi:
                return True
        return False

    def step(self, dt, io, world):
        self.t += dt
        raw = self.detect(world)
        if raw != self.raw:
            self.raw = raw
            if self.delay > 0:
                self._pending.append((self.t + self.delay, raw))
            else:
                self.on = raw
        while self._pending and self._pending[0][0] <= self.t:
            self.on = self._pending.pop(0)[1]
        io.set(self.output, (not self.on) if self.invert else self.on)

    def state(self):
        return {"name": self.name, "type": self.type, "conv": self.conv, "cylinder": self.cyl,
                "at": self.pos, "width": self.width, "on": self.on}


class Cylinder(Behavior):
    """エアシリンダ。extend: out.X  retract: out.X (省略時は extend の反転=単動)
    stroke: mm  time: 全ストローク時間 s  (retract_time: 戻り時間)
    end_sensor: in.X (伸端)  home_sensor: in.X (縮端)  sensor_band: 端から何mmでONか (既定 2)
    push: {conv: コンベヤ名, at: 位置mm, width: 60, to: 行先コンベヤ名 or null(消滅)} 伸端到達でワークを押し出す
    """
    type = "cylinder"

    def __init__(self, cfg, d):
        super().__init__(cfg, d)
        self.extend = self.ref("extend")
        self.retract = self.ref("retract")
        self.stroke = _f(d, "stroke", 50.0)
        self.time = max(_f(d, "time", 0.3), 1e-3)
        self.rtime = max(_f(d, "retract_time", self.time), 1e-3)
        self.end_sensor = self.ref("end_sensor")
        self.home_sensor = self.ref("home_sensor")
        self.band = _f(d, "sensor_band", 2.0)
        self.push = d.get("push")
        self.pos = 0.0
        self.pushed = False

    def reset(self):
        self.pos = 0.0
        self.pushed = False

    def step(self, dt, io, world):
        ext = io.bit(self.extend)
        ret = io.bit(self.retract) if self.retract is not None else not ext
        if ext and not ret:
            self.pos = min(self.stroke, self.pos + self.stroke / self.time * dt)
        elif ret and not ext:
            self.pos = max(0.0, self.pos - self.stroke / self.rtime * dt)
        at_end = self.pos >= self.stroke - self.band
        at_home = self.pos <= self.band
        io.set(self.end_sensor, at_end)
        io.set(self.home_sensor, at_home)
        if self.push and isinstance(self.push, dict):
            if at_end and not self.pushed:
                self.pushed = True
                conv = str(self.push.get("conv", ""))
                at = float(self.push.get("at", 0))
                width = float(self.push.get("width", 60))
                to = self.push.get("to")
                for w in world.works_on(conv):
                    if w.pos >= at - width / 2 and w.pos - w.length <= at + width / 2:
                        if to and to in world.by_name:
                            w.conveyor, w.pos, w.blocked = str(to), float(self.push.get("to_at", w.length)), False
                        else:
                            world.remove(w)
            elif not at_end:
                self.pushed = False

    def state(self):
        return {"name": self.name, "type": self.type, "pos": round(self.pos, 1), "stroke": self.stroke,
                "end": self.pos >= self.stroke - self.band, "home": self.pos <= self.band}


class Emitter(Behavior):
    """ワーク供給。onto: コンベヤ名  at: 位置  every: 周期 s (0 なら手動/トリガのみ)
    trigger: out.X (立上りで1個)  enable: out.X  length: ワーク長  max: 同時存在上限  initial: 起動時に置く個数"""
    type = "emitter"

    def __init__(self, cfg, d):
        super().__init__(cfg, d)
        self.onto = str(d["onto"])
        self.at = _f(d, "at", 0.0)
        self.every = _f(d, "every", 0.0)
        self.trigger = self.ref("trigger")
        self.enable = self.ref("enable")
        self.length = _f(d, "length", 50.0)
        self.max = int(d.get("max", 30))
        self.initial = int(d.get("initial", 0))
        self.timer = 0.0
        self.prev_trig = False
        self.total = 0
        self._did_initial = False

    def reset(self):
        self.timer = 0.0
        self.total = 0
        self._did_initial = False

    def emit(self, world: World) -> Workpiece | None:
        if len(world.works) >= self.max:
            return None
        # 供給位置が塞がっていれば出さない
        for w in world.works_on(self.onto):
            if w.pos - w.length < self.at + self.length and w.pos > self.at:
                return None
        self.total += 1
        return world.spawn(self.onto, self.at + self.length, self.length, label=str(self.total))

    def step(self, dt, io, world):
        if not self._did_initial:
            self._did_initial = True
            for i in range(self.initial):      # 起動時配置は等間隔に直接置く
                self.total += 1
                world.spawn(self.onto, self.at + self.length + i * (self.length + 100), self.length,
                            label=str(self.total))
        if self.enable is not None and not io.bit(self.enable):
            return
        if self.trigger is not None:
            t = io.bit(self.trigger)
            if t and not self.prev_trig:
                self.emit(world)
            self.prev_trig = t
        if self.every > 0:
            self.timer += dt
            if self.timer >= self.every:
                self.timer -= self.every
                self.emit(world)

    def state(self):
        return {"name": self.name, "type": self.type, "conv": self.onto, "at": self.at, "total": self.total}


class Axis(Behavior):
    """サーボ軸 (台形加減速)。target: out.wN:s32  start: out.X (立上りで起動)  home: out.X (原点復帰)
    jog_fwd/jog_rev: out.X   speed: 単位/s  accel: 加速時間 s  decel: 減速時間 s
    pos: in.wN:s32 (現在位置)  busy: in.X  inpos: in.X  tol: インポジション幅 (既定 1)
    unit_scale: 位置ワードの倍率 (例: 1mm=100pls なら 100)  lo/hi: ソフトリミット
    """
    type = "axis"

    def __init__(self, cfg, d):
        super().__init__(cfg, d)
        self.target = self.ref("target")
        self.start = self.ref("start")
        self.home = self.ref("home")
        self.jog_f = self.ref("jog_fwd")
        self.jog_r = self.ref("jog_rev")
        self.speed = _f(d, "speed", 500.0)
        self.accel = max(_f(d, "accel", 0.2), 1e-3)
        self.decel = max(_f(d, "decel", self.accel), 1e-3)
        self.pos_ref = self.ref("pos")
        self.busy_ref = self.ref("busy")
        self.inpos_ref = self.ref("inpos")
        self.tol = _f(d, "tol", 1.0)
        self.scale = _f(d, "unit_scale", 1.0)
        self.lo = _f(d, "lo", -1e12)
        self.hi = _f(d, "hi", 1e12)
        self.pos = _f(d, "initial", 0.0)
        self.v = 0.0
        self.tgt = self.pos
        self.moving = False
        self.prev_start = False
        self.prev_home = False

    def reset(self):
        self.pos = _f(self.d, "initial", 0.0)
        self.v = 0.0
        self.tgt = self.pos
        self.moving = False

    def step(self, dt, io, world):
        st = io.bit(self.start) if self.start is not None else False
        hm = io.bit(self.home) if self.home is not None else False
        if st and not self.prev_start:
            self.tgt = min(self.hi, max(self.lo, io.get(self.target) / self.scale))
            self.moving = True
        if hm and not self.prev_home:
            self.tgt = 0.0
            self.moving = True
        self.prev_start, self.prev_home = st, hm
        jog = (1 if (self.jog_f is not None and io.bit(self.jog_f)) else 0) - \
              (1 if (self.jog_r is not None and io.bit(self.jog_r)) else 0)
        if jog:
            self.moving = False
            self.tgt = self.pos
            vt = jog * self.speed
            a = self.speed / self.accel
            self.v = min(vt, self.v + a * dt) if vt > self.v else max(vt, self.v - a * dt)
            self.pos = min(self.hi, max(self.lo, self.pos + self.v * dt))
        elif self.moving:
            dist = self.tgt - self.pos
            dirn = 1 if dist > 0 else -1
            a_acc = self.speed / self.accel
            a_dec = self.speed / self.decel
            # 減速に必要な距離
            stop_d = self.v * self.v / (2 * a_dec)
            if abs(dist) <= stop_d + 1e-9 and self.v * dirn > 0:
                vt = 0.0
                self.v = max(0.0, abs(self.v) - a_dec * dt) * dirn
            else:
                vt = dirn * self.speed
                self.v = min(vt, self.v + a_acc * dt) if vt > self.v else max(vt, self.v - a_acc * dt)
            new = self.pos + self.v * dt
            if (dirn > 0 and new >= self.tgt) or (dirn < 0 and new <= self.tgt) or abs(dist) < 1e-6:
                new, self.v, self.moving = self.tgt, 0.0, False
            self.pos = new
        else:
            self.v = 0.0
        io.set(self.pos_ref, self.pos * self.scale)
        io.set(self.busy_ref, self.moving)
        io.set(self.inpos_ref, (not self.moving) and abs(self.pos - self.tgt) <= self.tol)

    def state(self):
        return {"name": self.name, "type": self.type, "pos": round(self.pos, 2), "target": round(self.tgt, 2),
                "v": round(self.v, 1), "busy": self.moving, "lo": self.lo if self.lo > -1e11 else None,
                "hi": self.hi if self.hi < 1e11 else None}


class Blinker(Behavior):
    """試験用: 周期的に in ビットを反転 (通信確認用)。output: in.X  period: s"""
    type = "blinker"

    def __init__(self, cfg, d):
        super().__init__(cfg, d)
        self.output = self.ref("output")
        self.period = _f(d, "period", 1.0)
        self.t = 0.0

    def step(self, dt, io, world):
        self.t += dt
        io.set(self.output, int(self.t / (self.period / 2)) % 2 == 0)

    def state(self):
        return {"name": self.name, "type": self.type}


class Counter(Behavior):
    """試験用: in ワードを毎周期インクリメント (数値変化の確認用)。output: in.wN  step: 1"""
    type = "counter"

    def __init__(self, cfg, d):
        super().__init__(cfg, d)
        self.output = self.ref("output")
        self.inc = int(d.get("step", 1))
        self.v = 0

    def reset(self):
        self.v = 0

    def step(self, dt, io, world):
        self.v += self.inc
        io.set(self.output, self.v)

    def state(self):
        return {"name": self.name, "type": self.type, "value": self.v}


REGISTRY: dict[str, type[Behavior]] = {
    c.type: c for c in (Conveyor, Stopper, Sensor, Cylinder, Emitter, Axis, Blinker, Counter)
}


def create_behavior(cfg: TwinConfig, d: dict) -> Behavior:
    t = str(d.get("type", "")).lower()
    if t not in REGISTRY:
        raise ValueError(f"objects: 未対応の type {t!r} (name={d.get('name')}) 対応: {sorted(REGISTRY)}")
    return REGISTRY[t](cfg, d)


def validate_objects(cfg: TwinConfig) -> None:
    """設定読込時に objects の参照・名前を検証し、World を仮生成する。"""
    names = set()
    for d in cfg.objects:
        if "name" not in d:
            raise ValueError(f"objects: name がありません: {d}")
        if d["name"] in names:
            raise ValueError(f"objects: 同名があります: {d['name']}")
        names.add(d["name"])
    world = World(cfg)
    for o in world.objects:
        if isinstance(o, (Stopper,)) and o.conv not in world.by_name:
            raise ValueError(f"objects.{o.name}: on={o.conv!r} のコンベヤがありません")
        if isinstance(o, Sensor) and o.conv and o.conv not in world.by_name:
            raise ValueError(f"objects.{o.name}: on={o.conv!r} のコンベヤがありません")
        if isinstance(o, Emitter) and o.onto not in world.by_name:
            raise ValueError(f"objects.{o.name}: onto={o.onto!r} のコンベヤがありません")
        if isinstance(o, Conveyor) and o.next and o.next not in world.by_name:
            raise ValueError(f"objects.{o.name}: next={o.next!r} のコンベヤがありません")
    # 書込み先が in 以外なら警告相当のエラー (PLC出力に書くのは誤り)
    for o in world.objects:
        for key in ("output", "end_sensor", "home_sensor", "pos", "busy", "inpos", "encoder"):
            r = o.ref(key) if key in o.d else None
            if r is not None and r.block != "in":
                raise ValueError(f"objects.{o.name}.{key}: 書込み先は in.* にしてください ({o.d[key]})")


def link_world(world: World) -> None:
    """ストッパをコンベヤに紐付ける (World 生成後に呼ぶ)。"""
    for o in world.objects:
        if isinstance(o, Conveyor):
            o.stoppers = [s for s in world.objects if isinstance(s, Stopper) and s.conv == o.name]
