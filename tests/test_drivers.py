"""ドライバのフレーム生成/解析 (実機なし)。"""
import socket
import threading

import pytest

from twin.drivers.keyence import KeyenceHostLink
from twin.drivers.melsec import MelsecSLMP
from twin.drivers.base import DriverError
from twin.drivers.mock import MockPLC


# --- Keyence --------------------------------------------------------------
def test_keyence_read_frame():
    assert KeyenceHostLink.build_read("DM", 1000, 64) == b"RDS DM1000.U 64\r"
    assert KeyenceHostLink.build_read("MR", 100, 2) == b"RDS MR100.U 2\r"
    assert KeyenceHostLink.build_read("B", 0x1F0, 1) == b"RDS B1F0.U 1\r"


def test_keyence_write_frame():
    assert KeyenceHostLink.build_write("DM", 1100, [1, 65535, 0x1234]) == b"WRS DM1100.U 3 1 65535 4660\r"
    assert KeyenceHostLink.build_write("DM", 1100, [-1]) == b"WRS DM1100.U 1 65535\r"


def test_keyence_parse():
    assert KeyenceHostLink.parse_read(b"0 12 65535\r\n", 3) == [0, 12, 65535]
    KeyenceHostLink.parse_write(b"OK\r\n")
    with pytest.raises(DriverError, match="E0"):
        KeyenceHostLink.parse_read(b"E0\r\n", 3)
    with pytest.raises(DriverError, match="E4"):
        KeyenceHostLink.parse_write(b"E4\r\n")
    with pytest.raises(DriverError, match="点数不一致"):
        KeyenceHostLink.parse_read(b"1 2\r\n", 3)


def test_keyence_roundtrip_with_fake_server():
    """簡易上位リンクサーバで read/write の往復を確認。"""
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    port = srv.getsockname()[1]
    mem = {}

    def serve():
        c, _ = srv.accept()
        buf = b""
        while True:
            d = c.recv(4096)
            if not d:
                break
            buf += d
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                p = line.decode().split()
                if p[0] == "RDS":
                    dev, n = p[1].split(".")[0], int(p[2])
                    base = int(dev[2:])
                    c.sendall((" ".join(str(mem.get(base + i, 0)) for i in range(n)) + "\r\n").encode())
                elif p[0] == "WRS":
                    base = int(p[1].split(".")[0][2:]); n = int(p[2])
                    for i in range(n):
                        mem[base + i] = int(p[3 + i])
                    c.sendall(b"OK\r\n")
        c.close()

    threading.Thread(target=serve, daemon=True).start()
    d = KeyenceHostLink("127.0.0.1", port)
    d.connect()
    d.write_words("DM", 1100, [7, 8, 9])
    assert d.read_words("DM", 1100, 4) == [7, 8, 9, 0]
    d.close()


# --- Melsec ---------------------------------------------------------------
def test_melsec_read_frame():
    d = MelsecSLMP("127.0.0.1", 5000)
    f = d.build_read("D", 1000, 64)
    # 50 00 | 00 FF | FF 03 | 00 | len | timer | cmd | sub | addr(3) | code | count
    assert f[:2] == b"\x50\x00"
    assert f[2:4] == b"\x00\xFF"
    assert f[4:6] == b"\xFF\x03"
    assert f[6] == 0
    assert int.from_bytes(f[7:9], "little") == len(f) - 9
    assert f[9:11] == b"\x10\x00"            # timer
    assert f[11:13] == b"\x01\x04"           # 0x0401
    assert f[13:15] == b"\x00\x00"           # sub
    assert f[15:18] == (1000).to_bytes(3, "little")
    assert f[18] == 0xA8                     # D
    assert f[19:21] == (64).to_bytes(2, "little")


def test_melsec_write_frame_and_bit_device():
    d = MelsecSLMP("127.0.0.1", 5000)
    f = d.build_write("D", 1100, [1, 0x1234])
    assert f[11:13] == b"\x01\x14"
    assert f[-4:] == b"\x01\x00\x34\x12"
    f = d.build_read("M", 16, 1)
    assert f[18] == 0x90
    with pytest.raises(DriverError, match="16の倍数"):
        d.build_read("M", 100, 1)


def test_melsec_parse():
    assert MelsecSLMP.parse_data(b"\x00\x00" + b"\x01\x00\xFF\xFF", 2) == [1, 65535]
    with pytest.raises(DriverError, match="C059"):
        MelsecSLMP.parse_data(b"\x59\xC0", 1)
    with pytest.raises(DriverError, match="サブヘッダ"):
        MelsecSLMP.parse_response(b"\x50\x00" + b"\x00" * 7)


def test_melsec_roundtrip_with_fake_server():
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    port = srv.getsockname()[1]
    mem = {}

    def serve():
        c, _ = srv.accept()
        while True:
            head = b""
            while len(head) < 9:
                d = c.recv(9 - len(head))
                if not d:
                    c.close(); return
                head += d
            n = int.from_bytes(head[7:9], "little")
            body = b""
            while len(body) < n:
                body += c.recv(n - len(body))
            cmd = int.from_bytes(body[2:4], "little")
            addr = int.from_bytes(body[6:9], "little")
            cnt = int.from_bytes(body[10:12], "little")
            if cmd == 0x0401:
                data = b"".join(mem.get(addr + i, 0).to_bytes(2, "little") for i in range(cnt))
            else:
                for i in range(cnt):
                    mem[addr + i] = int.from_bytes(body[12 + 2 * i:14 + 2 * i], "little")
                data = b""
            resp = b"\x00\x00" + data
            c.sendall(b"\xD0\x00\x00\xFF\xFF\x03\x00" + len(resp).to_bytes(2, "little") + resp)

    threading.Thread(target=serve, daemon=True).start()
    d = MelsecSLMP("127.0.0.1", port)
    d.connect()
    d.write_words("D", 1100, [5, 6])
    assert d.read_words("D", 1100, 3) == [5, 6, 0]
    d.close()


# --- Mock -----------------------------------------------------------------
def test_mock_memory_and_logic():
    m = MockPLC(logic="twin.drivers.demo_logic")
    m.connect()
    m.write_words("DM", 1100, [0] * 64)
    out = m.read_words("DM", 1000, 64)
    assert out[0] & 1 == 1          # CONV1_RUN
    assert out[0] >> 2 & 1 == 1     # ST1_UP
    m.mem.set_s32("DM", 50, -5)
    assert m.mem.s32("DM", 50) == -5


def test_keyence_udp_roundtrip_with_fake_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    mem = {}

    def serve():
        for _ in range(3):
            data, addr = srv.recvfrom(65535)
            p = data.decode().strip().split()
            if p[0] == "RDS":
                base, n = int(p[1].split(".")[0][2:]), int(p[2])
                srv.sendto((" ".join(str(mem.get(base + i, 0)) for i in range(n)) + "\r\n").encode(), addr)
            else:
                base, n = int(p[1].split(".")[0][2:]), int(p[2])
                for i in range(n):
                    mem[base + i] = int(p[3 + i])
                srv.sendto(b"OK\r\n", addr)

    threading.Thread(target=serve, daemon=True).start()
    d = KeyenceHostLink("127.0.0.1", port, protocol="udp")
    assert d.port == port and d.t.name == "udp"
    d.connect()
    d.write_words("DM", 1100, [3, 4])
    assert d.read_words("DM", 1100, 3) == [3, 4, 0]
    d.close()
    assert KeyenceHostLink("1.2.3.4", protocol="udp").port == 8500
    assert KeyenceHostLink("1.2.3.4").port == 8501


def test_clone_is_separate_connection():
    d = KeyenceHostLink("127.0.0.1", 9, protocol="tcp")
    c = d.clone()
    assert c is not d and c.ip == d.ip and c.port == d.port and c.protocol == "tcp"
    m = MelsecSLMP("127.0.0.1", 9, network=1, station=2)
    mc = m.clone()
    assert mc is not m and mc.network == 1 and mc.station == 2
