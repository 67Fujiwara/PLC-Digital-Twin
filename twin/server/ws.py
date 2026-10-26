"""最小 WebSocket (RFC6455) フレーム処理。外部ライブラリ不要。

サーバ側: 受信フレームはマスク済み (クライアント->サーバ)、送信はマスクなし。
対応: text / binary / ping / pong / close、拡張長 (16/64bit)、連続フレームの結合。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import struct

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1(key.strip().encode() + GUID).digest()).decode()


def encode_frame(payload: bytes, opcode: int = OP_TEXT, fin: bool = True) -> bytes:
    head = bytes([(0x80 if fin else 0) | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


class WSClosed(Exception):
    pass


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes, bool]:
    """1フレーム読む -> (opcode, payload, fin)。クライアントのマスクを解除する。"""
    try:
        b0, b1 = await reader.readexactly(2)
    except (asyncio.IncompleteReadError, ConnectionError):
        raise WSClosed()
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    n = b1 & 0x7F
    try:
        if n == 126:
            n = struct.unpack(">H", await reader.readexactly(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", await reader.readexactly(8))[0]
        if n > 16 * 1024 * 1024:
            raise WSClosed()
        mask = await reader.readexactly(4) if masked else b""
        data = await reader.readexactly(n)
    except (asyncio.IncompleteReadError, ConnectionError):
        raise WSClosed()
    if masked:
        data = bytes(data[i] ^ mask[i % 4] for i in range(n)) if n < 4096 else _unmask(data, mask)
    return opcode, data, fin


def _unmask(data: bytes, mask: bytes) -> bytes:
    # 大きなフレーム向け: int 演算でまとめて XOR
    n = len(data)
    rep = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(rep, "big")).to_bytes(n, "big")


async def read_message(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> tuple[int, bytes]:
    """制御フレームを処理しつつ、完結した text/binary メッセージを返す。"""
    buf = bytearray()
    msg_op = None
    while True:
        op, data, fin = await read_frame(reader)
        if op == OP_PING:
            writer.write(encode_frame(data, OP_PONG))
            continue
        if op == OP_PONG:
            continue
        if op == OP_CLOSE:
            try:
                writer.write(encode_frame(data[:2], OP_CLOSE))
                await writer.drain()
            except Exception:  # noqa: BLE001
                pass
            raise WSClosed()
        if op in (OP_TEXT, OP_BIN):
            msg_op = op
            buf = bytearray(data)
        elif op == OP_CONT:
            buf += data
        else:
            raise WSClosed()
        if fin:
            return msg_op or OP_TEXT, bytes(buf)
