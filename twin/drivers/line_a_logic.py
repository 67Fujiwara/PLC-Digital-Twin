"""Line A (configs/a_twin.yaml) 用のモック PLC ロジック: 実機ラダーの代わりに PC だけで流れを確認する。

本番 PLC では「排斥REQ (MR800〜) → ON ディレイ (DM17900〜) → SOL 出力を吹付時間 (T10〜) だけ ON」を行っている想定。
ここでは同じ動きを簡略化して再現する。距離/速度 = (700-400)/300 = 1.0s を ON ディレイとする。
"""
from __future__ import annotations

OUT, IN = 1000, 1100
LANES = 32
DELAY_S = (700.0 - 400.0) / 300.0 - 0.05   # カメラ→SOL の搬送時間 − 先行量
BLOW_S = 0.06


class Logic:
    def __init__(self) -> None:
        self.timers: list[list[float]] = [[] for _ in range(LANES)]   # レーンごとの吹付け予定時刻
        self.t = 0.0
        self.prev_req = 0
        self.blow_until = [0.0] * LANES

    def scan(self, mem, dt: float) -> None:
        self.t += dt
        req = mem.word("DM", IN + 2) | (mem.word("DM", IN + 3) << 16)
        rising = req & ~self.prev_req
        self.prev_req = req
        for k in range(LANES):
            if rising >> k & 1:
                self.timers[k].append(self.t + DELAY_S)
        sol = 0
        for k in range(LANES):
            while self.timers[k] and self.timers[k][0] <= self.t:
                self.timers[k].pop(0)
                self.blow_until[k] = self.t + BLOW_S
            if self.blow_until[k] > self.t:
                sol |= 1 << k
        mem.set_word("DM", OUT + 2, sol & 0xFFFF)
        mem.set_word("DM", OUT + 3, sol >> 16)
        mem.set_bit("DM", OUT + 0, 4, True)          # CONV_RUN
        mem.set_word("DM", OUT + 10, int(self.t) & 0xFFFF)
