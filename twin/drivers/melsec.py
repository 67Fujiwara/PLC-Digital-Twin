"""三菱 MELSEC iQ-R / Q / L  SLMP (MCプロトコル 3Eフレーム・バイナリ) TCP / UDP。

GX Works3 側: [ユニットパラメータ]-[外部機器構成] で SLMP接続機器 (TCP, ポート例 5000〜) を追加。
コマンド: 一括読出し 0x0401 / 一括書込み 0x1401、サブコマンド 0x0000 (ワード単位)。
ビットデバイス (M, X, Y, B...) はワード単位 (16点) で扱う。先頭番号は16の倍数にする。
"""
from __future__ import annotations

import struct

from .base import NetDriver, DriverError, ProtocolError

DEVICE_CODES = {
    "SM": 0x91, "SD": 0xA9, "X": 0x9C, "Y": 0x9D, "M": 0x90, "L": 0x92, "F": 0x93,
    "V": 0x94, "B": 0xA0, "D": 0xA8, "W": 0xB4, "TS": 0xC1, "TC": 0xC0, "TN": 0xC2,
    "SS": 0xC7, "SC": 0xC6, "SN": 0xC8, "CS": 0xC4, "CC": 0xC3, "CN": 0xC5,
    "SB": 0xA1, "SW": 0xB5, "S": 0x98, "DX": 0xA2, "DY": 0xA3, "Z": 0xCC, "R": 0xAF, "ZR": 0xB0,
}
BIT_DEVICES = {"SM", "X", "Y", "M", "L", "F", "V", "B", "TS", "TC", "SS", "SC", "CS", "CC", "SB", "S", "DX", "DY"}
MAX_WORDS = 960

END_CODES = {
    0xC050: "デバイス指定不正 (デバイスコード/点数)",
    0xC051: "点数超過",
    0xC056: "アドレス範囲外",
    0xC059: "コマンド/サブコマンド不正",
    0xC05B: "指定デバイスは対象外",
    0xC05C: "要求内容不正 (ワード単位でビットデバイス指定時は先頭を16の倍数に)",
    0xC05F: "対象ユニットに対して実行できない要求",
    0xC060: "要求内容 (データ) 不正",
    0xC061: "要求データ長が不正",
    0xC0D8: "ブロック数超過",
    0xC200: "リモートパスワード不正",
    0xC204: "リモートパスワード未解除",
}


class MelsecSLMP(NetDriver):
    name = "melsec"

    def __init__(self, ip: str, port: int = 5000, timeout: float = 1.0,
                 network: int = 0, station: int = 0xFF, timer: int = 0x0010, protocol: str = "tcp"):
        super().__init__(ip, port, timeout, protocol)
        self.network = network
        self.station = station
        self.timer = timer  # 監視タイマ (250ms単位)

    def _ctor_args(self) -> dict:
        d = super()._ctor_args()
        d.update(network=self.network, station=self.station, timer=self.timer)
        return d

    # --- フレーム -----------------------------------------------------------
    def _header(self, body: bytes) -> bytes:
        # サブヘッダ 50 00 / ネットワーク / PC番号 FF / 要求先ユニット I/O 03FF / 局番 00 / データ長 / 監視タイマ
        data = struct.pack("<H", self.timer) + body
        return (b"\x50\x00" + bytes([self.network, self.station]) + b"\xFF\x03" + b"\x00"
                + struct.pack("<H", len(data)) + data)

    @staticmethod
    def _device(dev: str, addr: int) -> bytes:
        dev = dev.upper()
        if dev not in DEVICE_CODES:
            raise DriverError(f"melsec: 未対応デバイス {dev}")
        if not (0 <= addr <= 0xFFFFFF):
            raise DriverError(f"melsec: デバイス番号が範囲外 {dev}{addr}")
        if dev in BIT_DEVICES and addr % 16:
            raise DriverError(f"melsec: ビットデバイスをワード単位で扱うため先頭番号は16の倍数にしてください ({dev}{addr})")
        return struct.pack("<I", addr)[:3] + bytes([DEVICE_CODES[dev]])

    def build_read(self, dev: str, addr: int, n: int) -> bytes:
        if not (1 <= n <= MAX_WORDS):
            raise DriverError(f"melsec: 読出し点数は 1〜{MAX_WORDS}")
        body = struct.pack("<HH", 0x0401, 0x0000) + self._device(dev, addr) + struct.pack("<H", n)
        return self._header(body)

    def build_write(self, dev: str, addr: int, values: list[int]) -> bytes:
        if not (1 <= len(values) <= MAX_WORDS):
            raise DriverError(f"melsec: 書込み点数は 1〜{MAX_WORDS}")
        body = (struct.pack("<HH", 0x1401, 0x0000) + self._device(dev, addr)
                + struct.pack("<H", len(values)) + b"".join(struct.pack("<H", v & 0xFFFF) for v in values))
        return self._header(body)

    @staticmethod
    def parse_response(frame_head: bytes) -> tuple[int, int]:
        """応答ヘッダ 9バイト -> (残りデータ長, 0)。サブヘッダ D0 00 を検証。"""
        if frame_head[:2] != b"\xD0\x00":
            raise DriverError(f"melsec: 応答サブヘッダ不正 {frame_head[:2].hex()}")
        length = struct.unpack("<H", frame_head[7:9])[0]
        return length, 0

    @staticmethod
    def parse_data(data: bytes, n: int) -> list[int]:
        end = struct.unpack("<H", data[:2])[0]
        if end != 0:
            raise ProtocolError(f"melsec: 終了コード 0x{end:04X} {END_CODES.get(end, '')}")
        payload = data[2:]
        if len(payload) != n * 2:
            raise ProtocolError(f"melsec: 応答長不一致 期待{n * 2} 実際{len(payload)}")
        return list(struct.unpack(f"<{n}H", payload))

    # --- 通信 ---------------------------------------------------------------
    def _transact(self, frame: bytes) -> bytes:
        self._send(frame)
        head = self._recv_exact(9)
        length, _ = self.parse_response(head)
        return self._recv_exact(length)

    def read_words(self, dev: str, addr: int, n: int) -> list[int]:
        try:
            return self.parse_data(self._transact(self.build_read(dev, addr, n)), n)
        except ProtocolError as e:
            raise ProtocolError(f"{e} (読出し {dev}{addr} x{n})") from None

    def write_words(self, dev: str, addr: int, values: list[int]) -> None:
        data = self._transact(self.build_write(dev, addr, values))
        end = struct.unpack("<H", data[:2])[0]
        if end != 0:
            raise ProtocolError(f"melsec: 書込み終了コード 0x{end:04X} {END_CODES.get(end, '')} (書込み {dev}{addr} x{len(values)})")
