# -*- coding: utf-8 -*-
"""tests/test_config.py —— 配置解析与校验单测。

重点验证"写错就启动失败"，而不是让错误配置悄悄溜进运行期。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import (
    ClientConfig,
    ConfigError,
    LogConfig,
    MappingRule,
    ServerConfig,
    Timeouts,
)
from localtonet.errors import TunnelError

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# 默认值与合法配置
# --------------------------------------------------------------------------- #


def test_defaults_are_self_consistent() -> None:
    Timeouts().validate()
    assert ServerConfig(mapping=[MappingRule(9028, 8000)]).timeouts.heartbeat_interval == 30.0


def test_example_server_config_file_is_valid() -> None:
    cfg = ServerConfig.from_file(ROOT / "config.json")
    assert cfg.control.port == 7000
    assert cfg.data.port == 7001
    assert cfg.public_ports() == [9028]
    assert cfg.rule_for(9028) is not None
    assert cfg.rule_for(9999) is None


def test_example_client_config_file_is_valid() -> None:
    cfg = ClientConfig.from_file(ROOT / "client.json")
    assert cfg.control_port == 7000
    assert cfg.data_port == 7001
    assert cfg.local_ports == [8000]


def test_unknown_field_causes_failure() -> None:
    data = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    data["control"]["prot"] = 1234  # 打错字
    with pytest.raises(ConfigError, match="未知字段"):
        ServerConfig.from_dict(data)


# --------------------------------------------------------------------------- #
# 校验规则
# --------------------------------------------------------------------------- #


def test_empty_mapping_is_rejected() -> None:
    with pytest.raises(ConfigError, match="mapping 不能为空"):
        ServerConfig(mapping=[]).validate()


def test_control_and_data_port_must_differ() -> None:
    cfg = ServerConfig(mapping=[MappingRule(9028, 8000)])
    cfg.data.port = cfg.control.port
    with pytest.raises(ConfigError, match="同一个端口"):
        cfg.validate()


def test_duplicate_public_port_is_rejected() -> None:
    cfg = ServerConfig(mapping=[MappingRule(9028, 8000), MappingRule(9028, 8001)])
    with pytest.raises(ConfigError, match="重复"):
        cfg.validate()


def test_public_port_colliding_with_control_port_is_rejected() -> None:
    cfg = ServerConfig(mapping=[MappingRule(7000, 8000)])
    with pytest.raises(ConfigError, match="控制/数据端口冲突"):
        cfg.validate()


def test_public_port_out_of_range_is_rejected() -> None:
    with pytest.raises(ConfigError, match="超出合法范围"):
        MappingRule.from_dict({"public_port": 70000, "local_port": 8000})


def test_mapping_requires_both_ports() -> None:
    with pytest.raises(ConfigError, match="public_port 与 local_port"):
        MappingRule.from_dict({"public_port": 9028})


def test_pong_timeout_must_be_smaller_than_heartbeat_interval() -> None:
    with pytest.raises(ConfigError, match="pong_timeout"):
        Timeouts(heartbeat_interval=10.0, pong_timeout=10.0).validate()


def test_idle_timeout_must_exceed_heartbeat_interval() -> None:
    with pytest.raises(ConfigError, match="client_idle_timeout"):
        Timeouts(heartbeat_interval=30.0, client_idle_timeout=30.0).validate()


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ConfigError, match="log.level"):
        LogConfig(level="VERBOSE").validate()


def test_auth_enabled_requires_token() -> None:
    cfg = ServerConfig(mapping=[MappingRule(9028, 8000)])
    cfg.auth.enabled = True
    with pytest.raises(ConfigError, match="auth.token"):
        cfg.validate()


def test_client_rejects_empty_local_ports() -> None:
    with pytest.raises(ConfigError, match="local_ports 不能为空"):
        ClientConfig(local_ports=[]).validate()


def test_client_rejects_duplicate_local_ports() -> None:
    with pytest.raises(ConfigError, match="重复端口"):
        ClientConfig(local_ports=[8000, 8000]).validate()


# --------------------------------------------------------------------------- #
# 加载与覆盖
# --------------------------------------------------------------------------- #


def test_missing_file_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="配置文件不存在"):
        ServerConfig.from_file(ROOT / "no-such-file.json")


def test_invalid_json_raises_config_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="不是合法 JSON"):
        ServerConfig.from_file(broken)


def test_env_overrides_base_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALTONET_CONTROL_PORT", "7100")
    monkeypatch.setenv("LOCALTONET_LOG_LEVEL", "debug")
    monkeypatch.setenv("LOCALTONET_AUTH_TOKEN", "s3cret")

    cfg = ServerConfig.from_env(ServerConfig(mapping=[MappingRule(9028, 8000)]))

    assert cfg.control.port == 7100
    assert cfg.log.level == "DEBUG"
    assert cfg.auth.enabled is True
    assert cfg.auth.token == "s3cret"


def test_env_overrides_client_local_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALTONET_LOCAL_PORTS", "8000,8080 9000")
    monkeypatch.setenv("LOCALTONET_SERVER_HOST", "10.0.0.1")

    cfg = ClientConfig.from_env(ClientConfig(local_ports=[8000]))

    assert cfg.local_ports == [8000, 8080, 9000]
    assert cfg.server_host == "10.0.0.1"


def test_roundtrip_to_dict_stays_loadable() -> None:
    cfg = ServerConfig.from_file(ROOT / "config.json")
    assert ServerConfig.from_dict(cfg.to_dict()).public_ports() == cfg.public_ports()


def test_config_error_is_a_tunnel_error() -> None:
    assert issubclass(ConfigError, TunnelError)
