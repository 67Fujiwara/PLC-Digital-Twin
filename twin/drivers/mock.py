"""モックPLC (ソフトウェア)。実機なしでTwin Core / ブラウザUI を動かすため。

- デバイスは dict[str, list[int]] のワード配列。
- plc.logic に Python モジュール名 (例: twin.drivers.demo_logic) を指定すると、
  read_words のたびに logic.scan(mem, dt) が呼ばれ、ラダー相当の処理を実行する。
"""
from __future__ import annotations

import importlib
import time

from .base import Driver, DriverError


class Memory:
    """デバイスメモリ。mem.word("DM", 1000) / mem.set_word / mem.bit / mem.set_bit"""

    def __init__(self) -> None:
        self.banks: dict[str, dict[int, int]] = {}

    def _bank(self, dev: str) -> dict[int, int]:
        return self.banks.setdefault(dev.upper(), {})

    def word(self, dev: str, addr: int) -> int:
        return self._bank(dev).get(addr, 0)

    def set_word(self, dev: str, addr: int, v: int) -> None:
        self._bank(dev)[addr] = v & 0xFFFF

    def words(self, dev: str, addr: int, n: int) -> list[int]:
        b = self._bank(dev)
        return [b.get(addr + i, 0) for i in range(n)]

    def set_words(self, dev: str, addr: int, vals: list[int]) -> None:
        b = self._bank(dev)
        for i, v in enumerate(vals):
            b[addr + i] = v & 0xFFFF

    def bit(self, dev: str, addr: int, bit: int) -> bool:
        return bool(self.word(dev, addr) >> bit & 1)

    def set_bit(self, dev: str, addr: int, bit: int, v: bool) -> None:
        w = self.word(dev, addr)
        self.set_word(dev, addr, (w | (1 << bit)) if v else (w & ~(1 << bit)))

    def s32(self, dev: str, addr: int) -> int:
        v = self.word(dev, addr) | (self.word(dev, addr + 1) << 16)
        return v - 0x1_0000_0000 if v & 0x8000_0000 else v

    def set_s32(self, dev: str, addr: int, v: int) -> None:
        v &= 0xFFFF_FFFF
        self.set_word(dev, addr, v & 0xFFFF)
        self.set_word(dev, addr + 1, v >> 16)


class MockPLC(Driver):
    name = "mock"

    def __init__(self, logic: str | None = None, scan_ms: float = 2.0):
        self.mem = Memory()
        self.logic = None
        self.logic_name = logic
        self.scan_ms = scan_ms
        self._last = time.perf_counter()
        self._connected = False
        if logic:
            mod = importlib.import_module(logic)
            self.logic = mod.Logic() if hasattr(mod, "Logic") else mod

    def connect(self) -> None:
        self._connected = True
        self._last = time.perf_counter()

    def clone(self) -> "MockPLC":
        """並列 R/W 用: 同じメモリを共有する別インスタンス (ロジックは元だけが回す)。"""
        c = MockPLC(None, self.scan_ms)
        c.mem = self.mem
        return c

    def close(self) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def _scan(self) -> None:
        if self.logic is None:
            return
        now = time.perf_counter()
        dt = now - self._last
        self._last = now
        self.logic.scan(self.mem, dt)

    def read_words(self, dev: str, addr: int, n: int) -> list[int]:
        if not self._connected:
            raise DriverError("mock: 未接続")
        self._scan()
        return self.mem.words(dev, addr, n)

    def write_words(self, dev: str, addr: int, values: list[int]) -> None:
        if not self._connected:
            raise DriverError("mock: 未接続")
        self.mem.set_words(dev, addr, values)
