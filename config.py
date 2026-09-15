# -*- coding: utf-8 -*-
"""
config.py —— 配置模型与加载
============================
设计要点：

1. **强类型**：配置在启动时一次性解析成 dataclass，运行期不再碰字典字符串键。
   端口写错、类型写反会在 `validate()` 里立刻报错，而不是在业务逻辑深处炸开。
2. **fail fast**：JSON 里出现未知字段直接抛错。宁可启动失败，也不要"配置写错了但被静默忽略"。
3. **可覆盖**：优先级为 默认值 < JSON 文件 < 环境变量 < 命令行参数。
4. **扩展点**：``auth`` / ``limits`` / ``log`` 三段在 MVP 里只有部分被消费，
   但结构已经就位——后续加鉴权、限流、限流阈值时不必改动配置格式。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from localtonet.errors import TunnelError

__all__ = [
    "ConfigError",
    "DEFAULT_SERVER_CONFIG",
    "DEFAULT_CLIENT_CONFIG",
    "Timeouts",
    "ReconnectPolicy",
    "MappingRule",
    "ListenerConfig",
    "AuthConfig",
    "LimitsConfig",
    "MappingStoreConfig",
    "ServerTlsConfig",
    "ClientTlsConfig",
    "LogConfig",
    "ServerConfig",
    "ClientConfig",
]

DEFAULT_SERVER_CONFIG = "config.json"
DEFAULT_CLIENT_CONFIG = "client.json"

_MAX_PORT = 65535


class ConfigError(TunnelError):
    """配置非法。"""

    code = 500


def _as_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where} 必须是整数，实际为 {type(value).__name__}")
    return value


def _as_float(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} 必须是数字，实际为 {type(value).__name__}")
    return float(value)


def _as_str(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{where} 必须是字符串，实际为 {type(value).__name__}")
    return value


def _as_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where} 必须是布尔值，实际为 {type(value).__name__}")
    return value


_ENV_TRUE = ("1", "true", "yes", "on")
_ENV_FALSE = ("0", "false", "no", "off")


def _as_env_bool(value: str, where: str) -> bool:
    """解析环境变量里的布尔值。

    环境变量天生是字符串，``bool("false") is True`` 这种坑必须先在这里拦死，
    否则 ``LOCALTONET_TLS_ENABLED=false`` 会**打开** TLS。
    """
    text = value.strip().lower()
    if text in _ENV_TRUE:
        return True
    if text in _ENV_FALSE:
        return False
    raise ConfigError(f"{where} 只接受 {_ENV_TRUE + _ENV_FALSE} 之一，实际为 {value!r}")


def _check_unknown(data: Mapping[str, Any], allowed: Tuple[str, ...], where: str) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ConfigError(f"{where} 存在未知字段 {unknown}，允许的字段为 {list(allowed)}")


def _require_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where} 必须是对象，实际为 {type(value).__name__}")
    return value


def check_port(port: int, where: str) -> int:
    """端口范围校验，供配置与命令行参数共用。"""
    if not 1 <= port <= _MAX_PORT:
        raise ConfigError(f"{where} 端口 {port} 超出合法范围 1-{_MAX_PORT}")
    return port


# --------------------------------------------------------------------------- #
# 子配置
# --------------------------------------------------------------------------- #


@dataclass
class Timeouts:
    """所有超时集中一处，便于整体调参与测试时压缩时间尺度。

    ⚠️ ``pair_timeout`` 必须**明显大于**客户端侧的 ``connect_timeout``。
    访客连上来后，服务端要等客户端"连内网后端 + 开数据通道"这一整套动作完成；
    如果等的时间比客户端自己尝试连接的时间还短，客户端永远来不及上报
    ``conn_error``，访客就只会看到一句笼统的"配对超时"，
    真正的原因（后端没启动、端口写错）反而被吞掉了。
    """

    heartbeat_interval: float = 30.0   # 客户端发 ping 的间隔
    pong_timeout: float = 10.0         # 客户端等待 pong 的时限，超时判定连接失效
    client_idle_timeout: float = 120.0  # 服务端看门狗判定客户端失联的阈值
    watchdog_interval: float = 10.0    # 看门狗巡检间隔
    pair_timeout: float = 15.0         # 访客连接等待数据通道配对的上限（> 客户端 connect_timeout）
    connect_timeout: float = 5.0       # 建立 TCP 连接（数据通道 / 本机后端）的超时

    _FIELDS = (
        "heartbeat_interval",
        "pong_timeout",
        "client_idle_timeout",
        "watchdog_interval",
        "pair_timeout",
        "connect_timeout",
    )

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "Timeouts":
        if data is None:
            return cls()
        _check_unknown(data, cls._FIELDS, "timeouts")
        kwargs = {k: _as_float(v, f"timeouts.{k}") for k, v in data.items()}
        return cls(**kwargs)

    def validate(self) -> None:
        for name in self._FIELDS:
            if getattr(self, name) <= 0:
                raise ConfigError(f"timeouts.{name} 必须为正数")
        if self.pong_timeout >= self.heartbeat_interval:
            raise ConfigError("timeouts.pong_timeout 应小于 heartbeat_interval，否则心跳永远等不到超时")
        if self.client_idle_timeout <= self.heartbeat_interval:
            raise ConfigError("timeouts.client_idle_timeout 应大于 heartbeat_interval，否则看门狗会误杀健康连接")


@dataclass
class ReconnectPolicy:
    """指数退避参数。序列生成逻辑见 :class:`localtonet.core.backoff.Backoff`。"""

    initial_delay: float = 1.0
    max_delay: float = 60.0
    multiplier: float = 2.0
    jitter: float = 0.0  # 0 表示不加抖动；>0 时为最大随机抖动秒数

    _FIELDS = ("initial_delay", "max_delay", "multiplier", "jitter")

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "ReconnectPolicy":
        if data is None:
            return cls()
        _check_unknown(data, cls._FIELDS, "reconnect")
        kwargs = {k: _as_float(v, f"reconnect.{k}") for k, v in data.items()}
        return cls(**kwargs)

    def validate(self) -> None:
        if self.initial_delay <= 0:
            raise ConfigError("reconnect.initial_delay 必须为正数")
        if self.max_delay < self.initial_delay:
            raise ConfigError("reconnect.max_delay 不能小于 initial_delay")
        if self.multiplier < 1:
            raise ConfigError("reconnect.multiplier 不能小于 1")


@dataclass
class MappingRule:
    """一条端口映射：公网访客端口 -> 内网本地端口。

    ``host`` 是服务端监听访客端口的绑定地址；
    ``local_host`` 是客户端转发时连接内网后端的地址（仅在客户端侧有意义）。

    ``tls`` 是**三态**的访客端口 TLS 开关（本轮扩展）：

    * ``None``（默认）—— 跟随服务端全局默认 ``tls.visitor_enabled``，也是"旧文件缺字段"的解析结果；
    * ``True`` —— 该端口强制 TLS 终止；
    * ``False`` —— 该端口强制明文。

    做成 ``Optional[bool]`` 而不是 ``bool`` 的理由：老 ``mappings.json`` 里没有这个键，
    必须能解析成"没表态"而不是"关"，否则升级即改变行为。
    """

    public_port: int
    local_port: int
    host: str = "0.0.0.0"
    local_host: str = "127.0.0.1"
    remark: str = ""
    tls: Optional[bool] = None
    """``None`` = 跟随服务端 ``tls.visitor_enabled``（默认 false＝明文）。"""

    _FIELDS = ("public_port", "local_port", "host", "local_host", "remark", "tls")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "mapping[]") -> "MappingRule":
        _check_unknown(data, cls._FIELDS, where)
        if "public_port" not in data or "local_port" not in data:
            raise ConfigError(f"{where} 必须同时提供 public_port 与 local_port")
        # 三态解析：只接受 null / true / false。JSON 里写 "tls": "true"（字符串）
        # 是典型的"看着像开了其实没开"，必须 fail fast 而不是悄悄当 truthy 用。
        raw_tls = data.get("tls")
        tls = None if raw_tls is None else _as_bool(raw_tls, f"{where}.tls")
        rule = cls(
            public_port=check_port(_as_int(data["public_port"], f"{where}.public_port"), where),
            local_port=check_port(_as_int(data["local_port"], f"{where}.local_port"), where),
            host=_as_str(data.get("host", "0.0.0.0"), f"{where}.host"),
            local_host=_as_str(data.get("local_host", "127.0.0.1"), f"{where}.local_host"),
            remark=_as_str(data.get("remark", ""), f"{where}.remark"),
            tls=tls,
        )
        return rule

    def to_dict(self) -> Dict[str, Any]:
        """序列化。**必须把 ``tls is None`` 的键摘掉**。

        直接 ``asdict(self)`` 会让每条规则都带 ``"tls": null``，而 ``_check_unknown``
        只拒绝"多出来的键"——于是这些文件一旦拿回旧版本（``beaaf5f``）就会因为多出
        ``tls`` 而**拒绝启动**。"没表态"本来就不该写进文件。
        """
        data = asdict(self)
        if data.get("tls") is None:
            data.pop("tls", None)
        return data


@dataclass
class ListenerConfig:
    """监听地址与端口。"""

    host: str = "0.0.0.0"
    port: int = 7000

    _FIELDS = ("host", "port")

    @classmethod
    def from_dict(
        cls,
        data: Optional[Mapping[str, Any]],
        default_port: int,
        where: str,
    ) -> "ListenerConfig":
        if data is None:
            return cls(port=default_port)
        _check_unknown(data, cls._FIELDS, where)
        return cls(
            host=_as_str(data.get("host", "0.0.0.0"), f"{where}.host"),
            port=check_port(_as_int(data.get("port", default_port), f"{where}.port"), where),
        )


@dataclass
class AuthConfig:
    """客户端鉴权。两种凭据来源，**互斥**：

    * ``token`` —— 单个**共享**令牌（兼容路径）。谁拿到它都能认领任意端口，
      换令牌必须重启；适合本地演示与"整个内网就是一个信任域"的场景。
    * ``file`` —— 令牌表文件（JSON）。每个令牌自带身份与**允许认领的内网端口**，
      改文件最迟在下一次注册尝试时生效，不必重启。

    ``file`` 给出时以**文件为权威**、``token`` 只当首次种子（与 ``mapping_store`` 同一先例）。
    令牌表刻意**不内联进** ``config.json``：令牌是机密，而配置文件常被提交进仓库或贴进文档。
    """

    enabled: bool = False
    token: str = ""
    file: str = ""

    _FIELDS = ("enabled", "token", "file")

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "AuthConfig":
        if data is None:
            return cls()
        _check_unknown(data, cls._FIELDS, "auth")
        return cls(
            enabled=_as_bool(data.get("enabled", False), "auth.enabled"),
            token=_as_str(data.get("token", ""), "auth.token"),
            file=_as_str(data.get("file", ""), "auth.file"),
        )

    def validate(self) -> None:
        # 互斥先查：同时给两者时不该让人猜"到底哪个生效"（哪怕 enabled=false 也 fail fast）
        if self.token and self.file:
            raise ConfigError(
                "auth.token 与 auth.file 互斥：共享令牌与令牌表文件只能二选一，"
                "两者同时给出时无法判断以哪个为准"
            )
        if self.enabled and not (self.token or self.file):
            raise ConfigError("auth.enabled 为 true 时必须提供 auth.token 或 auth.file")


@dataclass
class LimitsConfig:
    """资源上限，同时也是一层拒绝服务防护。

    两类语义必须分开看：

    * ``max_*`` —— **容量**，必须为正；撞上它意味着"服务端该扩容了"（``503``）。
    * ``*_per_client`` / ``per_client_*`` —— **单客户端配额**，``0`` 表示不限；
      撞上它意味着"某个客户端该收敛了"（``429``）。
    """

    max_msg_len: int = 10 * 1024 * 1024
    max_clients: int = 64
    max_mappings: int = 32
    max_conns_per_client: int = 0
    """单个客户端同时进行中的访客转发数上限。0 表示不限。"""
    per_client_upload_bps: int = 0
    """单个客户端的上行带宽上限（字节/秒）。0 表示不限。"""
    per_client_download_bps: int = 0
    """单个客户端的下行带宽上限（字节/秒）。0 表示不限。"""

    _CAPACITY_FIELDS = ("max_msg_len", "max_clients", "max_mappings")
    _QUOTA_FIELDS = ("max_conns_per_client", "per_client_upload_bps", "per_client_download_bps")
    _FIELDS = _CAPACITY_FIELDS + _QUOTA_FIELDS

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "LimitsConfig":
        if data is None:
            return cls()
        _check_unknown(data, cls._FIELDS, "limits")
        return cls(**{k: _as_int(v, f"limits.{k}") for k, v in data.items()})

    def validate(self) -> None:
        for name in self._CAPACITY_FIELDS:
            if getattr(self, name) <= 0:
                raise ConfigError(f"limits.{name} 必须为正数")
        for name in self._QUOTA_FIELDS:
            if getattr(self, name) < 0:
                raise ConfigError(f"limits.{name} 不能为负数（0 表示不限）")


@dataclass
class MappingStoreConfig:
    """映射表存储后端。

    ``memory``（默认）—— 进程重启即回到配置文件里的 mapping。
    ``file`` —— 把映射表持久化到 JSON，**重启后以文件为准**；
    配置文件的 ``mapping`` 退化为"首次种子"，只在文件不存在或内容为空时生效。

    最后这条是"持久化不能白做"的前提：若照旧用配置文件覆盖 store，
    每次重启都会把持久化的内容冲掉，等于没持久化。
    """

    type: str = "memory"
    path: str = ""

    _FIELDS = ("type", "path")
    _TYPES = ("memory", "file")

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "MappingStoreConfig":
        if data is None:
            return cls()
        _check_unknown(data, cls._FIELDS, "mapping_store")
        return cls(
            type=_as_str(data.get("type", "memory"), "mapping_store.type").strip().lower(),
            path=_as_str(data.get("path", ""), "mapping_store.path"),
        )

    def validate(self) -> None:
        if self.type not in self._TYPES:
            raise ConfigError(
                f"mapping_store.type 必须是 {list(self._TYPES)} 之一，实际为 {self.type!r}"
            )
        if self.type == "file" and not self.path.strip():
            raise ConfigError("mapping_store.type 为 file 时必须提供 mapping_store.path")


@dataclass
class ServerTlsConfig:
    """服务端 TLS。**默认完全关闭**——不配就是明文，与 TLS 落地前行为一致。

    为什么两端的 TLS 配置是**两个类**而不是共用一个：两端需要的材料根本不同。
    服务端持有私钥、可能要求客户端出示证书（``require_client_cert`` + ``client_ca``）；
    客户端持有可信 CA、可能持有自己的客户端证书。塞进一个类就得靠"哪些字段在哪端生效"
    的口头约定，而 `_check_unknown` 的 fail fast 会因此失效。

    ``enabled`` 与 ``cert``/``key`` 的关系沿用 ``mapping_store`` 的既有先例：
    **JSON 里要求显式写 ``enabled``**（配置文件讲究所见即所得），
    而命令行与环境变量这两层只要给出证书路径就**隐式开启**——
    覆盖层的存在意义就是"临时改一处"，别让人填了三个路径还漏掉一个开关。
    """

    enabled: bool = False
    cert: str = ""
    """证书链文件路径（PEM，服务端证书在前）。"""
    key: str = ""
    """私钥文件路径（PEM）。"""
    require_client_cert: bool = False
    """是否要求客户端出示证书（双向认证 mTLS）。

    应用层已有共享令牌做身份校验，mTLS 是**纵深防御**而非必需品：
    开启后客户端证书必须由 ``client_ca`` 签发，否则在 TLS 握手阶段就被拒。
    """
    client_ca: str = ""
    """校验客户端证书用的 CA 路径，仅 ``require_client_cert=True`` 时生效。"""
    handshake_timeout: float = 10.0
    """TLS 握手超时秒数。默认 60s 会让"明文客户端打 TLS 端口"这类失败白占连接，
    直接拖慢测试收尾，因此显式收紧。访客端口复用同一个值，不为它单开字段。"""

    visitor_enabled: bool = False
    """**访客端口**（浏览器/curl 打进来的那一跳）TLS 终止的**默认值**，不是总开关。

    语义是"未显式设置 ``MappingRule.tls`` 的端口跟不跟着它"：``False`` 表示默认明文，
    单个端口仍可用 ``"tls": true`` 显式打开；``True`` 表示默认全开，单个端口可用
    ``"tls": false`` 关回去。所以"全局关"**不会**废掉某个端口显式开的开关。
    """
    visitor_cert: str = ""
    """访客端口证书链（PEM）。**独立于 ``cert``、绝不回落**——隧道自签证书够用，
    访客端口是公网入口，证书必须被浏览器信任，两者信任域不同，
    一旦隐式回落就会出现"以为配了公网证书、实则自签、浏览器报红查不出原因"。"""
    visitor_key: str = ""
    """访客端口私钥（PEM）。与 ``visitor_cert`` 成对提供。"""

    _FIELDS = (
        "enabled",
        "cert",
        "key",
        "require_client_cert",
        "client_ca",
        "handshake_timeout",
        "visitor_enabled",
        "visitor_cert",
        "visitor_key",
    )

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "ServerTlsConfig":
        if data is None:
            return cls()
        _check_unknown(data, cls._FIELDS, "tls")
        return cls(
            enabled=_as_bool(data.get("enabled", False), "tls.enabled"),
            cert=_as_str(data.get("cert", ""), "tls.cert"),
            key=_as_str(data.get("key", ""), "tls.key"),
            require_client_cert=_as_bool(data.get("require_client_cert", False), "tls.require_client_cert"),
            client_ca=_as_str(data.get("client_ca", ""), "tls.client_ca"),
            handshake_timeout=_as_float(data.get("handshake_timeout", 10.0), "tls.handshake_timeout"),
            visitor_enabled=_as_bool(data.get("visitor_enabled", False), "tls.visitor_enabled"),
            visitor_cert=_as_str(data.get("visitor_cert", ""), "tls.visitor_cert"),
            visitor_key=_as_str(data.get("visitor_key", ""), "tls.visitor_key"),
        )

    def validate(self) -> None:
        if self.handshake_timeout <= 0:
            raise ConfigError("tls.handshake_timeout 必须为正数")

        # 访客端口 TLS 与 ``enabled``（控制/数据两跳）是各自独立的开关，
        # 因此这几条校验必须放在 ``enabled`` 的早退**之前**——否则
        # "控制通道明文 + 访客端口 TLS"这个完全合法的组合会被整体跳过校验。
        if bool(self.visitor_cert) != bool(self.visitor_key):
            raise ConfigError("tls.visitor_cert 与 tls.visitor_key 必须成对提供（访客端口 TLS 用）")
        if self.visitor_enabled and not (self.visitor_cert and self.visitor_key):
            raise ConfigError(
                "tls.visitor_enabled 为 true 时必须同时提供 tls.visitor_cert 与 tls.visitor_key，"
                "否则默认开的端口会静默变成明文"
            )
        # 注意：visitor_enabled=False 但给了证书 —— **不报错**。
        # 那正是"默认关、个别端口显式开"的用法（见 MappingRule.tls 三态）。

        if not self.enabled:
            return
        if not self.cert or not self.key:
            raise ConfigError("tls.enabled 为 true 时必须同时提供 tls.cert 与 tls.key")
        if self.require_client_cert and not self.client_ca:
            raise ConfigError(
                "tls.require_client_cert 为 true 时必须提供 tls.client_ca，否则无法校验客户端证书"
            )
        if self.client_ca and not self.require_client_cert:
            raise ConfigError(
                "提供了 tls.client_ca 但 tls.require_client_cert 为 false——"
                "要么打开双向认证，要么删掉 client_ca，别让它静默失效"
            )


@dataclass
class ClientTlsConfig:
    """客户端 TLS。**默认完全关闭**——不配就是明文。

    ``ca`` 留空表示使用**系统信任库**（适合目标服务端持有公网证书的场景）；
    自签 CA 必须显式给出 ``ca``，否则校验会失败——这是刻意的，不做静默降级。
    """

    enabled: bool = False
    ca: str = ""
    """可信 CA 文件路径；留空则使用系统信任库。"""
    cert: str = ""
    """客户端证书路径，仅服务端开启双向认证时需要。"""
    key: str = ""
    """客户端私钥路径。"""
    check_hostname: bool = True
    """是否校验服务端证书里的主机名。关掉它仍然校验证书链，只放过主机名。"""
    skip_verify: bool = False
    """**调试专用**：完全跳过证书链与主机名校验，**优先级高于 ``check_hostname``**。

    开启后会记一条 WARNING。默认关，且必须显式配置才生效——
    绝不允许"证书校验失败就自动降级到这里"。
    """

    _FIELDS = ("enabled", "ca", "cert", "key", "check_hostname", "skip_verify")

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "ClientTlsConfig":
        if data is None:
            return cls()
        _check_unknown(data, cls._FIELDS, "tls")
        return cls(
            enabled=_as_bool(data.get("enabled", False), "tls.enabled"),
            ca=_as_str(data.get("ca", ""), "tls.ca"),
            cert=_as_str(data.get("cert", ""), "tls.cert"),
            key=_as_str(data.get("key", ""), "tls.key"),
            check_hostname=_as_bool(data.get("check_hostname", True), "tls.check_hostname"),
            skip_verify=_as_bool(data.get("skip_verify", False), "tls.skip_verify"),
        )

    def validate(self) -> None:
        if bool(self.cert) != bool(self.key):
            raise ConfigError("tls.cert 与 tls.key 必须成对提供（服务端要求双向认证时用）")


@dataclass
class LogConfig:
    level: str = "INFO"
    file: str = ""

    _FIELDS = ("level", "file")

    _LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "LogConfig":
        if data is None:
            return cls()
        _check_unknown(data, cls._FIELDS, "log")
        return cls(
            level=_as_str(data.get("level", "INFO"), "log.level").upper(),
            file=_as_str(data.get("file", ""), "log.file"),
        )

    def validate(self) -> None:
        if self.level not in self._LEVELS:
            raise ConfigError(f"log.level 必须是 {list(self._LEVELS)} 之一，实际为 {self.level!r}")


# --------------------------------------------------------------------------- #
# 服务端配置
# --------------------------------------------------------------------------- #


@dataclass
class ServerConfig:
    name: str = "localtonet"
    control: ListenerConfig = field(default_factory=lambda: ListenerConfig(port=7000))
    data: ListenerConfig = field(default_factory=lambda: ListenerConfig(port=7001))
    advertise_host: str = ""
    """告知客户端用于回连数据通道的地址。留空则复用客户端连入时看到的本地地址，
    这样同一份配置既能跑 127.0.0.1 本地演示，也能跑公网部署，不必改配置。"""

    mapping: List[MappingRule] = field(default_factory=list)
    mapping_store: MappingStoreConfig = field(default_factory=MappingStoreConfig)
    """映射表的存储后端。``type=file`` 时以持久化文件为准，``mapping`` 仅作首次种子。"""
    timeouts: Timeouts = field(default_factory=Timeouts)
    reconnect: ReconnectPolicy = field(default_factory=ReconnectPolicy)
    auth: AuthConfig = field(default_factory=AuthConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    tls: ServerTlsConfig = field(default_factory=ServerTlsConfig)
    """控制通道与数据通道的传输加密。默认关闭＝明文，与 TLS 落地前完全一致。"""
    log: LogConfig = field(default_factory=LogConfig)

    _FIELDS = (
        "name",
        "control",
        "data",
        "advertise_host",
        "mapping",
        "mapping_store",
        "timeouts",
        "reconnect",
        "auth",
        "limits",
        "tls",
        "log",
    )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ServerConfig":
        data = _require_mapping(data, "根配置")
        _check_unknown(data, cls._FIELDS, "根配置")

        raw_mapping = data.get("mapping", [])
        if not isinstance(raw_mapping, list):
            raise ConfigError("mapping 必须是数组")

        cfg = cls(
            name=_as_str(data.get("name", "localtonet"), "name"),
            control=ListenerConfig.from_dict(data.get("control"), 7000, "control"),
            data=ListenerConfig.from_dict(data.get("data"), 7001, "data"),
            advertise_host=_as_str(data.get("advertise_host", ""), "advertise_host"),
            mapping=[
                MappingRule.from_dict(_require_mapping(item, f"mapping[{i}]"), f"mapping[{i}]")
                for i, item in enumerate(raw_mapping)
            ],
            mapping_store=MappingStoreConfig.from_dict(data.get("mapping_store")),
            timeouts=Timeouts.from_dict(data.get("timeouts")),
            reconnect=ReconnectPolicy.from_dict(data.get("reconnect")),
            auth=AuthConfig.from_dict(data.get("auth")),
            limits=LimitsConfig.from_dict(data.get("limits")),
            tls=ServerTlsConfig.from_dict(data.get("tls")),
            log=LogConfig.from_dict(data.get("log")),
        )
        cfg.validate()
        return cfg

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> "ServerConfig":
        return cls.from_dict(_load_json(path))

    @classmethod
    def from_env(cls, base: Optional["ServerConfig"] = None) -> "ServerConfig":
        """在已有配置上叠加环境变量覆盖。"""
        cfg = base or cls()
        env = _env_map()
        if "control_port" in env:
            cfg.control.port = check_port(int(env["control_port"]), "LOCALTONET_CONTROL_PORT")
        if "control_host" in env:
            cfg.control.host = env["control_host"]
        if "data_port" in env:
            cfg.data.port = check_port(int(env["data_port"]), "LOCALTONET_DATA_PORT")
        if "data_host" in env:
            cfg.data.host = env["data_host"]
        if "advertise_host" in env:
            cfg.advertise_host = env["advertise_host"]
        if "auth_token" in env:
            cfg.auth.token = env["auth_token"]
            cfg.auth.enabled = True
        # 令牌表文件：同样"给出即隐式开启"（与 --mapping-store-path 隐式切 file 同理）。
        # 与 LOCALTONET_AUTH_TOKEN 同时给出会在下面的 validate() 里被互斥校验拦下。
        if "auth_file" in env:
            cfg.auth.file = env["auth_file"]
            cfg.auth.enabled = True
        if "mapping_store" in env:
            cfg.mapping_store.type = env["mapping_store"].strip().lower()
        if "mapping_store_path" in env:
            cfg.mapping_store.path = env["mapping_store_path"]
        # TLS：给出证书路径即隐式开启，避免"路径都填了却漏了开关"（与 --mapping-store-path 同理）
        if "tls_cert" in env:
            cfg.tls.cert = env["tls_cert"]
            cfg.tls.enabled = True
        if "tls_key" in env:
            cfg.tls.key = env["tls_key"]
            cfg.tls.enabled = True
        if "tls_client_ca" in env:
            cfg.tls.client_ca = env["tls_client_ca"]
        if "tls_require_client_cert" in env:
            cfg.tls.require_client_cert = _as_env_bool(
                env["tls_require_client_cert"], "LOCALTONET_TLS_REQUIRE_CLIENT_CERT"
            )
        if "tls_enabled" in env:
            cfg.tls.enabled = _as_env_bool(env["tls_enabled"], "LOCALTONET_TLS_ENABLED")
        if "tls_handshake_timeout" in env:
            cfg.tls.handshake_timeout = _as_float(
                env["tls_handshake_timeout"], "LOCALTONET_TLS_HANDSHAKE_TIMEOUT"
            )
        # 访客端口 TLS：同样"给出证书路径即隐式开启"，但它们只影响全局默认
        # （``visitor_enabled``），不碰控制/数据两跳的 ``tls.enabled``。
        if "tls_visitor_cert" in env:
            cfg.tls.visitor_cert = env["tls_visitor_cert"]
            cfg.tls.visitor_enabled = True
        if "tls_visitor_key" in env:
            cfg.tls.visitor_key = env["tls_visitor_key"]
            cfg.tls.visitor_enabled = True
        if "tls_visitor_enabled" in env:
            cfg.tls.visitor_enabled = _as_env_bool(
                env["tls_visitor_enabled"], "LOCALTONET_TLS_VISITOR_ENABLED"
            )
        if "log_level" in env:
            cfg.log.level = env["log_level"].upper()
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.control.port == self.data.port:
            raise ConfigError("control 与 data 不能使用同一个端口")
        if not self.mapping:
            raise ConfigError("mapping 不能为空，至少需要一条端口映射")
        if len(self.mapping) > self.limits.max_mappings:
            raise ConfigError(
                f"mapping 条目数 {len(self.mapping)} 超过 limits.max_mappings={self.limits.max_mappings}"
            )

        reserved = {self.control.port, self.data.port}
        seen: Dict[int, int] = {}
        for i, rule in enumerate(self.mapping):
            if rule.public_port in seen:
                raise ConfigError(
                    f"mapping[{i}].public_port={rule.public_port} 与 mapping[{seen[rule.public_port]}] 重复"
                )
            if rule.public_port in reserved:
                raise ConfigError(f"mapping[{i}].public_port={rule.public_port} 与控制/数据端口冲突")
            seen[rule.public_port] = i

        self.timeouts.validate()
        self.reconnect.validate()
        self.auth.validate()
        self.mapping_store.validate()
        self.limits.validate()
        self.tls.validate()
        self.log.validate()

    def public_ports(self) -> List[int]:
        return [rule.public_port for rule in self.mapping]

    def rule_for(self, public_port: int) -> Optional[MappingRule]:
        for rule in self.mapping:
            if rule.public_port == public_port:
                return rule
        return None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# 客户端配置
# --------------------------------------------------------------------------- #


@dataclass
class ClientConfig:
    server_host: str = "127.0.0.1"
    control_port: int = 7000
    data_port: int = 7001
    client_id: str = ""
    """留空则由客户端生成稳定标识（``主机名-随机后缀``）。"""
    local_host: str = "127.0.0.1"
    local_ports: List[int] = field(default_factory=list)
    """要认领的本地端口列表。服务端会据此把对应访客端口的流量派发给本客户端。"""

    timeouts: Timeouts = field(default_factory=Timeouts)
    reconnect: ReconnectPolicy = field(default_factory=ReconnectPolicy)
    auth_token: str = ""
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    tls: ClientTlsConfig = field(default_factory=ClientTlsConfig)
    """控制通道与数据通道的传输加密。默认关闭＝明文。"""
    log: LogConfig = field(default_factory=LogConfig)

    _FIELDS = (
        "server_host",
        "control_port",
        "data_port",
        "client_id",
        "local_host",
        "local_ports",
        "timeouts",
        "reconnect",
        "auth_token",
        "limits",
        "tls",
        "log",
    )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ClientConfig":
        data = _require_mapping(data, "根配置")
        _check_unknown(data, cls._FIELDS, "根配置")

        raw_ports = data.get("local_ports", [])
        if not isinstance(raw_ports, list):
            raise ConfigError("local_ports 必须是数组")

        cfg = cls(
            server_host=_as_str(data.get("server_host", "127.0.0.1"), "server_host"),
            control_port=check_port(_as_int(data.get("control_port", 7000), "control_port"), "control_port"),
            data_port=check_port(_as_int(data.get("data_port", 7001), "data_port"), "data_port"),
            client_id=_as_str(data.get("client_id", ""), "client_id"),
            local_host=_as_str(data.get("local_host", "127.0.0.1"), "local_host"),
            local_ports=[
                check_port(_as_int(p, f"local_ports[{i}]"), f"local_ports[{i}]")
                for i, p in enumerate(raw_ports)
            ],
            timeouts=Timeouts.from_dict(data.get("timeouts")),
            reconnect=ReconnectPolicy.from_dict(data.get("reconnect")),
            auth_token=_as_str(data.get("auth_token", ""), "auth_token"),
            limits=LimitsConfig.from_dict(data.get("limits")),
            tls=ClientTlsConfig.from_dict(data.get("tls")),
            log=LogConfig.from_dict(data.get("log")),
        )
        cfg.validate()
        return cfg

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> "ClientConfig":
        return cls.from_dict(_load_json(path))

    @classmethod
    def from_env(cls, base: Optional["ClientConfig"] = None) -> "ClientConfig":
        cfg = base or cls()
        env = _env_map()
        if "server_host" in env:
            cfg.server_host = env["server_host"]
        if "server_port" in env:
            cfg.control_port = check_port(int(env["server_port"]), "LOCALTONET_SERVER_PORT")
        if "data_port" in env:
            cfg.data_port = check_port(int(env["data_port"]), "LOCALTONET_DATA_PORT")
        if "client_id" in env:
            cfg.client_id = env["client_id"]
        if "local_ports" in env:
            cfg.local_ports = [
                check_port(int(p), "LOCALTONET_LOCAL_PORTS")
                for p in env["local_ports"].replace(",", " ").split()
            ]
        if "auth_token" in env:
            cfg.auth_token = env["auth_token"]
        # TLS：给出 ca / 客户端证书即隐式开启，与 --mapping-store-path 隐式切 file 同理
        if "tls_ca" in env:
            cfg.tls.ca = env["tls_ca"]
            cfg.tls.enabled = True
        if "tls_cert" in env:
            cfg.tls.cert = env["tls_cert"]
            cfg.tls.enabled = True
        if "tls_key" in env:
            cfg.tls.key = env["tls_key"]
            cfg.tls.enabled = True
        if "tls_enabled" in env:
            cfg.tls.enabled = _as_env_bool(env["tls_enabled"], "LOCALTONET_TLS_ENABLED")
        if "tls_check_hostname" in env:
            cfg.tls.check_hostname = _as_env_bool(env["tls_check_hostname"], "LOCALTONET_TLS_CHECK_HOSTNAME")
        if "tls_skip_verify" in env:
            cfg.tls.skip_verify = _as_env_bool(env["tls_skip_verify"], "LOCALTONET_TLS_SKIP_VERIFY")
        if "log_level" in env:
            cfg.log.level = env["log_level"].upper()
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.server_host:
            raise ConfigError("server_host 不能为空")
        if self.control_port == self.data_port:
            raise ConfigError("control_port 与 data_port 不能相同")
        if not self.local_ports:
            raise ConfigError("local_ports 不能为空，至少需要认领一个端口")
        if len(set(self.local_ports)) != len(self.local_ports):
            raise ConfigError("local_ports 存在重复端口")
        if len(self.local_ports) > self.limits.max_mappings:
            raise ConfigError(
                f"local_ports 数量 {len(self.local_ports)} 超过 limits.max_mappings={self.limits.max_mappings}"
            )
        self.timeouts.validate()
        self.reconnect.validate()
        self.limits.validate()
        self.tls.validate()
        self.log.validate()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# 加载辅助
# --------------------------------------------------------------------------- #


def _load_json(path: str | os.PathLike[str]) -> Dict[str, Any]:
    file_path = Path(path)
    if not file_path.is_file():
        raise ConfigError(f"配置文件不存在：{file_path}")
    try:
        raw = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {file_path}：{exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件 {file_path} 不是合法 JSON：{exc}") from exc
    return dict(_require_mapping(data, str(file_path)))


def _env_map() -> Dict[str, str]:
    """收集 ``LOCALTONET_`` 前缀的环境变量，键统一转小写。"""
    prefix = "LOCALTONET_"
    result: Dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith(prefix) and value != "":
            result[key[len(prefix):].lower()] = value
    return result
