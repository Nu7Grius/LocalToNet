# -*- coding: utf-8 -*-
"""
examples/demo_backend.py —— 演示用的"内网后端"
================================================
内网穿透工具自己不产生内容，它只是把公网流量转进内网。所以要验证链路，
必须先有一个**内网的 HTTP 服务**。这个文件就是它，零第三方依赖，
既能直接命令行启动演示，也能被测试当夹具导入。

路由：

========================  =========================================================
路径                       行为
========================  =========================================================
``GET /``                 回 200 + JSON（含路径、时间、对端地址）
``GET /echo?msg=x``       回 200 + 原文，用来验证请求参数透传
``GET /big?kb=256``       回指定大小的二进制体，用来验证大流量分片正确
``GET /stream?chunks=50`` 逐块写出并关闭连接（无 Content-Length），用来验证流式场景
``GET /slow?ms=300``      延迟后响应，用来构造并发场景
``POST /``                回显请求体长度与前 64 字节
``其他``                   404
========================  =========================================================
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logging_setup import get_logger, setup_logging  # noqa: E402

__all__ = ["DemoBackend"]

_MAX_BODY = 8 * 1024 * 1024

_PHRASES = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}


def _build_response(
    status: int,
    body: bytes,
    *,
    content_type: str = "text/plain; charset=utf-8",
    keep_alive: bool = False,
) -> bytes:
    head = (
        f"HTTP/1.1 {status} {_PHRASES.get(status, 'OK')}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: {'keep-alive' if keep_alive else 'close'}\r\n"
        f"\r\n"
    ).encode("latin-1")
    return head + body


class DemoBackend:
    """一个够用的最小 HTTP/1.1 服务。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._log = logger or get_logger("demo-backend")
        self._server: Optional[asyncio.AbstractServer] = None
        self._tasks: set[asyncio.Task] = set()
        self.requests_served = 0

    @property
    def port(self) -> int:
        """实际监听端口。传 0 启动时会拿到内核分配的真实端口。"""
        return self._port

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._on_client, self._host, self._port)
        sockets = self._server.sockets or []
        if sockets:
            self._port = int(sockets[0].getsockname()[1])
        self._log.info("演示后端已监听 http://%s:%d", self._host, self._port)
        return self._port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except (OSError, RuntimeError):
                pass
            self._server = None
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # ------------------------------------------------------------------ #

    def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._serve(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            while True:
                try:
                    request = await self._read_request(reader)
                except (asyncio.IncompleteReadError, ConnectionError):
                    break
                if request is None:
                    break
                method, target, headers, body = request
                keep_alive = headers.get("connection", "").lower() == "keep-alive"
                self.requests_served += 1

                if method == "GET" and urlparse(target).path == "/stream":
                    await self._stream_response(writer, target)
                    return
                if method == "GET" and urlparse(target).path == "/slow":
                    await self._slow_response(writer, target)
                    return

                status, payload, content_type = self._route(method, target, body)
                writer.write(_build_response(status, payload, content_type=content_type, keep_alive=keep_alive))
                await writer.drain()
                if not keep_alive:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 演示服务，出错回 500 即可
            self._log.exception("处理请求时异常：%s", exc)
            try:
                writer.write(_build_response(500, b"backend error\n"))
                await writer.drain()
            except (OSError, ConnectionError):
                pass
        finally:
            try:
                writer.close()
            except (OSError, RuntimeError):
                pass
        self._log.debug("连接结束 peer=%s", peer)

    async def _read_request(
        self,
        reader: asyncio.StreamReader,
    ) -> Optional[Tuple[str, str, Dict[str, str], bytes]]:
        raw = await reader.readuntil(b"\r\n\r\n")
        if not raw:
            return None
        lines = raw.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) < 2:
            return None
        method, target = parts[0], parts[1]

        headers: Dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()

        body = b""
        length = headers.get("content-length")
        if length and length.isdigit():
            size = int(length)
            if size > _MAX_BODY:
                raise ConnectionError(f"请求体 {size} 字节超过上限")
            body = await reader.readexactly(size)
        return method, target, headers, body

    def _route(self, method: str, target: str, body: bytes) -> Tuple[int, bytes, str]:
        parsed = urlparse(target)
        query = parse_qs(parsed.query)
        path = parsed.path

        if method == "GET" and path == "/":
            payload = {
                "ok": True,
                "path": path,
                "time": datetime.now(timezone.utc).isoformat(),
                "query": {k: v[0] if len(v) == 1 else v for k, v in query.items()},
            }
            return 200, (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"), "application/json; charset=utf-8"

        if method == "GET" and path == "/echo":
            message = query.get("msg", [""])[0]
            return 200, (message + "\n").encode("utf-8"), "text/plain; charset=utf-8"

        if method == "GET" and path == "/big":
            size = self._int_param(query, "kb", 256, low=1, high=8192) * 1024
            block = bytes((index % 251 for index in range(1024)))
            return 200, block * (size // 1024) + block[: size % 1024], "application/octet-stream"

        if method == "POST" and path == "/":
            preview = body[:64].decode("utf-8", errors="replace")
            return 200, f"received {len(body)} bytes, preview={preview}\n".encode("utf-8"), "text/plain; charset=utf-8"

        return 404, b"not found\n", "text/plain; charset=utf-8"

    async def _stream_response(self, writer: asyncio.StreamWriter, target: str) -> None:
        """逐块写出、不用 Content-Length，靠关闭连接表示结束——流式场景。"""
        query = parse_qs(urlparse(target).query)
        chunks = self._int_param(query, "chunks", 50, low=1, high=10000)
        interval = float(query.get("interval", ["0.002"])[0])

        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        await writer.drain()
        for index in range(chunks):
            writer.write(f"chunk-{index:04d}\n".encode("utf-8"))
            await writer.drain()
            if interval > 0:
                await asyncio.sleep(interval)

    async def _slow_response(self, writer: asyncio.StreamWriter, target: str) -> None:
        query = parse_qs(urlparse(target).query)
        delay = self._int_param(query, "ms", 200, low=0, high=60000) / 1000.0
        await asyncio.sleep(delay)
        writer.write(_build_response(200, f"slept {delay:.3f}s\n"))
        await writer.drain()

    @staticmethod
    def _int_param(query: Dict[str, list], name: str, default: int, *, low: int, high: int) -> int:
        raw = query.get(name, [None])[0]
        if raw is None:
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        return max(low, min(high, value))


async def _main() -> int:
    parser = argparse.ArgumentParser(description="内网穿透演示后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    setup_logging(args.log_level)
    backend = DemoBackend(args.host, args.port)
    await backend.start()
    log = get_logger("demo-backend")
    log.info("可直接访问 http://%s:%d/ 验证；配套启动 server.py 与 client.py 后走公网端口访问", args.host, backend.port)
    try:
        while True:
            await asyncio.sleep(3600)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await backend.stop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_main()))
    except KeyboardInterrupt:
        print("\n已退出")
