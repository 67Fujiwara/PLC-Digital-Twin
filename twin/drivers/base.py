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

    def clone(self) -> "Driver":
        """同じ設定の別接続 (読み/書き並列用)。既定は自分自身を返す (並列不可)。"""
        return self


# ---------------------------------------------------------------------------
# トランスポート (TCP / UDP)
# ---------------------------------------------------------------------------
class Transport:
    name = "tcp"

    def __init__(self, ip: str, port: int, timeout: float = 1.0):
        self.ip, self.port, self.timeout = ip, port, timeout
        self.sock: socket.socket | None = None

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def connect(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def send(self, data: bytes) -> None:
        raise NotImplementedError

    def recv_exact(self, n: int) -> bytes:
        raise NotImplementedError

    def recv_until(self, term: bytes, limit: int = 65536) -> bytes:
        raise NotImplementedError


class TcpTransport(Transport):
    """TCP常時接続 + TCP_NODELAY。"""
    name = "tcp"

    def connect(self) -> None:
        self.close()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect((self.ip, self.port))
        except OSError as e:
            s.close()
            raise DriverError(f"tcp: {self.ip}:{self.port} に接続できません ({e})") from e
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = s

    def send(self, data: bytes) -> None:
        if self.sock is None:
            raise DriverError("tcp: 未接続")
        try:
            self.sock.sendall(data)
        except OSError as e:
            self.close()
            raise DriverError(f"tcp: 送信失敗 ({e})") from e

    def recv_exact(self, n: int) -> bytes:
        assert self.sock is not None
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self.sock.recv(n - len(buf))
            except socket.timeout as e:
                self.close()
                raise DriverError("tcp: 応答タイムアウト") from e
            except OSError as e:
                self.close()
                raise DriverError(f"tcp: 受信失敗 ({e})") from e
            if not chunk:
                self.close()
                raise DriverError("tcp: 切断されました")
            buf += chunk
        return bytes(buf)

    def recv_until(self, term: bytes, limit: int = 65536) -> bytes:
        assert self.sock is not None
        buf = bytearray()
        while not buf.endswith(term):
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout as e:
                self.close()
                raise DriverError("tcp: 応答タイムアウト") from e
            except OSError as e:
                self.close()
                raise DriverError(f"tcp: 受信失敗 ({e})") from e
            if not chunk:
                self.close()
                raise DriverError("tcp: 切断されました")
            buf += chunk
            if len(buf) > limit:
                self.close()
                raise DriverError("tcp: 応答が長すぎます")
        return bytes(buf)


class UdpTransport(Transport):
    """UDP (コネクト済みソケット)。1 応答 = 1 データグラム前提だが複数に分かれても結合する。
    TCP より往復が短く、再送制御が無いぶん応答タイムアウト時は DriverError で再接続する。"""
    name = "udp"

    def __init__(self, ip: str, port: int, timeout: float = 1.0):
        super().__init__(ip, port, timeout)
        self._buf = bytearray()

    def connect(self) -> None:
        self.close()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(self.timeout)
        try:
            s.connect((self.ip, self.port))
        except OSError as e:
            s.close()
            raise DriverError(f"udp: {self.ip}:{self.port} を開けません ({e})") from e
        self.sock = s
        self._buf.clear()

    def send(self, data: bytes) -> None:
        if self.sock is None:
            raise DriverError("udp: 未接続")
        self._buf.clear()                      # 古い応答を捨てる
        try:
            self.sock.send(data)
        except OSError as e:
            self.close()
            raise DriverError(f"udp: 送信失敗 ({e})") from e

    def _fill(self) -> None:
        assert self.sock is not None
        try:
            self._buf += self.sock.recv(65535)
        except socket.timeout as e:
            self.close()
            raise DriverError("udp: 応答タイムアウト") from e
        except OSError as e:
            self.close()
            raise DriverError(f"udp: 受信失敗 ({e})") from e

    def recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._fill()
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def recv_until(self, term: bytes, limit: int = 65536) -> bytes:
        while not self._buf.endswith(term):
            self._fill()
            if len(self._buf) > limit:
                self.close()
                raise DriverError("udp: 応答が長すぎます")
        out = bytes(self._buf)
        self._buf.clear()
        return out


def make_transport(protocol: str, ip: str, port: int, timeout: float) -> Transport:
    p = (protocol or "tcp").lower()
    if p == "udp":
        return UdpTransport(ip, port, timeout)
    if p == "tcp":
        return TcpTransport(ip, port, timeout)
    raise DriverError(f"protocol は tcp / udp のいずれか: {protocol}")


class NetDriver(Driver):
    """トランスポートを持つドライバの共通部分。"""

    def __init__(self, ip: str, port: int, timeout: float = 1.0, protocol: str = "tcp"):
        self.ip, self.port, self.timeout, self.protocol = ip, port, timeout, protocol
        self.t = make_transport(protocol, ip, port, timeout)

    def connect(self) -> None:
        self.t.connect()

    def close(self) -> None:
        self.t.close()

    @property
    def connected(self) -> bool:
        return self.t.connected

    # 互換 (旧 TcpDriver API)
    def _send(self, data: bytes) -> None:
        self.t.send(data)

    def _recv_exact(self, n: int) -> bytes:
        return self.t.recv_exact(n)

    def _recv_until(self, term: bytes, limit: int = 65536) -> bytes:
        return self.t.recv_until(term, limit)

    def clone(self) -> "NetDriver":
        return type(self)(**self._ctor_args())

    def _ctor_args(self) -> dict:
        return {"ip": self.ip, "port": self.port, "timeout": self.timeout, "protocol": self.protocol}


TcpDriver = NetDriver   # 後方互換
