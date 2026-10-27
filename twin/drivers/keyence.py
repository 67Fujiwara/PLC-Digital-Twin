"""キーエンス KV-8000 / KV-7000 / KV-Nano 上位リンク通信 (Ethernet, ポート8501)。

コマンド (ASCII, CR終端):
  RDS <dev><no>.U <count>\\r              連続読出し  -> "v v v ...\\r\\n"
  WRS <dev><no>.U <count> v v v ...\\r    連続書込み  -> "OK\\r\\n"
エラー応答: E0(デバイス番号異常) E1(コマンド異常) E4(書込み禁止) E5(ユニット異常) E6(ユニット番号異常)

リレーデバイス (MR/R/LR/CR/B/VB) はチャンネル単位 (16点=1ワード) で読み書きする。
  例) MR100 (=ch1 bit00) を先頭に 2ワード -> "RDS MR100.U 2"
"""
from __future__ import annotations

from .base import TcpDriver, DriverError

ERRORS = {
    "E0": "デバイス番号異常",
    "E1": "コマンド異常 (未対応コマンド/書式)",
    "E2": "プログラム未登録",
    "E4": "書込み禁止 (スイッチ/モード)",
    "E5": "ユニット異常",
    "E6": "ユニット番号異常",
}
CH_DEVICES = {"MR", "R", "LR", "CR", "VB", "B"}
WORD_DEVICES = {"DM", "EM", "FM", "ZF", "W", "TM", "CM", "Z", "T", "C"}
MAX_WORDS = 1000


class KeyenceHostLink(TcpDriver):
    name = "keyence"

    def __init__(self, ip: str, port: int = 8501, timeout: float = 1.0):
        super().__init__(ip, port, timeout)

    # --- 書式 ---------------------------------------------------------------
    @staticmethod
    def device_str(dev: str, addr: int) -> str:
        dev = dev.upper()
        if dev == "B":
            return f"B{addr:X}"
        if dev == "W":
            return f"W{addr:04X}"
        return f"{dev}{addr}"

    @classmethod
    def build_read(cls, dev: str, addr: int, n: int) -> bytes:
        if not (1 <= n <= MAX_WORDS):
            raise DriverError(f"keyence: 読出し点数は 1〜{MAX_WORDS}")
        return f"RDS {cls.device_str(dev, addr)}.U {n}\r".encode("ascii")

    @classmethod
    def build_write(cls, dev: str, addr: int, values: list[int]) -> bytes:
        if not (1 <= len(values) <= MAX_WORDS):
            raise DriverError(f"keyence: 書込み点数は 1〜{MAX_WORDS}")
        vals = " ".join(str(v & 0xFFFF) for v in values)
        return f"WRS {cls.device_str(dev, addr)}.U {len(values)} {vals}\r".encode("ascii")

    @staticmethod
    def parse_read(resp: bytes, n: int) -> list[int]:
        text = resp.decode("ascii", errors="replace").strip()
        if text in ERRORS or (len(text) == 2 and text[0] == "E"):
            raise DriverError(f"keyence: {text} {ERRORS.get(text, '')}")
        parts = text.split()
        if len(parts) != n:
            raise DriverError(f"keyence: 応答点数不一致 期待{n} 実際{len(parts)}: {text[:60]!r}")
        try:
            return [int(p) & 0xFFFF for p in parts]
        except ValueError as e:
            raise DriverError(f"keyence: 応答解析失敗: {text[:60]!r}") from e

    @staticmethod
    def parse_write(resp: bytes) -> None:
        text = resp.decode("ascii", errors="replace").strip()
        if text != "OK":
            raise DriverError(f"keyence: 書込み応答 {text!r} {ERRORS.get(text, '')}")

    # --- 通信 ---------------------------------------------------------------
    def read_words(self, dev: str, addr: int, n: int) -> list[int]:
        self._send(self.build_read(dev, addr, n))
        return self.parse_read(self._recv_until(b"\r\n"), n)

    def write_words(self, dev: str, addr: int, values: list[int]) -> None:
        self._send(self.build_write(dev, addr, values))
        self.parse_write(self._recv_until(b"\r\n"))
