# -*- coding: utf-8 -*-
"""
server_gui.py —— 服务端管理台入口（公网侧）
============================================
用法::

    python server_gui.py                                # 读同目录 config.json 并自动启动
    python server_gui.py -c my.json --mapping 9028:8000
    python server_gui.py --auth-file tokens.json        # 令牌表鉴权
    python server_gui.py --no-autostart                 # 只开窗口，不自动启动服务端

管理台能做的事：启动/停止服务端、看**在线客户端**（身份、对端地址、认领的内网端口、
在线与空闲时长）、编辑并提交映射表（提交后立即起停访客端口，并广播给所有客户端）、
实时状态栏与事件日志。

命令行参数与 ``server.py`` 完全一致（同一套解析函数），所以习惯命令行的人不用重新学一套。

三条边界要先说清楚：

1. **不新增协议、不做远程管理**。管理台就贴在服务端进程里，直接读
   :meth:`TunnelServer.snapshot`、直接调 :meth:`TunnelServer.submit_mapping`。
   "服务端继续跑、窗口在另一台机器上开"是远程管理，需要协议扩展与权限模型，
   不在本轮范围。
2. **关掉窗口 = 服务端下线**（访客端口全部关闭）。要长时间托管请用 ``server.py``。
3. **在线客户端表是只读的**：本轮不做"踢人"——没有权限模型的情况下，
   一个误点的按钮就能掐断正在服务的隧道。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import ConfigError  # noqa: E402
from localtonet.errors import TunnelError  # noqa: E402
from localtonet.gui import create_server_app  # noqa: E402
from logging_setup import setup_logging  # noqa: E402
from server import build_parser, load_config  # noqa: E402 - 复用命令行服务端的参数定义


def build_gui_parser() -> argparse.ArgumentParser:
    """在服务端参数之上追加界面专属开关。"""
    parser = build_parser()
    parser.prog = "server_gui.py"
    parser.description = "内网穿透服务端管理台：看在线客户端、可视化编辑映射表，提交即生效"
    parser.add_argument("--no-autostart", action="store_true", help="打开窗口后不自动启动服务端")
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
        "管理台启动：服务端 %s，控制 %s:%d，数据 %s:%d，访客端口 %s",
        config.name,
        config.control.host,
        config.control.port,
        config.data.host,
        config.data.port,
        [f"{rule.public_port}->{rule.local_port}" for rule in config.mapping],
    )

    try:
        app = create_server_app(config, autostart=not args.no_autostart)
    except RuntimeError as exc:
        # 没有 tkinter（常见于精简版 Linux）时给一句能照着做的提示，而不是堆栈
        print(f"无法启动管理台：{exc}", file=sys.stderr)
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
