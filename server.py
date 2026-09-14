# -*- coding: utf-8 -*-
"""
server.py —— 服务端入口（公网侧）
==================================
用法::

    python server.py                          # 读取同目录 config.json
    python server.py -c my.json               # 指定配置
    python server.py --mapping 9028:8000      # 临时映射，覆盖配置文件里的 mapping
    python server.py --token s3cret           # 开启 token 鉴权
    python server.py --no-auth                # 强制关闭鉴权（覆盖 JSON / 环境变量）
    python server.py --mapping-store file --mapping-store-path mappings.json
                                              # 映射持久化，重启后映射还在

配置文件格式见 ``config.json``；所有字段都可用 ``LOCALTONET_`` 前缀的环境变量覆盖，
命令行参数的优先级最高（默认值 < JSON < 环境变量 < 命令行）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from config import ConfigError, MappingRule, ServerConfig, check_port
from localtonet.core.events import EventType
from localtonet.errors import TunnelError
from localtonet.server.core import TunnelServer
from logging_setup import get_logger, setup_logging

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"


def parse_mapping_item(value: str) -> MappingRule:
    """解析 ``公网端口:本地端口`` 形式的映射参数。"""
    text = value.strip()
    if ":" not in text:
        raise argparse.ArgumentTypeError(f"映射必须是 公网端口:本地端口，收到 {value!r}")
    public_raw, _, local_raw = text.partition(":")
    try:
        public_port = check_port(int(public_raw), "--mapping")
        local_port = check_port(int(local_raw), "--mapping")
    except (TypeError, ValueError, ConfigError) as exc:
        raise argparse.ArgumentTypeError(f"映射 {value!r} 解析失败：{exc}") from exc
    return MappingRule(public_port=public_port, local_port=local_port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="server.py",
        description="内网穿透服务端：监听控制/数据通道与访客端口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径（默认 config.json）")
    parser.add_argument("--control-host", help="控制通道绑定地址")
    parser.add_argument("--control-port", type=int, help="控制通道端口（默认 7000）")
    parser.add_argument("--data-host", help="数据通道绑定地址")
    parser.add_argument("--data-port", type=int, help="数据通道端口（默认 7001）")
    parser.add_argument("--advertise-host", help="告知客户端回连数据通道的地址，公网部署时填公网 IP")
    parser.add_argument(
        "--mapping",
        action="append",
        type=parse_mapping_item,
        metavar="公网:本地",
        help="端口映射，可重复；一旦指定就整体替换配置文件里的 mapping",
    )
    parser.add_argument(
        "--mapping-store",
        choices=("memory", "file"),
        help="映射表存储方式：memory（默认，重启即回到配置文件）或 file（持久化，重启后以文件为准）",
    )
    parser.add_argument(
        "--mapping-store-path",
        metavar="路径",
        help="持久化文件路径（默认 mappings.json，相对当前工作目录）；会自动开启 file 模式",
    )
    auth_group = parser.add_mutually_exclusive_group()
    auth_group.add_argument(
        "--token",
        metavar="令牌",
        help="开启 token 鉴权（等价于 auth.enabled=true + auth.token），客户端须带同一令牌",
    )
    auth_group.add_argument(
        "--no-auth",
        action="store_true",
        help="强制关闭鉴权，覆盖配置文件与环境变量里的设置（本地演示用）",
    )
    tls_group = parser.add_mutually_exclusive_group()
    tls_group.add_argument(
        "--tls-cert",
        metavar="路径",
        help="开启 TLS 并指定证书链（PEM）。与 --tls-key 配套，给出即隐式开启 TLS",
    )
    tls_group.add_argument(
        "--no-tls",
        action="store_true",
        help="强制关闭 TLS，覆盖配置文件与环境变量里的设置（本地演示用）",
    )
    parser.add_argument("--tls-key", metavar="路径", help="TLS 私钥（PEM），与 --tls-cert 配套")
    parser.add_argument(
        "--tls-client-ca",
        metavar="路径",
        help="校验客户端证书用的 CA；给出即要求客户端出示证书（双向认证 mTLS）",
    )
    parser.add_argument("--log-level", help="日志级别（DEBUG/INFO/WARNING/ERROR）")
    parser.add_argument("--log-file", help="同时写入日志文件")
    return parser


def load_config(args: argparse.Namespace) -> ServerConfig:
    config_path = Path(args.config)
    if config_path.is_file():
        config = ServerConfig.from_env(ServerConfig.from_file(config_path))
    else:
        if config_path != DEFAULT_CONFIG_PATH:
            raise ConfigError(f"指定的配置文件不存在：{config_path}")
        config = ServerConfig.from_env(ServerConfig(mapping=[MappingRule(9028, 8000)]))

    if args.control_host:
        config.control.host = args.control_host
    if args.control_port is not None:
        config.control.port = check_port(args.control_port, "--control-port")
    if args.data_host:
        config.data.host = args.data_host
    if args.data_port is not None:
        config.data.port = check_port(args.data_port, "--data-port")
    if args.advertise_host:
        config.advertise_host = args.advertise_host
    if args.mapping:
        config.mapping = list(args.mapping)
    # 映射存储：命令行优先级最高。只给路径时自动切到 file——
    # "给了持久化路径却还留在 memory 模式"是最容易让人踩空的组合。
    if args.mapping_store:
        config.mapping_store.type = args.mapping_store
    if args.mapping_store_path:
        config.mapping_store.path = args.mapping_store_path
        config.mapping_store.type = "file"
    # 鉴权：命令行优先级最高（默认值 < JSON < 环境变量 < 命令行）。
    # --token 的 default 必须是 None，否则无法区分"没给"和"给了空串"。
    if args.no_auth:
        config.auth.enabled = False
        config.auth.token = ""
    if args.token is not None:
        token = args.token.strip()
        if not token:
            raise ConfigError("--token 不能为空；若要关闭鉴权请改用 --no-auth")
        config.auth.token = token
        config.auth.enabled = True
    # TLS：命令行优先级最高。--no-tls 是逃生门；--tls-cert/--tls-key 给出即隐式开启
    # （与 --mapping-store-path 隐式切 file 同理，避免"路径都填了却漏了开关"）。
    # 这些参数的 default 必须是 None，否则无法区分"没给"和"给了空串"。
    if args.no_tls:
        config.tls.enabled = False
    if args.tls_cert is not None:
        if not args.tls_cert.strip():
            raise ConfigError("--tls-cert 不能为空；若要关闭 TLS 请改用 --no-tls")
        config.tls.cert = args.tls_cert
        config.tls.enabled = True
    if args.tls_key is not None:
        if not args.tls_key.strip():
            raise ConfigError("--tls-key 不能为空")
        config.tls.key = args.tls_key
        config.tls.enabled = True
    if args.tls_client_ca is not None:
        if not args.tls_client_ca.strip():
            raise ConfigError("--tls-client-ca 不能为空")
        config.tls.client_ca = args.tls_client_ca
        config.tls.require_client_cert = True
        config.tls.enabled = True
    if args.log_level:
        config.log.level = args.log_level.upper()
    if args.log_file:
        config.log.file = args.log_file

    config.validate()
    return config


async def serve(config: ServerConfig) -> int:
    log = get_logger("server.cli")
    try:
        server = TunnelServer(config)
    except ConfigError as exc:
        # 持久化映射文件损坏这类问题归到"配置错误"，与命令行解析失败同一个退出码
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    def announce(**payload: object) -> None:
        log.info("服务端就绪：%s", payload)

    server.events.once(EventType.SERVER_STARTED, announce)

    await server.start()
    try:
        await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        await server.stop()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    setup_logging(config.log.level, config.log.file)
    try:
        return asyncio.run(serve(config))
    except KeyboardInterrupt:
        print("\n已收到中断信号，正在退出…")
        return 0
    except TunnelError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
