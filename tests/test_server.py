"""HTTP/WebSocket サーバと TwinApp の結合試験 (モックPLC)。"""
import asyncio
import base64
import json
import os
import socket
import struct

import pytest

from twin.config import load_config
from twin.server.app import TwinApp
from twin.server.ws import encode_frame, accept_key

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def test_accept_key():
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def client_frame(text: str) -> bytes:
    """クライアント側 (マスクあり) のテキストフレーム。"""
    payload = text.encode()
    mask = b"\x01\x02\x03\x04"
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    head = bytes([0x81])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    else:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    return head + mask + masked


async def read_frame(reader):
    b0, b1 = await reader.readexactly(2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", await reader.readexactly(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", await reader.readexactly(8))[0]
    return b0 & 0x0F, await reader.readexactly(n)


@pytest.mark.timeout(60)
def test_app_end_to_end():
    cfg = load_config(os.path.join(ROOT, "configs", "demo_line.yaml"))
    port = free_port()
    app = TwinApp(cfg, "127.0.0.1", port, fps=30)

    async def scenario():
        app.start_loop()
        await app.server.start()
        task = asyncio.create_task(app._broadcast_task())
        try:
            await asyncio.sleep(1.0)
            # --- REST ---
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(b"GET /api/stat HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            data = await r.read()
            assert b"200 OK" in data
            body = json.loads(data.split(b"\r\n\r\n", 1)[1])
            assert body["stat"]["status"] == "connected" and body["samples"] > 50
            w.close()
            # 静的ファイル
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            data = await r.read()
            assert b"200 OK" in data and b"<title>PLC Digital Twin</title>" in data
            w.close()
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(b"GET /../twin/config.py HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            data = await r.read()
            assert b"404" in data
            w.close()
            # --- WebSocket ---
            r, w = await asyncio.open_connection("127.0.0.1", port)
            key = base64.b64encode(os.urandom(16)).decode()
            w.write((f"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            head = await r.readuntil(b"\r\n\r\n")
            assert b"101" in head and accept_key(key).encode() in head
            op, data = await read_frame(r)
            hello = json.loads(data)
            assert hello["type"] == "hello" and hello["config"]["out"]["words"] == 64
            op, data = await read_frame(r)
            hist = json.loads(data)
            assert hist["type"] == "history" and len(hist["samples"]) > 10
            # frame を受ける
            frames = []
            while len(frames) < 5:
                op, data = await read_frame(r)
                m = json.loads(data)
                if m["type"] == "frame":
                    frames.append(m)
            assert frames[-1]["stat"]["status"] == "connected"
            assert frames[-1]["sim"]["objects"]
            # 強制: in word1 bit0 (ESTOP) を ON -> CONV1_RUN (out bit0) が OFF になる
            async def wait_frame(pred, limit=90):
                for _ in range(limit):
                    op, data = await read_frame(r)
                    m = json.loads(data)
                    if m["type"] == "frame" and m["samples"] and pred(m, m["samples"][-1]):
                        return m
                raise AssertionError("期待する状態のフレームが届かない")

            w.write(client_frame(json.dumps({"type": "force_bit", "block": "in", "word": 1, "bit": 0, "value": 1})))
            await w.drain()
            m = await wait_frame(lambda m, s: s["in"][1] & 1 == 1 and s["out"][0] & 1 == 0)
            assert m["forces"]["bits"] == [{"block": "in", "word": 1, "bit": 0, "value": 1}]
            # 解除 -> 素の値 (0) に戻り、CONV1_RUN が再び ON
            w.write(client_frame(json.dumps({"type": "force_bit", "block": "in", "word": 1, "bit": 0, "value": None})))
            await w.drain()
            m = await wait_frame(lambda m, s: s["in"][1] & 1 == 0 and s["out"][0] & 1 == 1)
            assert m["forces"]["bits"] == []
            # ping/pong
            w.write(client_frame(json.dumps({"type": "ping", "t": 1})))
            w.write(encode_frame(b"hi", 0x9))  # ping 制御フレーム (マスクなしだがサーバは受理)
            await w.drain()
            got_pong = False
            for _ in range(40):
                op, data = await read_frame(r)
                if op == 0x1 and json.loads(data).get("type") == "pong":
                    got_pong = True
                    break
            assert got_pong
            # CSV
            w.close()
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(b"GET /api/record.csv HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            data = await r.read()
            text = data.split(b"\r\n\r\n", 1)[1].decode("utf-8-sig")
            lines = text.strip().splitlines()
            assert lines[0].startswith("seq,wall,period_ms,out.CONV1_RUN")
            assert len(lines) > 100
            w.close()
        finally:
            task.cancel()
            await app.shutdown()

    asyncio.run(scenario())
