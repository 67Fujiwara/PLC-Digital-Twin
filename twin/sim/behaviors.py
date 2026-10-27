"""挙動ライブラリ 第1世代: conveyor / cylinder / sensor / stopper / emitter / axis / blinker。

YAML 例は configs/demo_line.yaml を参照。各クラスの docstring に受け付けるキーを記す。
"""
from __future__ import annotations

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
    width: 幅 mm (既定 300; ワークの Y 座標はこの幅の中)
    """
    type = "conveyor"

    def __init__(self, cfg, d):
        super().__init__(cfg, d)
        self.run = self.ref("run")
        self.reverse = self.ref("reverse")
        self.speed_ref = self.ref("speed_ref")
        self.encoder = self.ref("encoder")
        self.length = _f(d, "length", 2000.0)
        self.width = _f(d, "width", 300.0)
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
        # 前方のワークから順に進める (前詰め)。追突は Y 方向に重なるワーク同士だけ。
        works = sorted(world.works_on(self.name), key=lambda w: -w.pos)
        stops = [s.pos for s in self.stoppers if s.is_up()]
        ahead: list[Workpiece] = []
        for w in works:
            new = w.pos + self.v * dt
            w.blocked = False
            if self.v > 0:
                for sp in stops:
                    if w.pos <= sp + 1e-6 < new:
                        new, w.blocked = sp, True
                for a in ahead:
                    if abs(a.y - w.y) < (a.width + w.width) / 2:   # Y が重なる
                        lim = a.pos - a.length
                        if new > lim:
                            new, w.blocked = lim, True
                w.pos = new
                if w.pos - w.length >= self.length:        # 後端が末端を越えた (乗り移り/流出: 追突判定から外す)
                    if self.next and self.next in world.by_name:
                        w.conveyor, w.pos = self.next, w.pos - self.length
                    else:
                        world.exit_work(w)
                else:
                    ahead.append(w)
            else:
                w.pos = new
                if w.pos <= 0:
                    world.exit_work(w)

    def state(self):
        return {"name": self.name, "type": self.type, "length": self.length, "width": self.width,
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
                            world.eject_work(w)
            elif not at_end:
                self.pushed = False

    def state(self):
        return {"name": self.name, "type": self.type, "pos": round(self.pos, 1), "stroke": self.stroke,
                "end": self.pos >= self.stroke - self.band, "home": self.pos <= self.band}


class Emitter(Behavior):
    """ワーク供給。onto: コンベヤ名  at: 位置  every: 周期 s (0 なら手動/トリガのみ)
    trigger: out.X (立上りで1個)  enable: out.X  length: ワーク長  max: 同時存在上限  initial: 起動時に置く個数
    width: ワーク幅 (既定 length)  y: 幅方向位置 mm / "random" (コンベヤ幅内にランダム, 既定)
    ng_rate: 不良率 0〜1 (既定 0)  jitter: 供給周期のばらつき s (0〜jitter を加算)"""
    type = "emitter"

    def __init__(self, cfg, d):
        super().__init__(cfg, d)
        self.onto = str(d["onto"])
        self.at = _f(d, "at", 0.0)
        self.every = _f(d, "every", 0.0)
        self.trigger = self.ref("trigger")
        self.enable = self.ref("enable")
        self.length = _f(d, "length", 50.0)
        self.width = _f(d, "width", self.length)
        self.y = d.get("y", "random")
        self.ng_rate = _f(d, "ng_rate", 0.0)
        self.jitter = _f(d, "jitter", 0.0)
        self.max = int(d.get("max", 30))
        self.initial = int(d.get("initial", 0))
        self.timer = 0.0
        self.next_in = 0.0
        self.prev_trig = False
        self.total = 0
        self._did_initial = False

    def reset(self):
        self.timer = 0.0
        self.next_in = 0.0
        self.total = 0
        self._did_initial = False

    def _free(self, world: World, pos: float, y: float) -> bool:
        """供給位置 (X: pos-length〜pos, Y: y±width/2) に重なるワークが無いか。"""
        for w in world.works_on(self.onto):
            if w.pos - w.length < pos and w.pos > pos - self.length and abs(w.y - y) < (w.width + self.width) / 2:
                return False
        return True

    def _pick_y(self, world: World, pos: float) -> float | None:
        conv = world.by_name.get(self.onto)
        cw = float(getattr(conv, "width", 300.0))
        if isinstance(self.y, (int, float)):
            y = float(self.y)
            return y if self._free(world, pos, y) else None
        lo, hi = self.width / 2, max(self.width / 2, cw - self.width / 2)
        for _ in range(8):                      # 空いている Y を数回試す
            y = world.rng.uniform(lo, hi)
            if self._free(world, pos, y):
                return y
        return None

    def _place(self, world: World, pos: float, y: float | None = None) -> Workpiece:
        if y is None:
            conv = world.by_name.get(self.onto)
            cw = float(getattr(conv, "width", 300.0))
            y = float(self.y) if isinstance(self.y, (int, float)) else cw / 2
        ng = world.rng.random() < self.ng_rate
        self.total += 1
        if ng:
            world.stats["ng_total"] += 1
        return world.spawn(self.onto, pos, self.length, label=str(self.total), y=y, width=self.width, ng=ng)

    def emit(self, world: World) -> Workpiece | None:
        if len(world.works) >= self.max:
            return None
        pos = self.at + self.length
        y = self._pick_y(world, pos)
        if y is None:                           # 供給位置が塞がっている
            return None
        return self._place(world, pos, y)

    def step(self, dt, io, world):
        if not self._did_initial:
            self._did_initial = True
            for i in range(self.initial):      # 起動時配置は等間隔に直接置く
                self._place(world, self.at + self.length + i * (self.length + 100))
        if self.enable is not None and not io.bit(self.enable):
            return
        if self.trigger is not None:
            t = io.bit(self.trigger)
            if t and not self.prev_trig:
                self.emit(world)
            self.prev_trig = t
        if self.every > 0:
            self.timer += dt
            if self.timer >= self.every + self.next_in:
                self.timer = 0.0
                self.next_in = world.rng.uniform(0, self.jitter) if self.jitter > 0 else 0.0
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


class Camera(Behavior):
    """画像検査カメラ。コンベヤ上の NG ワークの座標を PLC へ出力する。
    on: コンベヤ名  at: 撮像基準線 (トリガ線) の位置 mm  fov: 視野長 mm (at を中心)
    trigger: out.X (立上りで撮像)。省略時はワーク前端が at を通過するたびに自動撮像
    delay: 処理時間 s (撮像から結果出力まで)
    出力 (in):  done: 結果有効ビット  count: NG 個数 (word)  enc: 撮像時エンコーダ値 (u32)
                results: 先頭ワード (NG ごとに 2 ワード: X, Y)  slots: 最大個数 (既定 4)
                x_unit/y_unit: 座標の単位 mm (既定 1 → 1mm/1digit, 0.1 なら 0.1mm/digit)
    ack: out.X を指定すると、done は ack の立上りまで保持 (ハンドシェイク)。省略時は done_time s (既定 0.1) で自動 OFF
    座標系: X = 撮像瞬間の「ワーク中心 − トリガ線」(下流が正)、Y = コンベヤ片側エッジからワーク中心まで。
    PLC 側は「ワーク位置 = at + X + (エンコーダ差 ÷ pulses_per_mm)」で追従する。
    """
    type = "camera"

    def __init__(self, cfg, d):
        super().__init__(cfg, d)
        self.conv = str(d["on"])
        self.pos = _f(d, "at", 0.0)
        self.fov = _f(d, "fov", 300.0)
        self.trigger = self.ref("trigger")
        self.ack = self.ref("ack")
        self.delay = _f(d, "delay", 0.05)
        self.done_time = _f(d, "done_time", 0.1)
        self.done_ref = self.ref("done")
        self.count_ref = self.ref("count")
        self.enc_ref = self.ref("enc")
        self.results = self.ref("results")
        self.slots = int(d.get("slots", 4))
        self.x_unit = _f(d, "x_unit", 1.0)
        self.y_unit = _f(d, "y_unit", 1.0)
        self.prev_trig = False
        self.prev_ack = False
        self.t = 0.0
        self.pending: list[tuple[float, list, int]] = []   # (出力時刻, [(x,y)], enc)
        self.done_until = -1.0
        self.done = False
        self.last: list = []
        self.captures = 0
        self.flash = 0.0
        self._prev_front: dict[int, float] = {}

    def reset(self):
        self.pending.clear()
        self.done = False
        self.last = []
        self.captures = 0
        self._prev_front.clear()

    def capture(self, world: World) -> None:
        conv = world.by_name.get(self.conv)
        lo, hi = self.pos - self.fov / 2, self.pos + self.fov / 2
        res = []
        for w in world.works_on(self.conv):
            cx = w.pos - w.length / 2
            if lo <= cx <= hi and not w.reported:
                w.reported = True
                if w.ng:
                    res.append((cx - self.pos, w.y))
        enc = int(getattr(conv, "pulses", 0.0)) & 0xFFFF_FFFF
        self.pending.append((self.t + self.delay, res, enc))
        self.captures += 1
        self.flash = 0.15

    def step(self, dt, io, world):
        self.t += dt
        self.flash = max(0.0, self.flash - dt)
        if self.trigger is not None:
            tr = io.bit(self.trigger)
            if tr and not self.prev_trig:
                self.capture(world)
            self.prev_trig = tr
        else:
            # 自動: ワーク前端がトリガ線を通過した瞬間
            fired = False
            cur = {}
            for w in world.works_on(self.conv):
                cur[w.id] = w.pos
                pf = self._prev_front.get(w.id)
                if pf is not None and pf < self.pos <= w.pos and not fired:
                    self.capture(world)
                    fired = True
            self._prev_front = cur
        # 結果出力 (前の結果が受け取られる (done OFF) まで次はキューで待つ)
        if not self.done and self.pending and self.pending[0][0] <= self.t:
            _, res, enc = self.pending.pop(0)
            self.last = res[:self.slots]
            io.set(self.count_ref, len(self.last))
            io.set(self.enc_ref, enc)
            if self.results is not None:
                for i in range(self.slots):
                    x, y = self.last[i] if i < len(self.last) else (0.0, 0.0)
                    io.set(Ref(self.results.block, "word", self.results.word + 2 * i, 0, "s16"), x / self.x_unit)
                    io.set(Ref(self.results.block, "word", self.results.word + 2 * i + 1, 0, "u16"), y / self.y_unit)
            self.done = True
            self.done_until = self.t + self.done_time
        if self.ack is not None:
            a = io.bit(self.ack)
            if self.done and a and not self.prev_ack:
                self.done = False
            self.prev_ack = a
        elif self.done and self.t >= self.done_until:
            self.done = False
        io.set(self.done_ref, self.done)

    def state(self):
        return {"name": self.name, "type": self.type, "conv": self.conv, "at": self.pos, "fov": self.fov,
                "done": self.done, "flash": self.flash > 0, "captures": self.captures, "queued": len(self.pending),
                "last": [[round(x, 1), round(y, 1)] for x, y in self.last]}


class AirBlow(Behavior):
    """多連エアブロー (幅方向に nozzles 連)。
    on: コンベヤ名  at: 位置 mm  nozzles: 連数  blow: out.wN (bit0=ノズル1, bit1=ノズル2…) または [out.B1, out.B2, ...]
    pitch: ノズル間隔 mm (省略時はコンベヤ幅を等分)  y0: ノズル1 の Y 中心 (省略時は pitch/2)
    band: 各ノズルが効く Y 幅 (既定 pitch)  reach: X 方向に効く幅 (既定 60)
    ON のノズルの帯 (Y) × at±reach/2 (X) にかかるワークを排出する。NG なら排出成功、OK なら誤排出として集計。
    """
    type = "airblow"

    def __init__(self, cfg, d):
        super().__init__(cfg, d)
        self.conv = str(d["on"])
        self.pos = _f(d, "at", 0.0)
        self.n = max(1, int(d.get("nozzles", 1)))
        blow = d.get("blow")
        self.blow_word: Ref | None = None
        self.blow_bits: list[Ref | None] = []
        if isinstance(blow, list):
            self.blow_bits = [cfg.resolve(b) for b in blow]
        elif blow is not None:
            r = cfg.resolve(blow)
            if r.kind == "word":
                self.blow_word = r
            else:
                self.blow_bits = [r]
        self.pitch = d.get("pitch")
        self.y0 = d.get("y0")
        self.band = d.get("band")
        self.reach = _f(d, "reach", 60.0)
        self.active = [False] * self.n
        self.fired = 0
        self.puff = [0.0] * self.n

    def reset(self):
        self.fired = 0

    def nozzle_y(self, world: World) -> list[float]:
        conv = world.by_name.get(self.conv)
        cw = float(getattr(conv, "width", 300.0))
        pitch = float(self.pitch) if self.pitch is not None else cw / self.n
        y0 = float(self.y0) if self.y0 is not None else pitch / 2
        return [y0 + i * pitch for i in range(self.n)]

    def step(self, dt, io, world):
        if self.blow_word is not None:
            v = io.get(self.blow_word)
            self.active = [bool(v >> i & 1) for i in range(self.n)]
        else:
            self.active = [bool(io.bit(self.blow_bits[i])) if i < len(self.blow_bits) and self.blow_bits[i] is not None else False
                           for i in range(self.n)]
        ys = self.nozzle_y(world)
        conv = world.by_name.get(self.conv)
        cw = float(getattr(conv, "width", 300.0))
        pitch = float(self.pitch) if self.pitch is not None else cw / self.n
        band = float(self.band) if self.band is not None else pitch
        for i in range(self.n):
            self.puff[i] = max(0.0, self.puff[i] - dt)
            if not self.active[i]:
                continue
            self.puff[i] = 0.1
            ylo, yhi = ys[i] - band / 2, ys[i] + band / 2
            xlo, xhi = self.pos - self.reach / 2, self.pos + self.reach / 2
            for w in list(world.works_on(self.conv)):
                cx = w.pos - w.length / 2
                if xlo <= cx <= xhi and (w.y + w.width / 2) > ylo and (w.y - w.width / 2) < yhi:
                    world.eject_work(w)
                    self.fired += 1

    def state(self):
        return {"name": self.name, "type": self.type, "conv": self.conv, "at": self.pos, "nozzles": self.n,
                "active": self.active, "puff": [p > 0 for p in self.puff], "fired": self.fired,
                "reach": self.reach}


REGISTRY: dict[str, type[Behavior]] = {
    c.type: c for c in (Conveyor, Stopper, Sensor, Cylinder, Emitter, Axis, Blinker, Counter, Camera, AirBlow)
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
        if isinstance(o, (Stopper, Camera, AirBlow)) and o.conv not in world.by_name:
            raise ValueError(f"objects.{o.name}: on={o.conv!r} のコンベヤがありません")
        if isinstance(o, Sensor) and o.conv and o.conv not in world.by_name:
            raise ValueError(f"objects.{o.name}: on={o.conv!r} のコンベヤがありません")
        if isinstance(o, Emitter) and o.onto not in world.by_name:
            raise ValueError(f"objects.{o.name}: onto={o.onto!r} のコンベヤがありません")
        if isinstance(o, Conveyor) and o.next and o.next not in world.by_name:
            raise ValueError(f"objects.{o.name}: next={o.next!r} のコンベヤがありません")
    # 書込み先が in 以外なら警告相当のエラー (PLC出力に書くのは誤り)
    for o in world.objects:
        for key in ("output", "end_sensor", "home_sensor", "pos", "busy", "inpos", "encoder",
                    "done", "count", "enc", "results"):
            r = o.ref(key) if key in o.d else None
            if r is not None and r.block != "in":
                raise ValueError(f"objects.{o.name}.{key}: 書込み先は in.* にしてください ({o.d[key]})")


def link_world(world: World) -> None:
    """ストッパをコンベヤに紐付ける (World 生成後に呼ぶ)。"""
    for o in world.objects:
        if isinstance(o, Conveyor):
            o.stoppers = [s for s in world.objects if isinstance(s, Stopper) and s.conv == o.name]
