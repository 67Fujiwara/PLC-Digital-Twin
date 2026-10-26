"""シミュレーションカーネル (物理エンジンなし・キネマティック)。

World.step(dt, io):
  - io.get(ref) で PLC出力 (out ブロック / watch) を読む
  - io.set(ref, value) で PLC入力 (in ブロック) を書く
  - ワークは 1次元 (コンベヤ上の位置 mm) で管理する
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Ref, TwinConfig


def _u16(v: int) -> int:
    return int(v) & 0xFFFF


def to_signed(v: int, bits: int) -> int:
    v &= (1 << bits) - 1
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


class IOBus:
    """ブロック配列へのアクセス。out/watch は読み、in は書く (読みも可)。"""

    def __init__(self, out: list[int], inp: list[int], watch: list[int]):
        self.blocks = {"out": out, "in": inp, "watch": watch}

    def get(self, ref: Ref | None, default: int = 0) -> int:
        if ref is None:
            return default
        arr = self.blocks[ref.block]
        if ref.word >= len(arr):
            return default
        if ref.kind == "bit":
            return (arr[ref.word] >> ref.bit) & 1
        if ref.nwords == 2:
            lo = arr[ref.word]
            hi = arr[ref.word + 1] if ref.word + 1 < len(arr) else 0
            v = lo | (hi << 16)
            return to_signed(v, 32) if ref.dtype == "s32" else v
        v = arr[ref.word]
        return to_signed(v, 16) if ref.dtype == "s16" else v

    def bit(self, ref: Ref | None) -> bool:
        return bool(self.get(ref))

    def set(self, ref: Ref | None, value) -> None:
        if ref is None:
            return
        arr = self.blocks[ref.block]
        if ref.word >= len(arr):
            return
        if ref.kind == "bit":
            if value:
                arr[ref.word] |= 1 << ref.bit
            else:
                arr[ref.word] &= ~(1 << ref.bit) & 0xFFFF
            return
        v = int(round(value))
        if ref.nwords == 2:
            v &= 0xFFFF_FFFF
            arr[ref.word] = v & 0xFFFF
            if ref.word + 1 < len(arr):
                arr[ref.word + 1] = v >> 16
        else:
            arr[ref.word] = _u16(v)


@dataclass
class Workpiece:
    id: int
    conveyor: str
    pos: float            # 前端位置 (mm)
    length: float = 50.0
    blocked: bool = False
    label: str = ""

    def state(self) -> dict:
        return {"id": self.id, "conv": self.conveyor, "pos": round(self.pos, 1),
                "len": self.length, "blocked": self.blocked}


class Behavior:
    """挙動の基底。サブクラスは step(dt, io, world) と state() を実装。"""
    type = "behavior"

    def __init__(self, cfg: TwinConfig, d: dict):
        self.cfg = cfg
        self.d = d
        self.name = str(d.get("name", self.type))

    def ref(self, key: str) -> Ref | None:
        v = self.d.get(key)
        return self.cfg.resolve(v) if v is not None else None

    def reset(self) -> None:
        pass

    def step(self, dt: float, io: IOBus, world: "World") -> None:
        pass

    def state(self) -> dict:
        return {"name": self.name, "type": self.type}


class World:
    def __init__(self, cfg: TwinConfig):
        from .behaviors import create_behavior, link_world
        self.cfg = cfg
        self.objects: list[Behavior] = [create_behavior(cfg, d) for d in cfg.objects]
        self.by_name: dict[str, Behavior] = {o.name: o for o in self.objects}
        link_world(self)
        self.works: list[Workpiece] = []
        self._next_id = 1
        self.time = 0.0

    # --- ワーク操作 -----------------------------------------------------
    def spawn(self, conveyor: str, pos: float = 0.0, length: float = 50.0, label: str = "") -> Workpiece:
        w = Workpiece(self._next_id, conveyor, pos, length, label=label)
        self._next_id += 1
        self.works.append(w)
        return w

    def remove(self, w: Workpiece) -> None:
        if w in self.works:
            self.works.remove(w)

    def works_on(self, conveyor: str) -> list[Workpiece]:
        return [w for w in self.works if w.conveyor == conveyor]

    def reset(self) -> None:
        self.works.clear()
        self.time = 0.0
        for o in self.objects:
            o.reset()

    # --- 周期処理 -------------------------------------------------------
    def step(self, dt: float, io: IOBus) -> None:
        self.time += dt
        for o in self.objects:
            o.step(dt, io, self)

    def state(self) -> dict:
        return {
            "t": round(self.time, 3),
            "works": [w.state() for w in self.works],
            "objects": [o.state() for o in self.objects],
        }
