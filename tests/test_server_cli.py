# -*- coding: utf-8 -*-
"""
tests/test_server_cli.py —— 服务端命令行参数
==============================================
只测**配置层**：解析参数 → 覆盖配置 → 校验，全程不监听端口、不进事件循环。

重点守两条约定：

1. 配置优先级铁律：默认值 < JSON 文件 < 环境变量 < 命令行参数。
   服务端与客户端必须对称——客户端早就有 ``--token``，服务端不能缺。
2. 演示配置 ``config.json`` 的 ``auth.enabled`` 必须保持 ``false``，
   否则本机 demo 会突然要令牌。这条由最后一条用例钉死。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from server import build_parser, load_config, main

REPO_ROOT = Path(__file__).resolve().parent.parent

# 这些环境变量会干扰配置叠加，逐个清掉保证用例之间互不污染
_ENV_KEYS = (
    "LOCALTONET_CONTROL_PORT",
    "LOCALTONET_CONTROL_HOST",
    "LOCALTONET_DATA_PORT",
    "LOCALTONET_DATA_HOST",
    "LOCALTONET_ADVERTISE_HOST",
    "LOCALTONET_AUTH_TOKEN",
    "LOCALTONET_AUTH_FILE",
    "LOCALTONET_MAPPING_STORE",
    "LOCALTONET_MAPPING_STORE_PATH",
    "LOCALTONET_TLS_CERT",
    "LOCALTONET_TLS_KEY",
    "LOCALTONET_TLS_CLIENT_CA",
    "LOCALTONET_TLS_REQUIRE_CLIENT_CERT",
    "LOCALTONET_TLS_ENABLED",
    "LOCALTONET_TLS_VISITOR_ENABLED",
    "LOCALTONET_TLS_VISITOR_CERT",
    "LOCALTONET_TLS_VISITOR_KEY",
    "LOCALTONET_LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def write_server_config(tmp_path: Path, *, enabled: bool = False, token: str = "") -> Path:
    """写一份最小可用的服务端配置。"""
    payload: Dict[str, Any] = {
        "name": "cli-test",
        "control": {"host": "127.0.0.1", "port": 7000},
        "data": {"host": "127.0.0.1", "port": 7001},
        "mapping": [{"public_port": 9028, "local_port": 8000}],
        "auth": {"enabled": enabled, "token": token},
    }
    path = tmp_path / "server.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def parse(argv: list[str]):
    return build_parser().parse_args(argv)


# --------------------------------------------------------------------------- #
# 优先级
# --------------------------------------------------------------------------- #


def test_cli_token_turns_auth_on(tmp_path: Path) -> None:
    path = write_server_config(tmp_path)
    config = load_config(parse(["-c", str(path), "--token", "s3cret"]))

    assert config.auth.enabled is True
    assert config.auth.token == "s3cret"


def test_cli_token_overrides_json_token(tmp_path: Path) -> None:
    path = write_server_config(tmp_path, enabled=True, token="old-token")
    config = load_config(parse(["-c", str(path), "--token", "new-token"]))

    assert config.auth.enabled is True
    assert config.auth.token == "new-token"


def test_cli_without_token_keeps_json_auth(tmp_path: Path) -> None:
    """不给命令行参数时，JSON 里的鉴权设置必须原样保留。"""
    path = write_server_config(tmp_path, enabled=True, token="json-secret")
    config = load_config(parse(["-c", str(path)]))

    assert config.auth.enabled is True
    assert config.auth.token == "json-secret"


def test_cli_no_auth_overrides_env_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--no-auth`` 是本地演示的逃生门：环境变量开着鉴权也能关掉，且令牌一并清空。"""
    monkeypatch.setenv("LOCALTONET_AUTH_TOKEN", "from-env")
    path = write_server_config(tmp_path)

    config = load_config(parse(["-c", str(path), "--no-auth"]))

    assert config.auth.enabled is False
    assert config.auth.token == ""


# --------------------------------------------------------------------------- #
# 非法输入
# --------------------------------------------------------------------------- #


def test_blank_token_exits_with_code_2_and_readable_message(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """空令牌不能变成"enabled 但没 token"这种含混内部错误。"""
    path = write_server_config(tmp_path)
    code = main(["-c", str(path), "--token", ""])

    assert code == 2
    err = capsys.readouterr().err
    assert "--token" in err
    assert "不能为空" in err


def test_token_and_no_auth_are_mutually_exclusive(tmp_path: Path) -> None:
    path = write_server_config(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        parse(["-c", str(path), "--token", "x", "--no-auth"])

    assert excinfo.value.code == 2


# --------------------------------------------------------------------------- #
# 令牌表（--auth-file，鉴权二期）
# --------------------------------------------------------------------------- #


def test_cli_auth_file_turns_auth_on(tmp_path: Path) -> None:
    """给出令牌表路径即隐式开启鉴权（与 --mapping-store-path 隐式切 file 同理）。"""
    path = write_server_config(tmp_path)
    config = load_config(parse(["-c", str(path), "--auth-file", "tokens.json"]))

    assert config.auth.enabled is True
    assert config.auth.file == "tokens.json"
    assert config.auth.token == ""


def test_cli_auth_file_defaults_to_none(tmp_path: Path) -> None:
    """三态：不给时必须是 None，否则区分不出"没给"和"给了空串"。"""
    path = write_server_config(tmp_path)
    args = parse(["-c", str(path)])
    assert args.auth_file is None

    # 没给时 JSON/环境变量里的设置原样保留
    config = load_config(parse(["-c", str(path), "--token", "from-cli"]))
    assert config.auth.file == ""


def test_cli_no_auth_clears_both_credential_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """逃生门要把**两种**凭据都清掉：只清一半会留下"关了鉴权却还挂着令牌表"的半截状态。"""
    monkeypatch.setenv("LOCALTONET_AUTH_FILE", "from-env.json")
    path = write_server_config(tmp_path)

    config = load_config(parse(["-c", str(path), "--no-auth"]))

    assert config.auth.enabled is False
    assert config.auth.token == ""
    assert config.auth.file == ""


def test_cli_file_wins_over_json_token(tmp_path: Path) -> None:
    """命令行优先级最高：显式切到令牌表就把 JSON 里的共享令牌清掉。

    否则"JSON 配了 token、命令行给了 file"会撞上互斥校验，而用户做的
    恰恰是文档里写的"命令行覆盖 JSON"。
    """
    path = write_server_config(tmp_path, enabled=True, token="json-secret")
    config = load_config(parse(["-c", str(path), "--auth-file", "tokens.json"]))

    assert config.auth.file == "tokens.json"
    assert config.auth.token == ""
    assert config.auth.enabled is True


def test_cli_token_wins_over_json_file(tmp_path: Path) -> None:
    payload: Dict[str, Any] = {
        "name": "cli-test",
        "control": {"host": "127.0.0.1", "port": 7000},
        "data": {"host": "127.0.0.1", "port": 7001},
        "mapping": [{"public_port": 9028, "local_port": 8000}],
        "auth": {"enabled": True, "file": "json-tokens.json"},
    }
    path = tmp_path / "server.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    config = load_config(parse(["-c", str(path), "--token", "cli-secret"]))

    assert config.auth.token == "cli-secret"
    assert config.auth.file == ""
    assert config.auth.enabled is True


def test_cli_blank_auth_file_exits_with_code_2(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = write_server_config(tmp_path)
    code = main(["-c", str(path), "--auth-file", "  "])

    assert code == 2
    err = capsys.readouterr().err
    assert "--auth-file" in err
    assert "不能为空" in err


def test_cli_auth_file_and_token_are_mutually_exclusive(tmp_path: Path) -> None:
    """同一个互斥组里：既开令牌表又开共享令牌，argparse 必须当场拦下。"""
    path = write_server_config(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        parse(["-c", str(path), "--auth-file", "t.json", "--token", "x"])

    assert excinfo.value.code == 2


def test_cli_auth_file_and_no_auth_are_mutually_exclusive(tmp_path: Path) -> None:
    path = write_server_config(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        parse(["-c", str(path), "--auth-file", "t.json", "--no-auth"])

    assert excinfo.value.code == 2


# --------------------------------------------------------------------------- #
# 演示配置不受影响
# --------------------------------------------------------------------------- #


def test_demo_config_keeps_auth_disabled() -> None:
    """钉死本地 demo：仓库自带的 config.json 不能悄悄要求令牌。"""
    config = load_config(parse(["-c", str(REPO_ROOT / "config.json")]))

    assert config.auth.enabled is False
    assert config.auth.token == ""


# --------------------------------------------------------------------------- #
# 映射持久化参数
# --------------------------------------------------------------------------- #


def test_default_stays_in_memory(tmp_path: Path) -> None:
    path = write_server_config(tmp_path)
    config = load_config(parse(["-c", str(path)]))

    assert config.mapping_store.type == "memory"
    assert config.mapping_store.path == ""


def test_cli_mapping_store_selects_file_backend(tmp_path: Path) -> None:
    path = write_server_config(tmp_path)
    config = load_config(
        parse(["-c", str(path), "--mapping-store", "file", "--mapping-store-path", "state/m.json"])
    )

    assert config.mapping_store.type == "file"
    assert config.mapping_store.path == "state/m.json"


def test_cli_mapping_store_path_implies_file_mode(tmp_path: Path) -> None:
    """只给路径时自动切到 file——"给了持久化路径却还留在 memory 模式"最容易踩空。"""
    path = write_server_config(tmp_path)
    config = load_config(parse(["-c", str(path), "--mapping-store-path", "state/m.json"]))

    assert config.mapping_store.type == "file"


def test_cli_mapping_store_overrides_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALTONET_MAPPING_STORE", "file")
    monkeypatch.setenv("LOCALTONET_MAPPING_STORE_PATH", "from-env.json")
    path = write_server_config(tmp_path)

    config = load_config(parse(["-c", str(path), "--mapping-store", "memory"]))

    # 命令行优先级最高：type 被改回 memory，path 仍保留环境变量的值（不冲突）
    assert config.mapping_store.type == "memory"
    assert config.mapping_store.path == "from-env.json"


def test_cli_mapping_store_rejects_unknown_backend(tmp_path: Path) -> None:
    path = write_server_config(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        parse(["-c", str(path), "--mapping-store", "sqlite"])

    assert excinfo.value.code == 2


def test_file_mode_without_path_exits_with_code_2(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """``--mapping-store file`` 却不给路径 → 走配置错误退出码，而不是启动到一半炸掉。"""
    path = write_server_config(tmp_path)

    code = main(["-c", str(path), "--mapping-store", "file"])

    assert code == 2
    assert "mapping_store.path" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# TLS
# --------------------------------------------------------------------------- #


def test_cli_tls_cert_and_key_turn_tls_on(tmp_path: Path) -> None:
    path = write_server_config(tmp_path)
    config = load_config(parse(["-c", str(path), "--tls-cert", "c.pem", "--tls-key", "k.pem"]))

    assert config.tls.enabled is True
    assert config.tls.cert == "c.pem"
    assert config.tls.key == "k.pem"


def test_cli_tls_client_ca_implies_mtls(tmp_path: Path) -> None:
    path = write_server_config(tmp_path)
    config = load_config(
        parse(["-c", str(path), "--tls-cert", "c.pem", "--tls-key", "k.pem", "--tls-client-ca", "ca.pem"])
    )

    assert config.tls.require_client_cert is True
    assert config.tls.client_ca == "ca.pem"


def test_cli_no_tls_overrides_json_tls(tmp_path: Path) -> None:
    """``--no-tls`` 是本地演示逃生门：JSON 里开着 TLS 也能关掉。"""
    payload = {
        "name": "cli-test",
        "control": {"host": "127.0.0.1", "port": 7000},
        "data": {"host": "127.0.0.1", "port": 7001},
        "mapping": [{"public_port": 9028, "local_port": 8000}],
        "tls": {"enabled": True, "cert": "c.pem", "key": "k.pem"},
    }
    path = tmp_path / "server.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    config = load_config(parse(["-c", str(path), "--no-tls"]))

    assert config.tls.enabled is False


def test_cli_without_tls_keeps_json_tls(tmp_path: Path) -> None:
    """不给命令行 TLS 参数时，JSON 里的 TLS 设置必须原样保留。"""
    payload = {
        "name": "cli-test",
        "control": {"host": "127.0.0.1", "port": 7000},
        "data": {"host": "127.0.0.1", "port": 7001},
        "mapping": [{"public_port": 9028, "local_port": 8000}],
        "tls": {"enabled": True, "cert": "c.pem", "key": "k.pem"},
    }
    path = tmp_path / "server.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    config = load_config(parse(["-c", str(path)]))

    assert config.tls.enabled is True


# --------------------------------------------------------------------------- #
# 访客端口 TLS
# --------------------------------------------------------------------------- #


def _write_visitor_tls_config(tmp_path: Path) -> Path:
    payload: Dict[str, Any] = {
        "name": "cli-test",
        "control": {"host": "127.0.0.1", "port": 7000},
        "data": {"host": "127.0.0.1", "port": 7001},
        "mapping": [{"public_port": 9028, "local_port": 8000}],
        "tls": {"visitor_enabled": True, "visitor_cert": "json-vc.pem", "visitor_key": "json-vk.pem"},
    }
    path = tmp_path / "visitor.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_cli_visitor_cert_and_key_imply_global_default_on(tmp_path: Path) -> None:
    """给出访客证书即隐式打开**全局默认**，但绝不碰控制/数据两跳的 ``tls.enabled``。"""
    path = write_server_config(tmp_path)
    config = load_config(
        parse(["-c", str(path), "--visitor-tls-cert", "vc.pem", "--visitor-tls-key", "vk.pem"])
    )

    assert config.tls.visitor_enabled is True
    assert config.tls.visitor_cert == "vc.pem"
    assert config.tls.visitor_key == "vk.pem"
    assert config.tls.enabled is False, "访客端口 TLS 与控制/数据两跳是两个独立开关"


def test_cli_no_visitor_tls_overrides_json(tmp_path: Path) -> None:
    """``--no-visitor-tls`` 是逃生门：JSON 里开着也能一键全关。"""
    path = _write_visitor_tls_config(tmp_path)
    config = load_config(parse(["-c", str(path), "--no-visitor-tls"]))

    assert config.tls.visitor_enabled is False


def test_cli_visitor_tls_cert_and_no_visitor_tls_are_mutually_exclusive(tmp_path: Path) -> None:
    path = write_server_config(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        parse(["-c", str(path), "--visitor-tls-cert", "vc.pem", "--no-visitor-tls"])

    assert excinfo.value.code == 2


def test_cli_without_visitor_flags_keeps_json_visitor_tls(tmp_path: Path) -> None:
    """不给任何访客参数时 JSON 原样保留——证明这些开关的 default 是 None（三态）。"""
    path = _write_visitor_tls_config(tmp_path)
    config = load_config(parse(["-c", str(path)]))

    assert config.tls.visitor_enabled is True
    assert config.tls.visitor_cert == "json-vc.pem"
    assert config.tls.visitor_key == "json-vk.pem"

    args = parse(["-c", str(path)])
    assert args.visitor_tls_cert is None
    assert args.visitor_tls_key is None
    assert args.no_visitor_tls is False
