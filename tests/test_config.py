# -*- coding: utf-8 -*-
"""tests/test_config.py —— 配置解析与校验单测。

重点验证"写错就启动失败"，而不是让错误配置悄悄溜进运行期。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import (
    AuthConfig,
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
from localtonet.core.limiter import (
    DEFAULT_MAX_KEYS,
    RATE_LIMIT_SCOPES,
    ClientRateLimiter,
    compose_limit_key,
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


# --------------------------------------------------------------------------- #
# auth.file：令牌表（鉴权二期）
# --------------------------------------------------------------------------- #


def test_auth_file_defaults_to_empty() -> None:
    """默认必须是空串——不是 None。空＝"没配令牌表"，走鉴权一期路径。"""
    assert AuthConfig().file == ""
    assert ServerConfig(mapping=[MappingRule(9028, 8000)]).auth.file == ""


def test_auth_file_comes_from_json(tmp_path: Path) -> None:
    payload = {
        "mapping": [{"public_port": 9028, "local_port": 8000}],
        "auth": {"enabled": True, "file": "tokens.json"},
    }
    path = tmp_path / "server.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    cfg = ServerConfig.from_file(path)

    assert cfg.auth.enabled is True
    assert cfg.auth.file == "tokens.json"
    assert cfg.auth.token == ""


def test_env_auth_file_implies_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """给出令牌表文件即隐式开启鉴权（与 --mapping-store-path 隐式切 file 同理）。"""
    monkeypatch.setenv("LOCALTONET_AUTH_FILE", "tokens.json")

    cfg = ServerConfig.from_env(ServerConfig(mapping=[MappingRule(9028, 8000)]))

    assert cfg.auth.enabled is True
    assert cfg.auth.file == "tokens.json"


def test_env_auth_file_and_token_are_mutually_exclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量层同时给两者也必须 fail fast，不许让运维猜哪个生效。"""
    monkeypatch.setenv("LOCALTONET_AUTH_FILE", "tokens.json")
    monkeypatch.setenv("LOCALTONET_AUTH_TOKEN", "s3cret")

    with pytest.raises(ConfigError, match="互斥"):
        ServerConfig.from_env(ServerConfig(mapping=[MappingRule(9028, 8000)]))


def test_auth_token_and_file_are_mutually_exclusive() -> None:
    """一个凭据来源：共享令牌与令牌表文件不能并存。"""

    def payload(**auth: object) -> dict:
        return {"mapping": [{"public_port": 9028, "local_port": 8000}], "auth": auth}

    with pytest.raises(ConfigError, match="互斥"):
        ServerConfig.from_dict(payload(enabled=True, token="x", file="tokens.json"))

    # 哪怕 enabled=false 也照报：留着两份凭据本身就是隐患
    with pytest.raises(ConfigError, match="互斥"):
        ServerConfig.from_dict(payload(enabled=False, token="x", file="tokens.json"))


def test_auth_unknown_field_is_rejected() -> None:
    with pytest.raises(ConfigError, match="未知字段"):
        ServerConfig.from_dict(
            {"mapping": [{"public_port": 9028, "local_port": 8000}], "auth": {"files": "t.json"}}
        )


def test_auth_file_survives_roundtrip() -> None:
    cfg = ServerConfig.from_dict(
        {
            "mapping": [{"public_port": 9028, "local_port": 8000}],
            "auth": {"enabled": True, "file": "tokens.json"},
        }
    )

    again = ServerConfig.from_dict(cfg.to_dict())

    assert again.auth.file == "tokens.json"
    assert again.auth.enabled is True


# --------------------------------------------------------------------------- #
# auth.shared_can_manage_mapping：共享令牌模式下"能不能改映射表"的开关
# --------------------------------------------------------------------------- #
#
# 默认必须是 **True**（＝保持鉴权一期以来的行为）：升级不该悄悄改掉任何现有部署的能力。
# 反向的错法（默认 False）会让"只有一把钥匙"的部署在升级后突然改不动映射表，
# 而且症状是 403 + "去令牌表加字段"——那条建议在共享令牌部署里根本不存在。


def test_shared_mapping_write_defaults_to_open() -> None:
    """省略该字段＝保持现状：共享令牌持有者仍可改映射表（与 ``Identity("shared")`` 一致）。"""
    assert AuthConfig().shared_can_manage_mapping is True
    assert AuthConfig.from_dict(None).shared_can_manage_mapping is True
    assert AuthConfig.from_dict({"enabled": True, "token": "x"}).shared_can_manage_mapping is True
    # 整个 ServerConfig 走一遍，证明字段真的接进了 from_dict（而不是只在 dataclass 上躺着）
    cfg = ServerConfig(mapping=[MappingRule(9028, 8000)])
    assert cfg.auth.shared_can_manage_mapping is True


def test_shared_mapping_write_can_be_disabled_from_json() -> None:
    """显式 ``false`` 收紧共享令牌的写权限——这是本轮唯一能关掉那条路的手段。"""
    cfg = ServerConfig.from_dict(
        {
            "mapping": [{"public_port": 9028, "local_port": 8000}],
            "auth": {"enabled": True, "token": "s3cret", "shared_can_manage_mapping": False},
        }
    )

    assert cfg.auth.shared_can_manage_mapping is False
    # 开关必须能活着穿过序列化（否则"存盘一次就悄悄变回允许"）
    assert ServerConfig.from_dict(cfg.to_dict()).auth.shared_can_manage_mapping is False


def test_env_shared_mapping_write_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量是字符串，``"false"`` 必须真能关掉，而不是被当成真值放行。"""
    monkeypatch.setenv("LOCALTONET_AUTH_TOKEN", "s3cret")
    monkeypatch.setenv("LOCALTONET_AUTH_SHARED_CAN_MANAGE_MAPPING", "false")

    cfg = ServerConfig.from_env(ServerConfig(mapping=[MappingRule(9028, 8000)]))

    assert cfg.auth.token == "s3cret"
    assert cfg.auth.shared_can_manage_mapping is False
    # 反过来也要认（"0"/"off" 之类由 _as_env_bool 统一处理）
    monkeypatch.setenv("LOCALTONET_AUTH_SHARED_CAN_MANAGE_MAPPING", "1")
    assert ServerConfig.from_env(
        ServerConfig(mapping=[MappingRule(9028, 8000)])
    ).auth.shared_can_manage_mapping is True


def test_bad_shared_mapping_write_value_is_rejected() -> None:
    """``"false"`` / ``0`` / ``[]`` 这类"看着像关了"的写法必须报错，不做隐式转换。

    权限字段最怕"我以为关了，其实没关"——静默解释等于把安全开关做成摆设。
    """
    for value in ("true", 0, 1, [], None):
        with pytest.raises(ConfigError, match="shared_can_manage_mapping"):
            AuthConfig.from_dict({"enabled": True, "token": "x", "shared_can_manage_mapping": value})


def test_shared_mapping_write_typo_is_rejected() -> None:
    """写错一个词也要 fail fast，不能悄悄退回默认（那会静默保持"允许"）。"""
    with pytest.raises(ConfigError, match="未知字段"):
        ServerConfig.from_dict(
            {
                "mapping": [{"public_port": 9028, "local_port": 8000}],
                "auth": {"enabled": True, "token": "x", "shared_mapping_write": False},
            }
        )


def test_bad_env_shared_mapping_write_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALTONET_AUTH_SHARED_CAN_MANAGE_MAPPING", "maybe")

    with pytest.raises(ConfigError, match="LOCALTONET_AUTH_SHARED_CAN_MANAGE_MAPPING"):
        ServerConfig.from_env(ServerConfig(mapping=[MappingRule(9028, 8000)]))


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


@pytest.mark.parametrize("field", ("max_msg_len", "max_clients", "max_mappings", "rate_limit_max_keys"))
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
# limits：限速汇总口径（六期）
# --------------------------------------------------------------------------- #


def test_rate_limit_scope_defaults_to_client_scope() -> None:
    """兼容性红线：不写这个键就是历史行为（按客户端一份总额度）。

    默认值翻成 ``port``/``visitor`` 会让升级后的部署在同一份配置下把总带宽上限
    放大成"端口数/访客数 × 额度"——绝不能让升级自己改行为。
    """
    limits = LimitsConfig()
    assert limits.rate_limit_scope == "client"
    limits.validate()
    # 端口/访客分桶必须显式配置
    assert LimitsConfig.from_dict({"rate_limit_scope": "port"}).rate_limit_scope == "port"


def test_rate_limit_scope_rejects_unknown_value() -> None:
    with pytest.raises(ConfigError, match="rate_limit_scope"):
        LimitsConfig.from_dict({"rate_limit_scope": "sideways"}).validate()


def test_rate_limit_scope_must_be_str() -> None:
    with pytest.raises(ConfigError, match="limits.rate_limit_scope 必须是字符串"):
        LimitsConfig.from_dict({"rate_limit_scope": 1})


def test_rate_limit_max_keys_is_a_capacity_and_must_be_positive() -> None:
    with pytest.raises(ConfigError, match="limits.rate_limit_max_keys 必须为正数"):
        LimitsConfig.from_dict({"rate_limit_max_keys": 0}).validate()


def test_every_advertised_scope_is_accepted_by_the_limiter() -> None:
    """抗分歧钉子：配置侧的可选值表与限速器实现必须一一对应。

    校验用的是 ``RATE_LIMIT_SCOPES``，key 怎么编在 ``ClientRateLimiter`` 里；
    两边哪天只剩一边被改动（比如新增一种口径却忘了实现），这条用例先红。
    """
    for scope in RATE_LIMIT_SCOPES:
        limits = LimitsConfig(rate_limit_scope=scope)
        limits.validate()
        limiter = ClientRateLimiter(upload_bps=1024, scope=scope)
        assert limiter.scope == scope
        # 该口径能否编出 key（缺分片参数时必须炸，说明它真的按那个口径在编）
        if scope == "client":
            assert limiter.key_for("client-a") == compose_limit_key(scope, "client-a")
        elif scope == "port":
            assert limiter.key_for("client-a", public_port=80) == compose_limit_key(
                scope, "client-a", public_port=80
            )
        else:
            assert limiter.key_for("client-a", visitor_host="203.0.113.7") == compose_limit_key(
                scope, "client-a", visitor_host="203.0.113.7"
            )


def test_limits_max_keys_default_matches_the_limiter_default() -> None:
    """同一个默认值只允许有一处定义（``limiter.DEFAULT_MAX_KEYS``）。"""
    assert LimitsConfig().rate_limit_max_keys == DEFAULT_MAX_KEYS


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
