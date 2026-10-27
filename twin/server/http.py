"""asyncio ストリームだけで動く小さな HTTP/1.1 + WebSocket サーバ。

- GET <path>        : static_dir から静的配信 (index.html 既定)
- GET/POST /api/... : routes に登録した非同期ハンドラ -> (status, content_type, body)
- GET /ws (Upgrade) : WebSocket。on_connect / on_message / on_disconnect コールバック
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
from typing import Awaitable, Callable, Tuple
from urllib.parse import urlsplit, parse_qs

from .ws import accept_key, encode_frame, read_message, WSClosed, OP_TEXT

Handler = Callable[["Request"], Awaitable[Tuple[int, str, bytes]]]
REASONS = {200: "OK", 204: "No Content", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
           500: "Internal Server Error", 101: "Switching Protocols"}


class Request:
    def __init__(self, method: str, path: str, headers: dict[str, str], body: bytes):
        self.method = method
        parts = urlsplit(path)
        self.path = parts.path
        self.query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body.decode("utf-8") or "null")


class WSClient:
    def __init__(self, writer: asyncio.StreamWriter, peer: str):
        self.writer = writer
        self.peer = peer
        self.alive = True

    async def send_text(self, text: str) -> None:
        if not self.alive:
            return
        try:
            self.writer.write(encode_frame(text.encode("utf-8"), OP_TEXT))
            await self.writer.drain()
        except Exception:  # noqa: BLE001
            self.alive = False

    def close(self) -> None:
        self.alive = False
        try:
            self.writer.close()
        except Exception:  # noqa: BLE001
            pass


class WebServer:
    def __init__(self, host: str, port: int, static_dir: str):
        self.host = host
        self.port = port
        self.static_dir = os.path.abspath(static_dir)
        self.routes: dict[tuple[str, str], Handler] = {}
        self.clients: set[WSClient] = set()
        self.on_ws_connect: Callable[[WSClient], Awaitable[None]] | None = None
        self.on_ws_message: Callable[[WSClient, str], Awaitable[None]] | None = None
        self.on_ws_disconnect: Callable[[WSClient], Awaitable[None]] | None = None
        self._server: asyncio.AbstractServer | None = None

    def route(self, method: str, path: str):
        def deco(fn: Handler) -> Handler:
            self.routes[(method.upper(), path)] = fn
            return fn
        return deco

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port, reuse_address=True)

    async def stop(self) -> None:
        for c in list(self.clients):
            c.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def broadcast(self, text: str) -> None:
        if not self.clients:
            return
        data = encode_frame(text.encode("utf-8"), OP_TEXT)
        dead = []
        for c in list(self.clients):
            try:
                c.writer.write(data)
            except Exception:  # noqa: BLE001
                dead.append(c)
        for c in list(self.clients):
            try:
                await asyncio.wait_for(c.writer.drain(), timeout=2.0)
            except Exception:  # noqa: BLE001
                dead.append(c)
        for c in dead:
            c.alive = False
            self.clients.discard(c)
            c.close()

    # --- 接続処理 -------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer_s = f"{peer[0]}:{peer[1]}" if peer else "?"
        try:
            while True:
                req = await self._read_request(reader)
                if req is None:
                    break
                if req.headers.get("upgrade", "").lower() == "websocket":
                    await self._websocket(req, reader, writer, peer_s)
                    break
                keep = await self._respond(req, writer)
                if not keep:
                    break
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self._write_response(writer, 500, "text/plain; charset=utf-8", str(e).encode())
                await writer.drain()
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _read_request(self, reader: asyncio.StreamReader) -> Request | None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=30)
        except asyncio.TimeoutError:
            return None
        if not line:
            return None
        parts = line.decode("latin-1").strip().split(" ")
        if len(parts) < 2:
            return None
        method, path = parts[0].upper(), parts[1]
        headers: dict[str, str] = {}
        while True:
            h = await reader.readline()
            if h in (b"\r\n", b"\n", b""):
                break
            k, _, v = h.decode("latin-1").partition(":")
            headers[k.strip().lower()] = v.strip()
        n = int(headers.get("content-length", "0") or 0)
        body = await reader.readexactly(n) if n else b""
        return Request(method, path, headers, body)

    def _write_response(self, writer: asyncio.StreamWriter, status: int, ctype: str, body: bytes,
                        extra: dict[str, str] | None = None) -> None:
        head = [f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}", f"Content-Type: {ctype}",
                f"Content-Length: {len(body)}", "Cache-Control: no-store", "Connection: keep-alive",
                "Access-Control-Allow-Origin: *"]
        for k, v in (extra or {}).items():
            head.append(f"{k}: {v}")
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)

    async def _respond(self, req: Request, writer: asyncio.StreamWriter) -> bool:
        h = self.routes.get((req.method, req.path))
        if h is not None:
            try:
                status, ctype, body = await h(req)
            except Exception as e:  # noqa: BLE001
                status, ctype, body = 500, "application/json", json.dumps({"error": str(e)}).encode()
            self._write_response(writer, status, ctype, body)
        elif req.method == "GET":
            self._serve_static(req.path, writer)
        else:
            self._write_response(writer, 405, "text/plain", b"method not allowed")
        await writer.drain()
        return req.headers.get("connection", "").lower() != "close"

    def _serve_static(self, path: str, writer: asyncio.StreamWriter) -> None:
        rel = path.lstrip("/") or "index.html"
        full = os.path.abspath(os.path.join(self.static_dir, rel))
        if not full.startswith(self.static_dir + os.sep) and full != self.static_dir:
            self._write_response(writer, 404, "text/plain", b"not found")
            return
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            self._write_response(writer, 404, "text/plain", b"not found")
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            self._write_response(writer, 200, ctype, f.read())

    async def _websocket(self, req: Request, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                         peer: str) -> None:
        key = req.headers.get("sec-websocket-key")
        if not key:
            self._write_response(writer, 400, "text/plain", b"bad websocket request")
            await writer.drain()
            return
        resp = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept_key(key)}\r\n\r\n")
        writer.write(resp.encode("latin-1"))
        await writer.drain()
        client = WSClient(writer, peer)
        self.clients.add(client)
        try:
            if self.on_ws_connect:
                await self.on_ws_connect(client)
            while client.alive:
                op, data = await read_message(reader, writer)
                if op == OP_TEXT and self.on_ws_message:
                    await self.on_ws_message(client, data.decode("utf-8", errors="replace"))
        except WSClosed:
            pass
        finally:
            client.alive = False
            self.clients.discard(client)
            if self.on_ws_disconnect:
                await self.on_ws_disconnect(client)
