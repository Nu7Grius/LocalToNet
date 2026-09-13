# -*- coding: utf-8 -*-
"""
localtonet.server.auth —— 客户端鉴权
======================================
**扩展点**：MVP 里公网服务端默认放开注册（``NoneAuthenticator``），
但鉴权位置已经预留好：客户端在 ``register_client`` 里带上 ``token`` 字段，
服务端在校验点调 ``Authenticator.verify()``。

要换成别的方案（mTLS、签名挑战、一次性票据、对接公司 SSO），
只需要写一个新的 ``Authenticator`` 子类，注册流程一行都不用改。
"""

from __future__ import annotations

import hmac
from abc import ABC, abstractmethod
from typing import Any, Dict, Mapping, Optional

from config import AuthConfig
from localtonet.errors import AuthError

__all__ = ["Authenticator", "NoneAuthenticator", "TokenAuthenticator", "build_authenticator"]


class Authenticator(ABC):
    """客户端注册时的身份校验器。"""

    name = "abstract"

    @abstractmethod
    def verify(self, register_msg: Mapping[str, Any], peer: str) -> None:
        """校验注册消息。通过则正常返回，失败抛 :class:`AuthError`。"""


class NoneAuthenticator(Authenticator):
    """不校验。仅供本地演示与内网可信环境使用。"""

    name = "none"

    def verify(self, register_msg: Mapping[str, Any], peer: str) -> None:
        return None


class TokenAuthenticator(Authenticator):
    """共享密钥校验。

    用 :func:`hmac.compare_digest` 做**常量时间**比较——
    普通 ``==`` 会因为提前返回而泄漏 token 前缀信息。
    """

    name = "token"

    def __init__(self, token: str) -> None:
        if not token:
            raise AuthError("TokenAuthenticator 需要非空 token")
        self._token = token

    def verify(self, register_msg: Mapping[str, Any], peer: str) -> None:
        provided = register_msg.get("token")
        if not isinstance(provided, str) or not hmac.compare_digest(provided, self._token):
            raise AuthError(f"客户端 {peer} 提供的 token 不合法")


def build_authenticator(config: Optional[AuthConfig]) -> Authenticator:
    """按配置选择校验器。"""
    if config is None or not config.enabled:
        return NoneAuthenticator()
    return TokenAuthenticator(config.token)
