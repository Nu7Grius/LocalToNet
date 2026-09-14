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
    "LOCALTONET_MAPPING_STORE",
    "LOCALTONET_MAPPING_STORE_PATH",
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
