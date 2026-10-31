"""ライン設定 (twin/linegen.py) に合わせた疑似ラダー (モック PLC 用)。実機ラダーの手本。

- カメラ n の DONE 立上りで REQ ワード (レーンビット) を読み、レーンごとに「撮像時エンコーダ値」を登録
- 毎スキャン: 走行距離 = (現在エンコーダ − 登録値) ÷ pls/mm。レーン k のノズルは段 (k % rows) にあるので
  目標距離 = トリガ線→1段目 + 段 × 段間 − 先行量。到達したら SOL k を blow_s だけ ON
"""
from __future__ import annotations

OUT, IN = 1000, 1100


class Logic:
    def __init__(self, params: dict | None = None) -> None:
        p = params or {}
        self.lanes = int(p.get("lanes", 32)); self.rows = int(p.get("rows", 1)); self.row_gap = float(p.get("row_gap", 0))
        self.ppm = float(p.get("ppm", 10)); self.dist = float(p.get("trigger_to_eject", 300)); self.lead = float(p.get("lead", 15))
        self.blow_s = float(p.get("blow_s", 0.06)); self.req_words = list(p.get("req_words", [10]))
        self.done_bits = [tuple(x) for x in p.get("done_bits", [(1, 0)])]
        self.enc_word = int(p.get("enc_word", 4)); self.sol_word = int(p.get("sol_word", 2))
        self.track: list[tuple[int, int]] = []       # (lane, enc_at_capture)
        self.prev_done = [False] * len(self.done_bits)
        self.blow_until: list[float] = [0.0] * self.lanes
        self.t = 0.0
        self.count = 0

    def scan(self, mem, dt: float) -> None:
        self.t += dt
        enc = mem.word("DM", IN + self.enc_word) | (mem.word("DM", IN + self.enc_word + 1) << 16)
        for i, (w, b) in enumerate(self.done_bits):
            done = mem.bit("DM", IN + w, b)
            if done and not self.prev_done[i]:
                rw = self.req_words[i] if i < len(self.req_words) else self.req_words[0]
                req = mem.word("DM", IN + rw) | (mem.word("DM", IN + rw + 1) << 16)
                for k in range(self.lanes):
                    if req >> k & 1:
                        self.track.append((k, enc))
            self.prev_done[i] = done
        for lane, enc0 in list(self.track):
            travel = ((enc - enc0) & 0xFFFF_FFFF) / self.ppm
            target = self.dist + (lane % self.rows) * self.row_gap - self.lead
            if travel >= target:
                self.blow_until[lane] = self.t + self.blow_s
                self.count += 1
                self.track.remove((lane, enc0))
            elif travel > 5000:
                self.track.remove((lane, enc0))
        sol = 0
        for k in range(self.lanes):
            if self.blow_until[k] > self.t:
                sol |= 1 << k
        for w in range((self.lanes + 15) // 16):
            mem.set_word("DM", OUT + self.sol_word + w, (sol >> (16 * w)) & 0xFFFF)
        mem.set_bit("DM", OUT + 0, 0, True)                        # CONV_RUN
        mem.set_word("DM", OUT + 10, len(self.track))
        mem.set_word("DM", OUT + 11, self.count & 0xFFFF)
