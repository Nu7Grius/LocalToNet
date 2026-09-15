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
    """客户端鉴权失败（HTTP 语义上的 ``403``）。

    ``retryable`` 把 ``403`` 细分成两类，默认 ``False`` ＝**永久失败**，
    与鉴权一期的语义一字不差（老代码 ``except AuthError`` 不用改）：

    * ``retryable=False`` —— 令牌无效 / 被吊销 / ``client_id`` 冒充。重试一万次结果一样。
    * ``retryable=True`` —— **端口未授权**。改完服务端的令牌表，同一个客户端进程
      下一轮退避重试就能自动上车（热重载闭环的落点）。

    ⚠️ 不要整类改成可重试，也不要拿 ``409`` 表达"未授权"
    （``409`` 已被"端口被别的客户端占了"占用，两者语义不同）。
    """

    code = 403

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


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
    ``403`` 是**永久性失败**（凭据不对 / 被吊销 / 冒名），客户端应当停止重试——
    否则会陷入"每 60 秒被拒一次"的无意义循环。

    但 ``403`` 分两半：服务端可以在回执里给 ``retryable=true`` 表示
    "你这个人是对的，只是这个端口没授权"——**这类 403 必须继续重试**，
    因为运维改完令牌表后，同一个客户端进程应当自动上车（端口授权的热重载闭环）。
    判据收在 :attr:`is_fatal` 里，调用方只判它，不要自己比对 ``code``。

    ``retryable`` 缺省为 ``None`` ＝**老服务端没这个字段**：按鉴权一期语义推导
    （``403`` 永久、其余暂时），保证新客户端与老服务端互通。
    """

    code = 500

    def __init__(
        self,
        message: str,
        *,
        code: Optional[int] = None,
        retryable: Optional[bool] = None,
    ) -> None:
        super().__init__(message, code=code)
        self.retryable = retryable

    @property
    def is_fatal(self) -> bool:
        if self.retryable is not None:
            return self.code == 403 and not self.retryable
        return self.code == 403
