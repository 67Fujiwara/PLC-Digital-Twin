"""キーエンス KV-8000 / KV-7000 / KV-Nano 上位リンク通信 (Ethernet, TCP ポート 8501 / UDP ポート 8500)。

コマンド (ASCII, CR終端):
  RDS <dev><no>.U <count>\\r              連続読出し  -> "v v v ...\\r\\n"
  WRS <dev><no>.U <count> v v v ...\\r    連続書込み  -> "OK\\r\\n"
エラー応答: E0(デバイス番号異常) E1(コマンド異常) E4(書込み禁止) E5(ユニット異常) E6(ユニット番号異常)

リレーデバイス (MR/R/LR/CR/B/VB) はチャンネル単位 (16点=1ワード) で読み書きする。
  例) MR100 (=ch1 bit00) を先頭に 2ワード -> "RDS MR100.U 2"
"""
from __future__ import annotations

from .base import NetDriver, DriverError, ProtocolError

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


class KeyenceHostLink(NetDriver):
    name = "keyence"

    def __init__(self, ip: str, port: int | None = None, timeout: float = 1.0, protocol: str = "tcp"):
        if port is None:
            port = 8500 if protocol.lower() == "udp" else 8501
        super().__init__(ip, port, timeout, protocol)

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

    @classmethod
    def build_set_reset(cls, dev: str, word_addr: int, bit: int, value: bool) -> bytes:
        """リレー 1 点の ST (セット) / RS (リセット)。ワードデバイスは bit 指定なしの ST/RS が無いので呼ばない。"""
        dev = dev.upper()
        if dev == "B":
            no = f"{word_addr + bit:X}"                  # B はビット番号 (16 の倍数) 基準 (B20 = 0x20)
        elif dev in CH_DEVICES:
            no = f"{word_addr // 100}{bit:02d}"       # ch*100 + bit  (例 MR1 05 → MR105, ch0 → MR005)
        else:
            raise DriverError(f"keyence: {dev} は ST/RS 対象外")
        return f"{'ST' if value else 'RS'} {dev}{no}\r".encode("ascii")

    @staticmethod
    def parse_read(resp: bytes, n: int, cmd: str = "") -> list[int]:
        text = resp.decode("ascii", errors="replace").strip()
        if text in ERRORS or (len(text) == 2 and text[0] == "E"):
            raise ProtocolError(f"keyence: {text} {ERRORS.get(text, '')} (コマンド: {cmd or 'RDS'})")
        parts = text.split()
        if len(parts) != n:
            raise ProtocolError(f"keyence: 応答点数不一致 期待{n} 実際{len(parts)}: {text[:60]!r} (コマンド: {cmd})")
        try:
            return [int(p) & 0xFFFF for p in parts]
        except ValueError as e:
            raise ProtocolError(f"keyence: 応答解析失敗: {text[:60]!r} (コマンド: {cmd})") from e

    @staticmethod
    def parse_write(resp: bytes, cmd: str = "") -> None:
        text = resp.decode("ascii", errors="replace").strip()
        if text != "OK":
            raise ProtocolError(f"keyence: 書込み応答 {text!r} {ERRORS.get(text, '')} (コマンド: {cmd[:40] or 'WRS'})")

    # --- 通信 ---------------------------------------------------------------
    def read_words(self, dev: str, addr: int, n: int) -> list[int]:
        frame = self.build_read(dev, addr, n)
        self._send(frame)
        return self.parse_read(self._recv_until(b"\r\n"), n, frame.decode("ascii").strip())

    def write_words(self, dev: str, addr: int, values: list[int]) -> None:
        frame = self.build_write(dev, addr, values)
        self._send(frame)
        self.parse_write(self._recv_until(b"\r\n"), frame.decode("ascii").strip())

    def write_bit(self, dev: str, word_addr: int, bit: int, value: bool) -> None:
        dev = dev.upper()
        if dev in CH_DEVICES or dev == "B":           # リレーは 1 点だけ ST/RS (他ビットを潰さない)
            frame = self.build_set_reset(dev, word_addr, bit, value)
            self._send(frame)
            self.parse_write(self._recv_until(b"\r\n"), frame.decode("ascii").strip())
        else:                                          # DM などワードのビットは RMW
            super().write_bit(dev, word_addr, bit, value)
