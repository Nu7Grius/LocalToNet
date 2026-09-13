# -*- coding: utf-8 -*-
"""
client.py —— 客户端入口（内网侧）
==================================
用法::

    python client.py --server 127.0.0.1 --local-ports 8000
    python client.py -c client.json
    python client.py --server example.com:7000 --local-ports 8000,8080

客户端管三件事：连上服务端控制通道、维持心跳、把访客请求转进本机后端。
断线会自动指数退避重连（1s → 60s 封顶），重连成功后重新认领端口。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from config import ClientConfig, ConfigError, check_port
from localtonet.client.core import TunnelClient
from localtonet.errors import TunnelError
from logging_setup import get_logger, setup_logging

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "client.json"


def parse_server(value: str) -> Tuple[str, Optional[int]]:
    """支持 ``host`` 与 ``host:port`` 两种写法。"""
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError("--server 不能为空")
    host, sep, port_raw = text.rpartition(":")
    if not sep:
        return text, None
    if not host:
        raise argparse.ArgumentTypeError(f"--server 缺少主机名：{value!r}")
    try:
        return host, check_port(int(port_raw), "--server")
    except (TypeError, ValueError, ConfigError) as exc:
        raise argparse.ArgumentTypeError(f"--server {value!r} 解析失败：{exc}") from exc


def parse_ports(value: str) -> List[int]:
    ports: List[int] = []
    for item in value.replace(",", " ").split():
        try:
            ports.append(check_port(int(item), "--local-ports"))
        except (TypeError, ValueError, ConfigError) as exc:
            raise argparse.ArgumentTypeError(f"端口 {item!r} 非法：{exc}") from exc
    if not ports:
        raise argparse.ArgumentTypeError("--local-ports 至少要给一个端口")
    return ports


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="client.py",
        description="内网穿透客户端：把公网访客请求转进本机端口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径（默认 client.json）")
    parser.add_argument("--server", type=parse_server, metavar="主机[:端口]", help="服务端地址，例如 1.2.3.4:7000")
    parser.add_argument("--data-port", type=int, help="数据通道端口（服务端未下发时使用，默认 7001）")
    parser.add_argument("--client-id", help="客户端标识，留空自动生成（主机名-随机后缀）")
    parser.add_argument("--local-host", help="内网后端地址（默认 127.0.0.1）")
    parser.add_argument("--local-ports", type=parse_ports, metavar="端口,...", help="要认领的本机端口，逗号或空格分隔")
    parser.add_argument("--token", help="服务端开启鉴权时的访问令牌")
    parser.add_argument("--log-level", help="日志级别（DEBUG/INFO/WARNING/ERROR）")
    parser.add_argument("--log-file", help="同时写入日志文件")
    return parser


def load_config(args: argparse.Namespace) -> ClientConfig:
    config_path = Path(args.config)
    if config_path.is_file():
        config = ClientConfig.from_env(ClientConfig.from_file(config_path))
    else:
        if config_path != DEFAULT_CONFIG_PATH:
            raise ConfigError(f"指定的配置文件不存在：{config_path}")
        config = ClientConfig.from_env(ClientConfig(local_ports=[8000]))

    if args.server:
        host, port = args.server
        config.server_host = host
        if port is not None:
            config.control_port = port
    if args.data_port is not None:
        config.data_port = check_port(args.data_port, "--data-port")
    if args.client_id:
        config.client_id = args.client_id
    if args.local_host:
        config.local_host = args.local_host
    if args.local_ports:
        config.local_ports = list(args.local_ports)
    if args.token:
        config.auth_token = args.token
    if args.log_level:
        config.log.level = args.log_level.upper()
    if args.log_file:
        config.log.file = args.log_file

    config.validate()
    return config


async def connect(config: ClientConfig) -> int:
    client = TunnelClient(config)
    try:
        await client.run()
    except asyncio.CancelledError:
        pass
    finally:
        await client.stop()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    log = setup_logging(config.log.level, config.log.file)
    log.info(
        "准备连接 %s:%d，认领本机端口 %s (%s)",
        config.server_host,
        config.control_port,
        config.local_ports,
        config.local_host,
    )
    try:
        return asyncio.run(connect(config))
    except KeyboardInterrupt:
        print("\n已收到中断信号，正在退出…")
        return 0
    except TunnelError as exc:
        print(f"客户端异常退出：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
