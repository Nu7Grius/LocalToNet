# -*- coding: utf-8 -*-
"""
tests/helpers.py —— 端到端测试夹具
====================================
把"内网后端 + 服务端 + 客户端"三件套一键拉起来，并提供裸 HTTP 客户端工具。

为什么要裸 socket 发 HTTP 而不是用 `requests` / `httpx`？
    这个项目本身不解析 HTTP，它只搬运字节。用最原始的写法能让测试贴近真实链路，
    也顺带验证了"逐字节透传"没有偷偷改动报文（比如自动加了 header、改了 Content-Length）。
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from config import ClientConfig, MappingRule, ServerConfig
from examples.demo_backend import DemoBackend
from localtonet.client.core import TunnelClient
from localtonet.server.core import TunnelServer

__all__ = ["HttpResponse", "free_ports", "http_request", "http_stream_chunks", "TunnelHarness"]


def free_ports(count: int) -> List[int]:
    """向内核要 count 个空闲端口。

    同时持有所有 socket 再统一关闭，减少"拿到的端口被别人抢走"的窗口。
    """
    sockets: List[socket.socket] = []
    ports: List[int] = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
            ports.append(int(sock.getsockname()[1]))
    finally:
        for sock in sockets:
            sock.close()
    return ports


@dataclass
class HttpResponse:
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    raw: bytes = b""

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def parse_response(raw: bytes) -> HttpResponse:
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = 0
    if lines and lines[0]:
        parts = lines[0].split(" ")
        if len(parts) >= 2 and parts[1].isdigit():
            status = int(parts[1])
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return HttpResponse(status=status, headers=headers, body=body, raw=raw)


async def _send_request(
    port: int,
    path: str,
    *,
    method: str = "GET",
    body: bytes = b"",
    host: str = "127.0.0.1",
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection(host, port)
    head = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"User-Agent: localtonet-tests\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("latin-1")
    writer.write(head + body)
    await writer.drain()
    return reader, writer


async def http_request(
    port: int,
    path: str = "/",
    *,
    method: str = "GET",
    body: bytes = b"",
    host: str = "127.0.0.1",
    timeout: float = 15.0,
) -> HttpResponse:
    """发一个请求并读到 EOF，返回解析后的响应。"""
    reader, writer = await _send_request(port, path, method=method, body=body, host=host)
    chunks: List[bytes] = []
    try:
        while True:
            piece = await asyncio.wait_for(reader.read(65536), timeout=timeout)
            if not piece:
                break
            chunks.append(piece)
    except ConnectionError:
        # 服务端写完就关，Windows 下可能表现为 RST；已经读到的字节依然有效
        pass
    finally:
        await close_quietly(writer)
    return parse_response(b"".join(chunks))


async def http_stream_chunks(
    port: int,
    path: str,
    *,
    chunk_size: int = 64,
    host: str = "127.0.0.1",
    timeout: float = 15.0,
) -> List[bytes]:
    """按到达顺序读回数据块，用来验证流式响应中途没有被缓冲或重排。"""
    reader, writer = await _send_request(port, path, host=host)
    chunks: List[bytes] = []
    try:
        while True:
            piece = await asyncio.wait_for(reader.read(chunk_size), timeout=timeout)
            if not piece:
                break
            chunks.append(piece)
    except ConnectionError:
        pass
    finally:
        await close_quietly(writer)
    return chunks


async def close_quietly(writer: asyncio.StreamWriter) -> None:
    """关闭连接并吞掉清理期的所有异常。

    Windows 的 proactor 事件循环在"对端已发 RST"时会让 ``wait_closed()`` 抛
    ``ConnectionResetError``；这属于收尾噪音，不该影响测试断言。
    """
    with contextlib.suppress(Exception):
        writer.close()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(writer.wait_closed(), timeout=2.0)


class TunnelHarness:
    """一键拉起三件套，并提供等待/查询辅助。"""

    def __init__(
        self,
        *,
        start_client: bool = True,
        local_ports: Optional[Sequence[int]] = None,
        configure_server: Optional[Callable[[ServerConfig], None]] = None,
        configure_client: Optional[Callable[[ClientConfig], None]] = None,
        client_id: Optional[str] = None,
    ) -> None:
        self._start_client = start_client
        self._local_ports = list(local_ports) if local_ports else None
        self._configure_server = configure_server
        self._configure_client = configure_client
        self._client_id = client_id or f"test-{uuid.uuid4().hex[:8]}"

        self.backend = DemoBackend("127.0.0.1", 0)
        self.server: Optional[TunnelServer] = None
        self.client: Optional[TunnelClient] = None
        self.control_port = 0
        self.data_port = 0
        self.public_port = 0
        self.server_config: Optional[ServerConfig] = None

        self._server_task: Optional[asyncio.Task] = None
        self._client_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ #

    async def start(self) -> "TunnelHarness":
        backend_port = await self.backend.start()
        self.control_port, self.data_port, self.public_port = free_ports(3)

        config = ServerConfig(
            name="test-server",
            mapping=[
                MappingRule(
                    public_port=self.public_port,
                    local_port=backend_port,
                    host="127.0.0.1",
                    local_host="127.0.0.1",
                    remark="e2e",
                )
            ],
        )
        config.control.host = "127.0.0.1"
        config.control.port = self.control_port
        config.data.host = "127.0.0.1"
        config.data.port = self.data_port
        config.advertise_host = "127.0.0.1"
        config.timeouts.heartbeat_interval = 5.0
        config.timeouts.pong_timeout = 2.0
        config.timeouts.client_idle_timeout = 30.0
        config.timeouts.watchdog_interval = 0.2
        # pair_timeout 必须明显大于 connect_timeout：否则客户端来不及上报 conn_error，
        # 访客只会看到笼统的"配对超时"，掩盖真实失败原因
        config.timeouts.pair_timeout = 5.0
        config.timeouts.connect_timeout = 1.0
        config.reconnect.initial_delay = 0.05
        config.reconnect.max_delay = 0.2
        config.reconnect.jitter = 0.0
        if self._configure_server is not None:
            self._configure_server(config)
        config.validate()
        self.server_config = config

        self.server = TunnelServer(config)
        await self.server.start()
        self._server_task = asyncio.create_task(self.server.serve_forever(), name="test-server-serve")

        if self._start_client:
            await self.start_client(backend_port)
            # 必须等到注册真正完成，否则第一个请求会撞上"还没有在线客户端"的窗口
            await self.wait_online()
        return self

    def build_client_config(
        self,
        client_id: Optional[str] = None,
        local_ports: Optional[Sequence[int]] = None,
    ) -> ClientConfig:
        """构造一份与本次 harness 对齐的客户端配置。

        冲突测试需要起第二个客户端，复用它可以保证两边配置完全一致，
        差异只体现在 ``client_id`` 与 ``local_ports`` 上。
        """
        config = ClientConfig(
            server_host="127.0.0.1",
            control_port=self.control_port,
            data_port=self.data_port,
            client_id=client_id or self._client_id,
            local_host="127.0.0.1",
            local_ports=list(local_ports) if local_ports else (self._local_ports or [self.backend.port]),
        )
        config.timeouts.heartbeat_interval = 5.0
        config.timeouts.pong_timeout = 2.0
        config.timeouts.pair_timeout = 5.0
        config.timeouts.connect_timeout = 1.0
        config.reconnect.initial_delay = 0.05
        config.reconnect.max_delay = 0.2
        config.reconnect.jitter = 0.0
        if self._configure_client is not None:
            self._configure_client(config)
        config.validate()
        return config

    async def start_client(self, backend_port: Optional[int] = None) -> TunnelClient:
        client_config = self.build_client_config(local_ports=[backend_port] if backend_port else None)
        self._client_id = client_config.client_id
        self.client = TunnelClient(client_config)
        self._client_task = asyncio.create_task(self.client.run(), name="test-client-run")
        return self.client

    async def wait_online(self, *, timeout: float = 5.0, expect_ports: bool = True) -> None:
        """等到服务端真正登记了这个客户端（可选：等到端口也认领成功）。"""
        assert self.server is not None and self.client is not None
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            session = self.server.registry.get(self.client.client_id)
            if session is not None and (not expect_ports or session.local_ports):
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"{timeout}s 内客户端 {self.client.client_id} 未完成注册")

    async def wait_offline(self, *, timeout: float = 5.0) -> None:
        assert self.server is not None and self.client is not None
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.server.registry.get(self.client.client_id) is None:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"{timeout}s 内客户端 {self.client.client_id} 未从服务端下线")

    async def wait_until(self, predicate: Callable[[], bool], *, timeout: float = 5.0, what: str = "条件") -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"{timeout}s 内{what}未满足")

    async def stop_client(self) -> None:
        if self.client is not None:
            await self.client.stop()
        if self._client_task is not None:
            self._client_task.cancel()
            await asyncio.gather(self._client_task, return_exceptions=True)
            self._client_task = None

    async def stop(self) -> None:
        await self.stop_client()
        if self.server is not None:
            await self.server.stop()
        if self._server_task is not None:
            self._server_task.cancel()
            await asyncio.gather(self._server_task, return_exceptions=True)
            self._server_task = None
        await self.backend.stop()

    async def __aenter__(self) -> "TunnelHarness":
        return await self.start()

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.stop()
