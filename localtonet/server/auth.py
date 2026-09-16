# -*- coding: utf-8 -*-
"""
localtonet.server.auth —— 客户端鉴权
======================================
**扩展点**：MVP 里公网服务端默认放开注册（``NoneAuthenticator``），
但鉴权位置已经预留好：客户端在 ``register_client`` 里带上 ``token`` 字段，
服务端在校验点调 ``Authenticator.verify()``。

三个现成实现，由 :func:`build_authenticator` 按配置挑：

============  ==========================  ==========================================================
配置         实现                        能回答的问题
============  ==========================  ==========================================================
``auth.file`` :class:`TokenFileAuthenticator`  "你是**谁**、你能认领哪些内网端口"（令牌表 + 热重载）
``auth.token`` :class:`LegacyTokenAuthenticator` "口令对了吗"（共享令牌，鉴权一期路径，原样保留）
都不给         :class:`NoneAuthenticator`  什么都不问
============  ==========================  ==========================================================

``verify`` 返回 :class:`Identity` 而不是 ``None``：**校验器只回答"你是谁"，
不回答"你能不能认领这个端口"**。端口授权在注册流程里做
（``server/core.py``）——那里才知道本次 ``local_ports`` 是什么，
也才能把未授权的端口整体列出来一次性拒绝。

**唯一的例外是映射表写权限**（``Identity.can_manage_mapping``）：它不依赖任何上下文
（"你能改映射表吗"与本次注册带了什么无关），所以校验器一次答完，
由注册流程快照进 ``ClientSession``；``set_mapping`` 只读会话上那个布尔值，
不再回头查令牌表（那时令牌已被抹掉）。

要换成别的方案（mTLS、签名挑战、一次性票据、对接公司 SSO），
只需要写一个新的 ``Authenticator`` 子类，注册流程一行都不用改。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

from config import AuthConfig
from localtonet.errors import AuthError
from localtonet.server.tokenstore import TokenStore, compare_secret
from logging_setup import get_logger

__all__ = [
    "Identity",
    "Authenticator",
    "NoneAuthenticator",
    "TokenAuthenticator",
    "LegacyTokenAuthenticator",
    "TokenFileAuthenticator",
    "build_authenticator",
]


@dataclass(frozen=True)
class Identity:
    """校验通过后的身份。

    ``ports`` 是**允许认领的内网端口**（``local_ports``），**空＝不限**——
    与 ``limits`` 里 ``0``＝不限一脉相承；要禁止一切认领请用 ``enabled: false`` 吊销令牌。

    ``can_manage_mapping`` 是**能否修改服务端映射表**（``set_mapping``）。
    默认 ``False`` ＝ fail closed：新写的校验器忘了表态，结果是"改不动全局映射表"，
    而不是"悄悄放开"。要放行的实现必须显式写 ``True``。
    """

    name: str
    ports: Tuple[int, ...] = ()
    client_id: str = ""
    can_manage_mapping: bool = False

    def allows(self, local_port: int) -> bool:
        """该身份能否认领某个内网端口。空 ``ports`` ＝不限。"""
        return not self.ports or local_port in self.ports


ANONYMOUS = Identity(name="anonymous", can_manage_mapping=True)
"""匿名身份：不限端口、可改映射表。只在不校验的部署里出现。

写权限给 ``True`` 不是"图省事"：``--no-auth`` 部署里**根本没有身份概念**，
映射表写权限也就无从谈起。把它收成 ``False`` 只会让"关掉鉴权的单机演示"
突然改不动映射表——那是把一个不存在的主体当成受限主体，纯属误伤。"""


class Authenticator(ABC):
    """客户端注册时的身份校验器。"""

    name = "abstract"

    @abstractmethod
    def verify(self, register_msg: Mapping[str, Any], peer: str) -> Identity:
        """校验注册消息。通过则返回身份，失败抛 :class:`AuthError`。"""


class NoneAuthenticator(Authenticator):
    """不校验。仅供本地演示与内网可信环境使用。"""

    name = "none"

    def verify(self, register_msg: Mapping[str, Any], peer: str) -> Identity:
        return ANONYMOUS


class LegacyTokenAuthenticator(Authenticator):
    """共享密钥校验（鉴权一期路径，行为一字未改）。

    用 :func:`~localtonet.server.tokenstore.compare_secret` 做**常量时间**比较——
    普通 ``==`` 会因为提前返回而泄漏 token 前缀信息。

    共享令牌天然分不出身份（所有人都是同一把钥匙），所以身份标签固定为 ``shared``、
    端口范围为空（＝不限）：端口隔离仍然由端口独占与映射表负责。

    写权限同理给 ``True``：一把钥匙分不出"谁"，也就无法按人授权；这里保持鉴权一期的
    行为不变，要按身份收口请换令牌表（``auth.file``）。"""

    name = "token"

    def __init__(self, token: str) -> None:
        if not token:
            raise AuthError("TokenAuthenticator 需要非空 token")
        self._token = token

    def verify(self, register_msg: Mapping[str, Any], peer: str) -> Identity:
        provided = register_msg.get("token")
        if not isinstance(provided, str) or not compare_secret(provided, self._token):
            raise AuthError(f"客户端 {peer} 提供的 token 不合法")
        return Identity(name="shared", can_manage_mapping=True)


TokenAuthenticator = LegacyTokenAuthenticator
"""旧类名别名。鉴权一期时这个类就叫 ``TokenAuthenticator``，别让外部 import 碎掉。"""


class TokenFileAuthenticator(Authenticator):
    """令牌表校验：多令牌、多身份、按内网端口授权、改文件即生效。

    两类失败必须分开（见 :class:`~localtonet.errors.AuthError`）：

    * 令牌不在表里 / 被吊销 / ``client_id`` 冒充 → ``403, retryable=False``，客户端停手；
    * **端口未授权** → 异常不在这里抛（这里还不知道本次申明了哪些端口），
      由注册流程抛 ``403, retryable=True``，客户端继续退避重试。
    """

    def __init__(self, store: TokenStore, *, logger: Optional[logging.Logger] = None) -> None:
        self._store = store
        self._log = logger or get_logger("server.auth")

    @property
    def name(self) -> str:
        """启动日志里那行 ``鉴权：token-file(N 条)`` 用的可读描述。"""
        return f"token-file({self._store.entry_count} 条)"

    @property
    def store(self) -> TokenStore:
        return self._store

    def verify(self, register_msg: Mapping[str, Any], peer: str) -> Identity:
        # 热重载在**校验之前**做：比对文件戳，变了就重载。
        # 所以"改文件 → 最迟在下一次注册尝试生效"，不需要重启，也不需要后台任务。
        # 重载失败时 store 内部保留旧表（绝不降级为放行），这里拿到的仍是有效表。
        self._store.reload_if_changed()

        provided = register_msg.get("token")
        entry = self._store.lookup(provided if isinstance(provided, str) else "")
        if entry is None:
            raise AuthError(f"客户端 {peer} 提供的令牌不在令牌表中")
        if not entry.enabled:
            raise AuthError(f"身份 {entry.name} 的令牌已被吊销（enabled=false）")
        if entry.client_id and register_msg.get("client_id") != entry.client_id:
            raise AuthError(
                f"身份 {entry.name} 只允许 client_id={entry.client_id!r}，"
                f"实际为 {register_msg.get('client_id')!r}"
            )
        return Identity(
            name=entry.name,
            ports=entry.ports,
            client_id=entry.client_id,
            # 写权限**逐条目**：映射表是全局的，而"能不能认领自己的内网端口"
            # 与"能不能改全站映射"是两件事，不能用一个开关糊在一起（见 README 已知限制）
            can_manage_mapping=entry.can_manage_mapping,
        )


def build_authenticator(
    config: Optional[AuthConfig],
    *,
    logger: Optional[logging.Logger] = None,
) -> Authenticator:
    """按配置选择校验器。

    优先级：``auth.file``（令牌表）> ``auth.token``（共享令牌）> 不校验。
    两者互斥由 :meth:`AuthConfig.validate` 保证，所以这里只是"谁先被看见"。

    令牌表损坏 / 缺失时 ``TokenStore.load`` 会抛 :class:`~config.ConfigError`，
    **不静默退回共享令牌、更不放行**——鉴权组件的失败方向必须是"更严"。
    """
    log = logger or get_logger("server.auth")
    if config is None or not config.enabled:
        return NoneAuthenticator()
    if config.file:
        return TokenFileAuthenticator(TokenStore.load(config.file, logger=log), logger=log)
    return LegacyTokenAuthenticator(config.token)
