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
    """

    public_port: int
    local_port: int
    host: str = "0.0.0.0"
    local_host: str = "127.0.0.1"
    remark: str = ""

    _FIELDS = ("public_port", "local_port", "host", "local_host", "remark")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "mapping[]") -> "MappingRule":
        _check_unknown(data, cls._FIELDS, where)
        if "public_port" not in data or "local_port" not in data:
            raise ConfigError(f"{where} 必须同时提供 public_port 与 local_port")
        rule = cls(
            public_port=check_port(_as_int(data["public_port"], f"{where}.public_port"), where),
            local_port=check_port(_as_int(data["local_port"], f"{where}.local_port"), where),
            host=_as_str(data.get("host", "0.0.0.0"), f"{where}.host"),
            local_host=_as_str(data.get("local_host", "127.0.0.1"), f"{where}.local_host"),
            remark=_as_str(data.get("remark", ""), f"{where}.remark"),
        )
        return rule

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


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
    """客户端鉴权。

    MVP 里 ``enabled=False``，服务端使用 :class:`NoneAuthenticator` 直接放行。
    ``enabled=True`` 时走 :class:`TokenAuthenticator`，与协议中预留的 ``token`` 字段对接。
    """

    enabled: bool = False
    token: str = ""

    _FIELDS = ("enabled", "token")

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "AuthConfig":
        if data is None:
            return cls()
        _check_unknown(data, cls._FIELDS, "auth")
        return cls(
            enabled=_as_bool(data.get("enabled", False), "auth.enabled"),
            token=_as_str(data.get("token", ""), "auth.token"),
        )

    def validate(self) -> None:
        if self.enabled and not self.token:
            raise ConfigError("auth.enabled 为 true 时必须提供 auth.token")


@dataclass
class LimitsConfig:
    """资源上限，同时也是一层拒绝服务防护。"""

    max_msg_len: int = 10 * 1024 * 1024
    max_clients: int = 64
    max_mappings: int = 32

    _FIELDS = ("max_msg_len", "max_clients", "max_mappings")

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "LimitsConfig":
        if data is None:
            return cls()
        _check_unknown(data, cls._FIELDS, "limits")
        return cls(**{k: _as_int(v, f"limits.{k}") for k, v in data.items()})

    def validate(self) -> None:
        if self.max_msg_len <= 0:
            raise ConfigError("limits.max_msg_len 必须为正数")
        if self.max_clients <= 0:
            raise ConfigError("limits.max_clients 必须为正数")
        if self.max_mappings <= 0:
            raise ConfigError("limits.max_mappings 必须为正数")


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
    timeouts: Timeouts = field(default_factory=Timeouts)
    reconnect: ReconnectPolicy = field(default_factory=ReconnectPolicy)
    auth: AuthConfig = field(default_factory=AuthConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    log: LogConfig = field(default_factory=LogConfig)

    _FIELDS = (
        "name",
        "control",
        "data",
        "advertise_host",
        "mapping",
        "timeouts",
        "reconnect",
        "auth",
        "limits",
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
            timeouts=Timeouts.from_dict(data.get("timeouts")),
            reconnect=ReconnectPolicy.from_dict(data.get("reconnect")),
            auth=AuthConfig.from_dict(data.get("auth")),
            limits=LimitsConfig.from_dict(data.get("limits")),
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
        self.limits.validate()
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
