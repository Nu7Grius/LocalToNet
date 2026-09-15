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
    ClientTlsConfig,
    ConfigError,
    LimitsConfig,
    LogConfig,
    MappingRule,
    MappingStoreConfig,
    ServerConfig,
    ServerTlsConfig,
    Timeouts,
)
from localtonet.errors import TunnelError

ROOT = Path(__file__).resolve().parent.parent

_QUOTA_FIELDS = (
    "max_conns_per_client",
    "per_client_upload_bps",
    "per_client_download_bps",
)


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


# --------------------------------------------------------------------------- #
# limits：容量（>0）与配额（>=0）是两类字段
# --------------------------------------------------------------------------- #


def test_quota_fields_default_to_unlimited() -> None:
    limits = LimitsConfig()
    for name in _QUOTA_FIELDS:
        assert getattr(limits, name) == 0, f"{name} 默认必须是不限（0）"
    limits.validate()


@pytest.mark.parametrize("field", _QUOTA_FIELDS)
def test_quota_field_accepts_zero_but_rejects_negative(field: str) -> None:
    LimitsConfig(**{field: 0}).validate()

    with pytest.raises(ConfigError, match=field):
        LimitsConfig(**{field: -1}).validate()


@pytest.mark.parametrize("field", ("max_msg_len", "max_clients", "max_mappings"))
def test_capacity_field_rejects_zero(field: str) -> None:
    """容量字段与配额字段语义不同：容量为 0 等于"什么都收不了"，必须报错。"""
    with pytest.raises(ConfigError, match="必须为正数"):
        LimitsConfig(**{field: 0}).validate()


def test_quota_field_must_be_int() -> None:
    with pytest.raises(ConfigError, match="limits.per_client_upload_bps 必须是整数"):
        LimitsConfig.from_dict({"per_client_upload_bps": "64k"})


def test_unknown_limit_field_is_rejected() -> None:
    with pytest.raises(ConfigError, match="未知字段"):
        LimitsConfig.from_dict({"per_client_bandwidth": 1024})


def test_limits_block_is_loaded_from_server_config() -> None:
    data = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    data["limits"] = {"max_clients": 4, "max_conns_per_client": 8, "per_client_upload_bps": 4096}

    cfg = ServerConfig.from_dict(data)

    assert cfg.limits.max_conns_per_client == 8
    assert cfg.limits.per_client_upload_bps == 4096


# --------------------------------------------------------------------------- #
# mapping_store
# --------------------------------------------------------------------------- #


def test_mapping_store_defaults_to_memory() -> None:
    assert ServerConfig(mapping=[MappingRule(9028, 8000)]).mapping_store.type == "memory"


def test_mapping_store_file_requires_path() -> None:
    with pytest.raises(ConfigError, match="mapping_store.path"):
        MappingStoreConfig(type="file").validate()


def test_mapping_store_rejects_unknown_type() -> None:
    with pytest.raises(ConfigError, match="mapping_store.type"):
        MappingStoreConfig(type="sqlite").validate()


def test_mapping_store_type_is_normalized() -> None:
    assert MappingStoreConfig.from_dict({"type": "  FILE  ", "path": "m.json"}).type == "file"


def test_mapping_store_unknown_field_is_rejected() -> None:
    with pytest.raises(ConfigError, match="未知字段"):
        MappingStoreConfig.from_dict({"type": "file", "path": "m.json", "flush": True})


def test_mapping_store_from_server_config_dict() -> None:
    data = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    data["mapping_store"] = {"type": "file", "path": "data/mappings.json"}

    cfg = ServerConfig.from_dict(data)

    assert cfg.mapping_store.type == "file"
    assert cfg.mapping_store.path == "data/mappings.json"


def test_mapping_store_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALTONET_MAPPING_STORE", "FILE")
    monkeypatch.setenv("LOCALTONET_MAPPING_STORE_PATH", "from-env.json")

    cfg = ServerConfig.from_env(ServerConfig(mapping=[MappingRule(9028, 8000)]))

    assert cfg.mapping_store.type == "file"
    assert cfg.mapping_store.path == "from-env.json"


def test_demo_config_keeps_memory_store() -> None:
    """演示配置不能被悄悄切成持久化——那会改变"改配置就生效"的既有直觉。"""
    cfg = ServerConfig.from_file(ROOT / "config.json")
    assert cfg.mapping_store.type == "memory"


# --------------------------------------------------------------------------- #
# TLS 配置
# --------------------------------------------------------------------------- #


def test_tls_defaults_to_disabled() -> None:
    assert ServerTlsConfig().enabled is False
    assert ClientTlsConfig().enabled is False


def test_server_tls_requires_cert_and_key_when_enabled() -> None:
    with pytest.raises(ConfigError, match="cert"):
        ServerTlsConfig(enabled=True).validate()


def test_server_tls_mtls_requires_client_ca() -> None:
    with pytest.raises(ConfigError, match="client_ca"):
        ServerTlsConfig(enabled=True, cert="c.pem", key="k.pem", require_client_cert=True).validate()


def test_server_tls_client_ca_without_mtls_is_rejected() -> None:
    """给了 client_ca 却没开双向认证——要么打开，要么删掉，别让它静默失效。"""
    with pytest.raises(ConfigError, match="require_client_cert"):
        ServerTlsConfig(enabled=True, cert="c.pem", key="k.pem", client_ca="ca.pem").validate()


def test_client_tls_cert_key_must_be_paired() -> None:
    with pytest.raises(ConfigError, match="成对"):
        ClientTlsConfig(enabled=True, ca="ca.pem", cert="c.pem").validate()


def test_client_tls_skip_verify_requires_explicit_enabled() -> None:
    """skip_verify 是逃生门：默认关，且必须显式 enabled 才进入 TLS 路径。"""
    # 单独给 skip_verify 但没 enabled，仍是明文（不创建上下文）
    assert ClientTlsConfig(skip_verify=True).enabled is False


def test_tls_unknown_field_is_rejected() -> None:
    with pytest.raises(ConfigError, match="未知字段"):
        ServerTlsConfig.from_dict({"enabled": True, "cert": "c.pem", "key": "k.pem", "verify": False})


def test_server_config_roundtrip_keeps_tls() -> None:
    cfg = ServerConfig.from_file(ROOT / "config.json")
    cfg.tls = ServerTlsConfig(enabled=True, cert="c.pem", key="k.pem")
    assert ServerConfig.from_dict(cfg.to_dict()).tls.enabled is True


def test_demo_config_keeps_tls_disabled() -> None:
    """演示配置不能悄悄开 TLS——那会让本地 demo 突然要证书。"""
    cfg = ServerConfig.from_file(ROOT / "config.json")
    assert cfg.tls.enabled is False


# --------------------------------------------------------------------------- #
# 访客端口 TLS（per-port 开关，默认明文）
# --------------------------------------------------------------------------- #


def test_visitor_tls_defaults_to_plaintext() -> None:
    """三件套默认值：不开、无证书。且"关着又给了证书"是合法的（per-port 用法）。"""
    cfg = ServerTlsConfig()

    assert cfg.visitor_enabled is False
    assert cfg.visitor_cert == ""
    assert cfg.visitor_key == ""
    cfg.validate()  # 什么都不配 → 合法

    # 给了证书但没把全局默认打开：这是"默认明文、个别端口显式开"的正规用法，不能报错
    ServerTlsConfig(visitor_cert="vc.pem", visitor_key="vk.pem").validate()


def test_visitor_cert_and_key_must_be_paired_at_config_layer() -> None:
    """证书/私钥必须成对——只给一个就是"配了一半"，必须启动失败而不是静默明文。"""
    with pytest.raises(ConfigError, match="成对"):
        ServerTlsConfig(visitor_cert="vc.pem").validate()
    with pytest.raises(ConfigError, match="成对"):
        ServerTlsConfig(visitor_key="vk.pem").validate()
    with pytest.raises(ConfigError, match="visitor_enabled"):
        ServerTlsConfig(visitor_enabled=True).validate()


def test_visitor_tls_env_applies_and_does_not_touch_control_data_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """环境变量层：给出证书即隐式打开**访客默认**，但绝不碰 ``tls.enabled``。"""
    monkeypatch.setenv("LOCALTONET_TLS_VISITOR_CERT", "vc.pem")
    monkeypatch.setenv("LOCALTONET_TLS_VISITOR_KEY", "vk.pem")
    cfg = ServerConfig.from_env(ServerConfig(mapping=[MappingRule(9028, 8000)]))

    assert cfg.tls.visitor_cert == "vc.pem"
    assert cfg.tls.visitor_key == "vk.pem"
    assert cfg.tls.visitor_enabled is True
    assert cfg.tls.enabled is False, "访客端口 TLS 与控制/数据两跳是两个独立开关"

    # bool("false") is True 这个坑必须拦死
    monkeypatch.setenv("LOCALTONET_TLS_VISITOR_ENABLED", "false")
    cfg2 = ServerConfig.from_env(ServerConfig(mapping=[MappingRule(9028, 8000)]))
    assert cfg2.tls.visitor_enabled is False


def test_visitor_tls_unknown_field_is_rejected() -> None:
    with pytest.raises(ConfigError, match="未知字段"):
        ServerTlsConfig.from_dict({"visitor_ca": "ca.pem"})


def test_mapping_rule_tls_three_state_parsing() -> None:
    """``tls`` 只接受 null / true / false；字符串 "true" 必须 fail fast。"""
    where = {"public_port": 9028, "local_port": 8000}

    assert MappingRule.from_dict(dict(where)).tls is None, "缺字段＝没表态"
    assert MappingRule.from_dict(dict(where, tls=None)).tls is None
    assert MappingRule.from_dict(dict(where, tls=True)).tls is True
    assert MappingRule.from_dict(dict(where, tls=False)).tls is False

    with pytest.raises(ConfigError, match="布尔"):
        MappingRule.from_dict(dict(where, tls="true"))


def test_mapping_rule_roundtrip_omits_unspecified_tls() -> None:
    """``tls=None`` 不落盘、显式值原样往返——回滚安全的根据就在这条。"""
    plain = MappingRule(9028, 8000)
    assert "tls" not in plain.to_dict()
    assert MappingRule.from_dict(plain.to_dict()).tls is None

    explicit = MappingRule(9028, 8000, tls=True)
    assert explicit.to_dict()["tls"] is True
    assert MappingRule.from_dict(explicit.to_dict()).tls is True

    off = MappingRule(9028, 8000, tls=False)
    assert off.to_dict()["tls"] is False
    assert MappingRule.from_dict(off.to_dict()).tls is False


def test_server_config_roundtrip_keeps_visitor_tls() -> None:
    cfg = ServerConfig.from_file(ROOT / "config.json")
    cfg.tls = ServerTlsConfig(visitor_enabled=True, visitor_cert="vc.pem", visitor_key="vk.pem")

    restored = ServerConfig.from_dict(cfg.to_dict())

    assert restored.tls.visitor_enabled is True
    assert restored.tls.visitor_cert == "vc.pem"
    assert restored.tls.visitor_key == "vk.pem"
