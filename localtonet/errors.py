# -*- coding: utf-8 -*-
"""localtonet.errors —— 业务异常。

帧层面的错误（非法 JSON、越界长度）属于 :class:`protocol.ProtocolError`；
这里放的是**语义层**错误：端口被占、无客户端在线、后端连不上等。
"""

from __future__ import annotations

from typing import Iterable, Optional

__all__ = [
    "TunnelError",
    "AuthError",
    "PortConflictError",
    "NoClientOnline",
    "NoMappingError",
    "BackendConnectError",
    "QuotaExceededError",
    "RegistrationError",
]


class TunnelError(Exception):
    """本项目所有业务异常的基类。

    携带可选的 ``code``，服务端据此决定回给访客的 HTTP 状态码，
    也便于日志与前端做稳定映射。
    """

    code: int = 500

    def __init__(self, message: str, *, code: Optional[int] = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code

    def __str__(self) -> str:
        return f"[{self.code}] {super().__str__()}"

    @property
    def message(self) -> str:
        """不带 ``code`` 前缀的原始消息。

        回执里的 ``msg`` 已经有独立的 ``code`` 字段，再塞一份前缀就会出现
        ``[403] [403] 客户端提供的 token 不合法`` 这种叠字；日志里同理。
        """
        return Exception.__str__(self)


class AuthError(TunnelError):
    """客户端鉴权失败。"""

    code = 403


class PortConflictError(TunnelError):
    """请求认领的端口已被其他客户端持有。"""

    code = 409

    def __init__(self, conflicts: Iterable[int]) -> None:
        self.conflicts = sorted(conflicts)
        super().__init__(f"端口已被其他客户端占用：{self.conflicts}")


class NoClientOnline(TunnelError):
    """服务端上没有可用的在线客户端。"""

    code = 502


class NoMappingError(TunnelError):
    """访问的访客端口没有配置映射。"""

    code = 404

    def __init__(self, public_port: int) -> None:
        self.public_port = public_port
        super().__init__(f"端口 {public_port} 未配置映射")


class BackendConnectError(TunnelError):
    """客户端连不上内网后端服务。"""

    code = 502

    def __init__(self, host: str, port: int, reason: str = "") -> None:
        self.host = host
        self.port = port
        detail = f"（{reason}）" if reason else ""
        super().__init__(f"无法连接内网后端 {host}:{port}{detail}")


class QuotaExceededError(TunnelError):
    """某个客户端超出了自己的配额（并发数 / 带宽）。

    与 ``503``（服务端整体容量满）**语义不同，不能合并**：

    * ``503`` = "服务端没位置了"，运维该扩容；
    * ``429`` = "你这个客户端跑得太满"，该限流或让客户端收敛。

    混用会让运维看状态码分不清该扩容还是该限人。
    """

    code = 429


class RegistrationError(TunnelError):
    """客户端向服务端注册失败。

    ``code`` 取自服务端 ``register_ack`` 里的 ``code`` 字段。
    ``403`` 视为**永久性失败**（凭据不对），客户端应当停止重试——
    否则会陷入"每 60 秒被拒一次"的无意义循环。

    其余 ``code`` 一律按**暂时性失败**处理（继续退避重试），其中 ``503``（容量满）
    与 ``429``（配额超限）都属于"回头可能就好了"，绝不能与 ``403`` 混同。
    """

    code = 500

    @property
    def is_fatal(self) -> bool:
        return self.code == 403
