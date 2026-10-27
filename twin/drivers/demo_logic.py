"""デモ用ラダー相当ロジック (モックPLC用)。configs/demo_line.yaml と対で使う。

実機では本番ラダーがこの役割を担う。ここでは「PLCがこう動く」を Python で模した例。
I/O 割付 (DM1000=out: PLC→PC, DM1100=in: PC→PLC) は demo_line.yaml の tags と一致させる。
"""
from __future__ import annotations

OUT, IN = 1000, 1100


class Logic:
    def __init__(self) -> None:
        self.t = 0.0
        self.step = 0          # 0:待機 1:押出し 2:押出し保持 3:戻り 4:ストッパ解放
        # 押出しシリンダは REJECT_ALL (in.1.1) ON のときだけ動く。NG 排出はカメラ + エアブローが担う
        self.timer = 0.0
        self.count = 0
        self.reject = 0
        self.ax_step = 0
        self.ax_timer = 0.0
        self.scan_count = 0
        # --- カメラ追従排出 (実機ラダーの手本) ---
        self.CAM_AT = 600.0        # カメラ トリガ線位置 mm (YAML の camera.at と一致させる)
        self.BLOW_AT = 1200.0      # エアブロー位置 mm (airblow.at)
        self.PPM = 10.0            # エンコーダ pulses/mm (conveyor.pulses_per_mm)
        self.PITCH = 75.0          # ノズルピッチ mm (幅 300 / 4 連)
        self.NOZZLES = 4
        self.BLOW_TIME = 0.10      # 吹付け時間 s
        self.LEAD = 15.0           # 先行量 mm (ブロー立上り遅れ分)
        self.track: list[dict] = []   # 追従テーブル {x, y, enc}
        self.blow_timer = [0.0] * self.NOZZLES
        self.prev_done = False
        self.blow_count = 0

    def scan(self, mem, dt: float) -> None:
        self.t += dt
        self.scan_count = (self.scan_count + 1) & 0xFFFF
        # ---- 入力 (仮想センサ) ------------------------------------------
        cyl_end = mem.bit("DM", IN + 0, 0)
        cyl_home = mem.bit("DM", IN + 0, 1)
        pe1 = mem.bit("DM", IN + 0, 2)
        pe2 = mem.bit("DM", IN + 0, 3)
        ax_busy = mem.bit("DM", IN + 0, 4)
        ax_inpos = mem.bit("DM", IN + 0, 5)
        estop = mem.bit("DM", IN + 1, 0)      # 手動で ON できる非常停止 (in.16)
        reject_mode = mem.bit("DM", IN + 1, 1)  # 手動: ON なら全数排出

        run = not estop
        # ---- 搬送 ----------------------------------------------------------
        mem.set_bit("DM", OUT + 0, 0, run)                 # CONV1_RUN
        mem.set_bit("DM", OUT + 0, 3, run)                 # CONV2_RUN
        # ---- 押出しシリンダ シーケンス (PE1 検出 -> 奇数個目 or 排出モードを排出) --
        extend = False
        stopper_up = True
        if self.step == 0:
            if pe1 and cyl_home and run:
                self.count += 1
                if reject_mode:
                    self.step = 1
                else:
                    self.step = 4
                self.timer = 0.0
        elif self.step == 1:                     # 押出し
            extend = True
            if cyl_end:
                self.step = 2
                self.timer = 0.0
        elif self.step == 2:                     # 伸端保持 0.3s
            extend = True
            self.timer += dt
            if self.timer >= 0.3:
                self.step = 3
                self.reject += 1
        elif self.step == 3:                     # 戻り
            if cyl_home:
                self.step = 4
                self.timer = 0.0
        elif self.step == 4:                     # ストッパ解放 (通過させる)
            stopper_up = False
            self.timer += dt
            if self.timer >= 0.8 or not pe1 and self.timer >= 0.3:
                self.step = 0
        if not run:
            extend = False
        mem.set_bit("DM", OUT + 0, 1, extend)              # CYL1_EXT
        mem.set_bit("DM", OUT + 0, 2, stopper_up)          # ST1_UP
        # ---- サーボ軸: PE2 検出で 0 <-> 500 を交互に位置決め ------------
        ax_start = False
        if self.ax_step == 0:
            if pe2 and not ax_busy and run:
                tgt = 0 if mem.s32("DM", OUT + 10) == 500 else 500
                mem.set_s32("DM", OUT + 10, tgt)           # AX1_TARGET
                self.ax_step = 1
                self.ax_timer = 0.0
        elif self.ax_step == 1:
            ax_start = True
            self.ax_timer += dt
            if ax_busy or self.ax_timer > 0.1:
                self.ax_step = 2
        elif self.ax_step == 2:
            if not ax_busy and ax_inpos:
                self.ax_step = 3
                self.ax_timer = 0.0
        elif self.ax_step == 3:
            self.ax_timer += dt
            if not pe2 or self.ax_timer > 2.0:
                self.ax_step = 0
        mem.set_bit("DM", OUT + 0, 4, ax_start)            # AX1_START
        # ---- カメラ結果の受取り → 追従テーブルへ登録 ------------------------
        cam_done = mem.bit("DM", IN + 0, 6)
        ack = False
        if cam_done and not self.prev_done:
            n = mem.word("DM", IN + 30)
            enc0 = mem.word("DM", IN + 22) | (mem.word("DM", IN + 23) << 16)
            for i in range(min(n, 4)):
                x = mem.word("DM", IN + 32 + 2 * i)
                x = x - 0x10000 if x & 0x8000 else x
                y = mem.word("DM", IN + 33 + 2 * i)
                self.track.append({"x": x, "y": y, "enc": enc0})
            ack = True                       # 受取り完了 (1 スキャンだけ ON)
        self.prev_done = cam_done
        mem.set_bit("DM", OUT + 0, 5, ack)                 # CAM_ACK
        # ---- 追従: 現在位置 = CAM_AT + X + (エンコーダ差 / PPM) → ブロー位置で該当ノズルを吹く ----
        enc_now = mem.word("DM", IN + 20) | (mem.word("DM", IN + 21) << 16)
        for t in list(self.track):
            travel = ((enc_now - t["enc"]) & 0xFFFF_FFFF) / self.PPM
            pos = self.CAM_AT + t["x"] + travel
            if pos >= self.BLOW_AT - self.LEAD:
                k = min(self.NOZZLES - 1, max(0, int(t["y"] // self.PITCH)))
                self.blow_timer[k] = self.BLOW_TIME
                self.blow_count += 1
                self.track.remove(t)
            elif travel > 3000:              # 異常: 流れ過ぎたら破棄
                self.track.remove(t)
        blow_word = 0
        for k in range(self.NOZZLES):
            if self.blow_timer[k] > 0 and run:
                blow_word |= 1 << k
                self.blow_timer[k] -= dt
        mem.set_word("DM", OUT + 2, blow_word)             # BLOW1〜4
        mem.set_word("DM", OUT + 17, len(self.track))      # TRACK_N
        mem.set_word("DM", OUT + 18, self.blow_count)      # BLOW_CNT
        # ---- 監視数値 -----------------------------------------------------
        mem.set_word("DM", OUT + 12, self.count)           # WORK_COUNT
        mem.set_word("DM", OUT + 13, self.reject)          # REJECT_COUNT
        mem.set_word("DM", OUT + 14, self.step)            # SEQ_STEP
        mem.set_word("DM", OUT + 15, self.scan_count)      # SCAN_COUNT
        hz = 60 if run else 0
        mem.set_word("DM", OUT + 16, hz * 100)             # INV_HZ (0.01Hz単位)
        # watch 用: DM100〜 に適当な数値、MR100〜 にステップ表示
        mem.set_word("DM", 100, int(self.t * 10) & 0xFFFF)
        mem.set_word("DM", 101, self.count * 7 & 0xFFFF)
        mem.set_word("MR", 100, (1 << self.step) | (0x100 if run else 0))
