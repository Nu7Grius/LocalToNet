# -*- coding: utf-8 -*-
"""
localtonet.core.tls —— TLS 上下文的**唯一出处**
================================================
两端所有 ``ssl.SSLContext`` 都在这里构建，别处一律不许自己 ``ssl.create_default_context()``。
理由和 ``core/rules.py``（校验）、``core/limiter.py``（限速）一样：
散落在各处的 TLS 配置迟早会互不一致，而且"客户端与服务端的 Purpose 弄反"这类错误
不会报错、只会让行为整体错位——只有一个出处才守得住。

三条必须记住的事实：

1. **Purpose 不能弄反**：客户端用 ``Purpose.SERVER_AUTH``（我要验服务端），
   服务端用 ``Purpose.CLIENT_AUTH``（我没必要验服务端的主机名）。
   写反了不报错，但"验不验客户端证书"会整体错位。

2. **上下文建一次、全程复用**。本项目的数据通道是"每个请求一条 TCP"，
   若每条连接都新建 context，TLS 1.3 的会话票据（session ticket）缓存会直接丢失，
   每个请求都要付一次完整握手（1~2 RTT + 证书验证）。
   调用方请在 ``__init__`` 里建好存起来，不要在连接路径上现建。

3. **``skip_verify`` 的处理顺序不能反**：``check_hostname`` 为真时把 ``verify_mode``
   设为 ``CERT_NONE``，Python 会直接抛 ``ValueError``。先关主机名，再关校验链。

本模块只负责"建上下文"，不负责建连接——连接点各自在 ``server/core.py``、
``client/core.py``、``client/forwarder.py`` 里。
"""

from __future__ import annotations

import logging
import ssl
from pathlib import Path
from typing import Optional

from config import ClientTlsConfig, ConfigError, ServerTlsConfig
from logging_setup import get_logger

__all__ = [
    "MIN_TLS_VERSION",
    "build_client_context",
    "build_server_context",
    "build_visitor_context",
    "describe_client_tls",
    "describe_server_tls",
    "describe_visitor_tls",
]

MIN_TLS_VERSION = ssl.TLSVersion.TLSv1_2
"""最低协议版本。TLS 1.0/1.1 已被各大浏览器与 OpenSSL 3.x 默认淘汰，没必要给它们留门。"""


def _require_file(path: str, where: str) -> str:
    """确认路径指向真实文件。

    放在这里而不是 ``config.validate()`` 里，是为了让配置层保持"纯解析"、
    不掺文件系统状态；失败仍然抛 ``ConfigError``，
    所以 CLI 侧的"配置错误 → 退出码 2"链路照旧生效。
    """
    if not path:
        raise ConfigError(f"{where} 不能为空")
    file_path = Path(path)
    if not file_path.is_file():
        raise ConfigError(f"{where} 指向的文件不存在：{file_path}")
    return str(file_path)


def _load_cert_chain(context: ssl.SSLContext, certfile: str, keyfile: str, where: str) -> None:
    try:
        context.load_cert_chain(certfile, keyfile)
    except (ssl.SSLError, OSError, ValueError) as exc:
        # 别把原始异常直接扔给用户：最常见的两种情况（证书与私钥不配对、
        # 私钥有口令）靠一句 ssl 的英文报错根本定位不到
        raise ConfigError(f"加载 {where} 失败（cert={certfile}）：{exc}") from exc


def build_server_context(
    cfg: ServerTlsConfig,
    *,
    logger: Optional[logging.Logger] = None,
) -> Optional[ssl.SSLContext]:
    """构建服务端 TLS 上下文；``cfg.enabled`` 为假时返回 ``None``（＝明文）。"""
    if not cfg.enabled:
        return None
    log = logger or get_logger("core.tls")
    certfile = _require_file(cfg.cert, "tls.cert")
    keyfile = _require_file(cfg.key, "tls.key")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = MIN_TLS_VERSION
    # 服务端不校验"服务端证书的主机名"——那件事只属于客户端。
    # 显式写出来是为了让这个事实在代码里可见，而不是藏在默认值里。
    context.check_hostname = False
    _load_cert_chain(context, certfile, keyfile, "服务端证书")

    if cfg.require_client_cert:
        client_ca = _require_file(cfg.client_ca, "tls.client_ca")
        context.verify_mode = ssl.CERT_REQUIRED
        try:
            context.load_verify_locations(cafile=client_ca)
        except (ssl.SSLError, OSError, ValueError) as exc:
            raise ConfigError(f"加载 tls.client_ca 失败（{client_ca}）：{exc}") from exc

    log.info(
        "TLS 已启用（服务端）：最低 %s，客户端证书 %s",
        MIN_TLS_VERSION.name,
        f"必需（CA={cfg.client_ca}）" if cfg.require_client_cert else "不要求",
    )
    return context


def build_client_context(
    cfg: ClientTlsConfig,
    *,
    logger: Optional[logging.Logger] = None,
) -> Optional[ssl.SSLContext]:
    """构建客户端 TLS 上下文；``cfg.enabled`` 为假时返回 ``None``（＝明文）。"""
    if not cfg.enabled:
        return None
    log = logger or get_logger("core.tls")

    cafile = _require_file(cfg.ca, "tls.ca") if cfg.ca else None
    try:
        # Purpose 是 SERVER_AUTH：客户端要验的是**对面那台服务端**。
        # cafile 留空则加载系统信任库，公网证书场景无需额外配置。
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=cafile)
    except (ssl.SSLError, OSError, ValueError) as exc:
        raise ConfigError(f"加载 tls.ca 失败（{cafile}）：{exc}") from exc

    context.minimum_version = MIN_TLS_VERSION

    if cfg.cert or cfg.key:
        certfile = _require_file(cfg.cert, "tls.cert")
        keyfile = _require_file(cfg.key, "tls.key")
        _load_cert_chain(context, certfile, keyfile, "客户端证书")

    if cfg.skip_verify:
        # 顺序是硬约束：先 check_hostname=False，再 verify_mode=CERT_NONE。
        # 反过来 Python 会抛 ValueError: Cannot set verify_mode to CERT_NONE
        # when check_hostname is enabled.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        log.warning(
            "已跳过 TLS 证书校验（tls.skip_verify=true）：不验证书链也不验主机名，"
            "仅用于本地调试，绝不可用于生产"
        )
    elif not cfg.check_hostname:
        # 只放过主机名，证书链照验——"内网 IP 连自签证书"的常见折中
        context.check_hostname = False
        log.warning("已关闭 TLS 主机名校验（tls.check_hostname=false），证书链仍然校验")

    log.info(
        "TLS 已启用（客户端）：最低 %s，信任锚 %s，主机名校验 %s",
        MIN_TLS_VERSION.name,
        cafile or "系统信任库",
        "关" if (cfg.skip_verify or not cfg.check_hostname) else "开",
    )
    return context


def build_visitor_context(
    cfg: ServerTlsConfig,
    *,
    logger: Optional[logging.Logger] = None,
) -> Optional[ssl.SSLContext]:
    """构建**访客端口**的 TLS 上下文；没有配证书时返回 ``None``。

    与 :func:`build_server_context` 的三点关键差异（都是刻意的，别"统一"掉）：

    1. **判据是"证书是否齐备"，不是 ``visitor_enabled``**。
       把"能不能用"（证书）与"要不要用"（端口开关）拆开，才支持
       "全局默认关、个别端口 ``tls: true`` 显式开"——若拿全局开关当判据，
       全局一关就会把显式打开的端口一起废掉，且表现为静默降级成明文。
    2. **只做单向认证**：不设 ``verify_mode``，也不接受访客证书。
       访客是公网上的陌生人，要求他出示证书等于把服务挂掉；
       ``require_client_cert`` / ``client_ca`` 是隧道两端之间的事，跟这里无关。
    3. **不设 ``alpn_protocols``**：一旦协商出 ``h2``，而"客户端 → 内网后端"那一跳
       仍是 HTTP/1.1，隧道并不透传 ALPN，就会出现"浏览器以为在说 h2、
       后端在说 h1"的诡异故障。留空让浏览器退回 HTTP/1.1。
    """
    if not (cfg.visitor_cert and cfg.visitor_key):
        return None
    log = logger or get_logger("core.tls")
    certfile = _require_file(cfg.visitor_cert, "tls.visitor_cert")
    keyfile = _require_file(cfg.visitor_key, "tls.visitor_key")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = MIN_TLS_VERSION
    # 同 build_server_context：服务端不校验"服务端证书的主机名"，显式写出来让它可见
    context.check_hostname = False
    _load_cert_chain(context, certfile, keyfile, "访客端口证书")

    log.info(
        "访客端口 TLS 上下文已就绪：最低 %s，证书 %s（单向认证，不支持 ALPN 协商）",
        MIN_TLS_VERSION.name,
        certfile,
    )
    return context


def describe_server_tls(cfg: ServerTlsConfig) -> str:
    """给启动日志用的一句话描述。"""
    if not cfg.enabled:
        return "明文"
    return f"TLS（双向认证：{'开' if cfg.require_client_cert else '关'}）"


def describe_client_tls(cfg: ClientTlsConfig) -> str:
    """给启动日志用的一句话描述。"""
    if not cfg.enabled:
        return "明文"
    if cfg.skip_verify:
        return "TLS（已跳过校验，仅调试）"
    return f"TLS（校验主机名：{'开' if cfg.check_hostname else '关'}）"


def describe_visitor_tls(cfg: ServerTlsConfig) -> str:
    """访客端口 TLS 的一句话描述，给启动日志用。

    只描述"全局默认 + 能不能用"，因为 per-port 覆盖要逐端口才看得到——
    那件事由 ``MappingManager._listen`` 的逐端口日志负责。
    """
    if not (cfg.visitor_cert and cfg.visitor_key):
        return "访客端口：全部明文（未配置 tls.visitor_cert/visitor_key）"
    return (
        f"访客端口：默认{'TLS' if cfg.visitor_enabled else '明文'}"
        f"（证书 {cfg.visitor_cert}，可用 mapping[].tls 逐端口覆盖）"
    )
