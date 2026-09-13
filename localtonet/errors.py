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


class RegistrationError(TunnelError):
    """客户端向服务端注册失败。

    ``code`` 取自服务端 ``register_ack`` 里的 ``code`` 字段。
    ``403`` 视为**永久性失败**（凭据不对），客户端应当停止重试——
    否则会陷入"每 60 秒被拒一次"的无意义循环。
    """

    code = 500

    @property
    def is_fatal(self) -> bool:
        return self.code == 403
