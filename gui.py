# -*- coding: utf-8 -*-
"""
gui.py —— 图形界面入口（内网侧）
================================
用法::

    python gui.py
    python gui.py -c client.json --server 1.2.3.4:7000 --local-ports 8000
    python gui.py --token 你的令牌 --log-level DEBUG
    python gui.py --no-autostart        # 打开界面但不自动连接

界面能做的事：连接/断开、映射表增删改（双击一行即可编辑）、提交到服务端、
实时状态栏与事件日志。**提交走的是既有的 set_mapping 语义**——
服务端立刻起停对应端口，不需要改 JSON，也不需要重启客户端。

命令行参数与 ``client.py`` 完全一致（同一套解析函数），
所以习惯命令行的人不用重新学一套。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from client import build_parser, load_config  # noqa: E402 - 复用命令行客户端的参数定义
from config import ConfigError  # noqa: E402
from localtonet.gui import create_app  # noqa: E402
from localtonet.errors import TunnelError  # noqa: E402
from logging_setup import setup_logging  # noqa: E402


def build_gui_parser() -> argparse.ArgumentParser:
    """在客户端参数之上追加界面专属开关。"""
    parser = build_parser()
    parser.prog = "gui.py"
    parser.description = "内网穿透客户端图形界面：可视化编辑映射表，提交即生效"
    parser.add_argument("--no-autostart", action="store_true", help="打开界面后不自动连接服务端")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_gui_parser().parse_args(argv)

    try:
        config = load_config(args)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    log = setup_logging(config.log.level, config.log.file)
    log.info(
        "图形界面启动：服务端 %s:%d，认领本机端口 %s（%s）",
        config.server_host,
        config.control_port,
        config.local_ports,
        config.local_host,
    )

    try:
        app = create_app(config, autostart=not args.no_autostart)
    except RuntimeError as exc:
        # 没有 tkinter（常见于精简版 Linux）时给一句能照着做的提示，而不是堆栈
        print(f"无法启动图形界面：{exc}", file=sys.stderr)
        return 2
    except TunnelError as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 1

    try:
        return app.run()
    except KeyboardInterrupt:
        print("\n已收到中断信号，正在退出…")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
