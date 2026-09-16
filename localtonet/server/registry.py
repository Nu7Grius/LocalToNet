# -*- coding: utf-8 -*-
"""
localtonet.server.registry —— 在线客户端表与端口归属路由
=========================================================
这是整个服务端最核心的一小块状态，解决两个问题：

1. **谁在线**：``client_id -> ClientSession``。
2. **请求该派给谁**：``port_owner: {本地端口: client_id}``。

端口独占规则（教程 4.2）：一个客户端认领某个本地端口后，
访客端口上的请求**只**会派给它的持有者，避免两个客户端互相抢流量。
客户端断开时其持有的端口自动释放，其他客户端即可重新认领。

**并发约定**：本类不做任何加锁。所有读写都发生在同一个 asyncio 事件循环内，
且方法体内部不含 ``await``（不主动让出控制权），因此天然是原子的。
如果将来把服务端拆成多线程/多进程，需要在这里补锁——所以方法都刻意写成短小的临界区。

**扩展点**：``pick_client`` 是路由策略的注入点。默认是"归属优先，无归属退回第一个在线"，
将来要做加权轮询、按标签路由、灰度分流，传一个 ``routing`` 回调进来即可。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

import logging

from logging_setup import get_logger

__all__ = ["ClientSession", "ClientRegistry", "RoutingFn"]

RoutingFn = Callable[["ClientRegistry", Optional[int]], Optional["ClientSession"]]


@dataclass
class ClientSession:
    """一条在线客户端（控制通道）会话。"""

    client_id: str
    reader: asyncio.StreamReader = field(repr=False)
    writer: asyncio.StreamWriter = field(repr=False)
    peer: str = ""
    identity: str = ""
    """令牌表里的身份标签（``anonymous`` / ``shared`` / 令牌条目的 ``name``）。

    注意这是**标签**不是凭据——令牌明文绝不进会话（MEMORY 不变量 1）。
    """
    local_ports: Set[int] = field(default_factory=set)
    connected_at: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    register_msg: Dict[str, object] = field(default_factory=dict)

    def touch(self) -> None:
        """收到任意消息时刷新活跃时间。"""
        self.last_seen = time.monotonic()

    def idle_for(self, now: Optional[float] = None) -> float:
        return (now if now is not None else time.monotonic()) - self.last_seen

    def snapshot(self) -> Dict[str, object]:
        """给日志 / GUI / 未来的管理 API 用的只读快照。"""
        return {
            "client_id": self.client_id,
            "peer": self.peer,
            "identity": self.identity,
            "local_ports": sorted(self.local_ports),
            "online_seconds": round(time.monotonic() - self.connected_at, 1),
            "idle_seconds": round(self.idle_for(), 1),
        }


class ClientRegistry:
    """在线客户端与端口归属的登记表。"""

    def __init__(
        self,
        *,
        routing: Optional[RoutingFn] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._clients: Dict[str, ClientSession] = {}
        self._port_owner: Dict[int, str] = {}
        self._routing = routing
        self._log = logger or get_logger("server.registry")

    # ------------------------------------------------------------------ #
    # 在线客户端
    # ------------------------------------------------------------------ #

    def add(self, session: ClientSession) -> Optional[ClientSession]:
        """登记会话。

        若同 ``client_id`` 已有会话（常见于客户端重连但旧连接还没被内核回收），
        返回旧会话由调用方关闭——即"新连接顶掉旧连接"，避免同一身份出现两条控制通道。
        """
        previous = self._clients.get(session.client_id)
        if previous is not None:
            self._flush_ports(session.client_id)
        self._clients[session.client_id] = session
        return previous

    def get(self, client_id: str) -> Optional[ClientSession]:
        return self._clients.get(client_id)

    def has(self, client_id: str) -> bool:
        return client_id in self._clients

    def remove(self, client_id: str) -> Optional[ClientSession]:
        """移除会话并**同时释放它持有的所有端口**。"""
        session = self._clients.pop(client_id, None)
        if session is None:
            return None
        released = self._flush_ports(client_id)
        if released:
            self._log.info("客户端 %s 下线，释放端口归属 %s", client_id, released)
        return session

    def sessions(self) -> List[ClientSession]:
        return list(self._clients.values())

    def online_ids(self) -> List[str]:
        return list(self._clients.keys())

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # ------------------------------------------------------------------ #
    # 端口归属
    # ------------------------------------------------------------------ #

    def claim_ports(self, client_id: str, ports: List[int]) -> Tuple[List[int], List[int]]:
        """认领本地端口。

        返回 ``(认领成功, 冲突被拒)``。已属于**自己**的端口视为成功（幂等重认领）。
        """
        claimed: List[int] = []
        conflicts: List[int] = []
        for port in ports:
            owner = self._port_owner.get(port)
            if owner is not None and owner != client_id:
                conflicts.append(port)
                continue
            self._port_owner[port] = client_id
            claimed.append(port)

        session = self._clients.get(client_id)
        if session is not None:
            session.local_ports = set(claimed)

        if conflicts:
            self._log.warning("客户端 %s 认领端口冲突，被拒：%s", client_id, conflicts)
        return claimed, conflicts

    def release_ports(self, client_id: str) -> List[int]:
        """显式释放某个客户端持有的端口。"""
        return self._flush_ports(client_id)

    def owner_of(self, port: int) -> Optional[str]:
        return self._port_owner.get(port)

    def port_owner_snapshot(self) -> Dict[int, str]:
        return dict(self._port_owner)

    def _flush_ports(self, client_id: str) -> List[int]:
        released = [port for port, owner in self._port_owner.items() if owner == client_id]
        for port in released:
            del self._port_owner[port]
        return sorted(released)

    # ------------------------------------------------------------------ #
    # 路由与巡检
    # ------------------------------------------------------------------ #

    def pick_client(self, local_port: Optional[int] = None) -> Optional[ClientSession]:
        """为一次访客请求挑选接收方客户端。

        默认策略：端口的归属者优先；归属者不在线（或端口无人认领）时退回第一个在线客户端。
        单客户端场景下等价于"只有它"，多客户端场景下保证端口隔离。
        """
        if self._routing is not None:
            return self._routing(self, local_port)

        if local_port is not None:
            owner = self._port_owner.get(local_port)
            if owner is not None:
                session = self._clients.get(owner)
                if session is not None:
                    return session

        return next(iter(self._clients.values()), None)

    def collect_expired(self, idle_timeout: float) -> List[ClientSession]:
        """收集失联超时的会话（供看门狗使用）。"""
        now = time.monotonic()
        return [session for session in self._clients.values() if session.idle_for(now) > idle_timeout]

    def snapshot(self) -> List[Dict[str, object]]:
        return [session.snapshot() for session in self._clients.values()]

    def __len__(self) -> int:
        return len(self._clients)

    def __contains__(self, client_id: object) -> bool:
        return client_id in self._clients

    def __repr__(self) -> str:
        return f"ClientRegistry(clients={len(self._clients)}, ports={len(self._port_owner)})"
