from __future__ import annotations

import socket


class DriverError(Exception):
    """通信エラー・PLC応答エラー。ループ側で再接続対象。"""


class Driver:
    """ワード単位一括R/Wだけを提供する最小インターフェース。

    10msを守るため、1周期は read_words 1回 + write_words 1回に固定する。
    ビット単位アクセスは提供しない (ワードに畳んでから使う)。
    """
    name = "base"

    def connect(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def read_words(self, dev: str, addr: int, n: int) -> list[int]:
        raise NotImplementedError

    def write_words(self, dev: str, addr: int, values: list[int]) -> None:
        raise NotImplementedError

    @property
    def connected(self) -> bool:
        return True


class TcpDriver(Driver):
    """TCP常時接続 + TCP_NODELAY の共通部分。"""

    def __init__(self, ip: str, port: int, timeout: float = 1.0):
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.sock: socket.socket | None = None

    def connect(self) -> None:
        self.close()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect((self.ip, self.port))
        except OSError as e:
            s.close()
            raise DriverError(f"{self.name}: {self.ip}:{self.port} に接続できません ({e})") from e
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = s

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def _send(self, data: bytes) -> None:
        if self.sock is None:
            raise DriverError(f"{self.name}: 未接続")
        try:
            self.sock.sendall(data)
        except OSError as e:
            self.close()
            raise DriverError(f"{self.name}: 送信失敗 ({e})") from e

    def _recv_exact(self, n: int) -> bytes:
        assert self.sock is not None
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self.sock.recv(n - len(buf))
            except socket.timeout as e:
                self.close()
                raise DriverError(f"{self.name}: 応答タイムアウト") from e
            except OSError as e:
                self.close()
                raise DriverError(f"{self.name}: 受信失敗 ({e})") from e
            if not chunk:
                self.close()
                raise DriverError(f"{self.name}: 切断されました")
            buf += chunk
        return bytes(buf)

    def _recv_until(self, term: bytes, limit: int = 65536) -> bytes:
        assert self.sock is not None
        buf = bytearray()
        while not buf.endswith(term):
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout as e:
                self.close()
                raise DriverError(f"{self.name}: 応答タイムアウト") from e
            except OSError as e:
                self.close()
                raise DriverError(f"{self.name}: 受信失敗 ({e})") from e
            if not chunk:
                self.close()
                raise DriverError(f"{self.name}: 切断されました")
            buf += chunk
            if len(buf) > limit:
                self.close()
                raise DriverError(f"{self.name}: 応答が長すぎます")
        return bytes(buf)
