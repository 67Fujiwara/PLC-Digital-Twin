"""負荷試験用モックロジック: 実機のように多数のビット・ワードが毎スキャン変わる。"""
from __future__ import annotations

import random

OUT, IN = 1000, 1100


class Logic:
    def __init__(self) -> None:
        self.rng = random.Random(1)
        self.n = 0

    def scan(self, mem, dt: float) -> None:
        self.n += 1
        # out ブロック: カウンタ・タイマ現在値・ランダムに反転するビット
        for i in range(10, 40):
            mem.set_word("DM", OUT + i, (mem.word("DM", OUT + i) + i) & 0xFFFF)
        for w in range(0, 4):
            v = mem.word("DM", OUT + w)
            for _ in range(2):
                v ^= 1 << self.rng.randrange(16)
            mem.set_word("DM", OUT + w, v)
        # 監視デバイス: MR000〜MR915 (10ch) をチラつかせ、DM0〜DM63 を動かす
        for ch in range(0, 10):
            v = mem.word("MR", ch * 100)
            v ^= 1 << self.rng.randrange(16)
            mem.set_word("MR", ch * 100, v)
        for i in range(0, 64):
            if self.rng.random() < 0.3:
                mem.set_word("DM", i, self.rng.randrange(65536))
