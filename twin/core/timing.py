"""周期制御。非リアルタイムOSで 10ms を可能な限り守るための補助。"""
from __future__ import annotations

import os
import sys
import time


def raise_priority() -> str:
    """ループプロセスの優先度を上げる (失敗しても続行)。戻り値は結果メッセージ。"""
    try:
        if sys.platform == "win32":
            import ctypes
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00000080)  # HIGH_PRIORITY_CLASS
            ctypes.windll.winmm.timeBeginPeriod(1)  # type: ignore[attr-defined]
            return "windows: HIGH_PRIORITY_CLASS + timeBeginPeriod(1)"
        os.nice(-10)
        return "posix: nice -10"
    except Exception as e:  # noqa: BLE001
        return f"優先度変更不可 ({e}) - 通常優先度で続行"


def sleep_until(t_target: float, spin_margin: float = 0.0015) -> None:
    """perf_counter 基準で t_target まで待つ。直前 spin_margin 秒はビジーウェイトで精度を出す。"""
    while True:
        now = time.perf_counter()
        rem = t_target - now
        if rem <= 0:
            return
        if rem > spin_margin:
            time.sleep(rem - spin_margin)
        else:
            # スピン
            while time.perf_counter() < t_target:
                pass
            return


class JitterStats:
    """直近 N 周期の周期ジッタ統計 (ms)。"""

    def __init__(self, n: int = 200):
        self.n = n
        self.buf: list[float] = []
        self.max_all = 0.0

    def push(self, period_ms: float, nominal_ms: float) -> None:
        j = abs(period_ms - nominal_ms)
        self.buf.append(j)
        if len(self.buf) > self.n:
            self.buf.pop(0)
        self.max_all = max(self.max_all, j)

    def summary(self) -> dict:
        if not self.buf:
            return {"avg": 0.0, "max": 0.0, "p99": 0.0, "max_all": 0.0}
        s = sorted(self.buf)
        return {
            "avg": round(sum(s) / len(s), 3),
            "max": round(s[-1], 3),
            "p99": round(s[min(len(s) - 1, int(len(s) * 0.99))], 3),
            "max_all": round(self.max_all, 3),
        }
