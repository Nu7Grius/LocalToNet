# -*- coding: utf-8 -*-
"""
localtonet.core.rules —— 端口与映射表的共享校验
================================================
这两个函数原先藏在 :class:`localtonet.server.core.TunnelServer` 里当私有静态方法。
抽出来的原因很实际：**GUI 要在用户敲下数字的那一刻就标红**，而"什么算合法"只允许有
一份定义。让 GUI 自己再写一遍，就会出现"界面说没问题、服务端回执报错"的分裂——
这种 bug 排查起来极痛苦，因为它取决于两边谁先被改动。

因此：

* 服务端收到 ``register_client`` / ``set_mapping`` 时用它做**准入校验**；
* GUI 在表格编辑/提交前用同一个函数做**即时校验**。

同一条规则，一处定义，两个调用方。改规则只改这里，两边自动一致。

失败一律抛 :class:`config.ConfigError`（带 ``code``，可映射成 HTTP 状态码）。
"""

from __future__ import annotations

from typing import Any, Dict, List

from config import ConfigError, MappingRule, check_port

__all__ = ["parse_ports", "parse_mapping"]


def parse_ports(raw: Any, label: str = "local_ports") -> List[int]:
    """把一串原始值解析成合法的本地端口列表。

    * 必须是**非空**数组（认领零个端口的客户端没有意义，按错误处理而非静默接受）；
    * 每项必须是真正的 ``int``——``bool`` 是 ``int`` 的子类，必须显式排除，
      否则 ``True`` 会被当成端口 ``1``；
    * 不允许重复：重复端口在 ``claim_ports`` 里会退化成"重复认领同一个端口"，
      表面上成功，实际掩盖了配置写错。
    """
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{label} 必须是非空端口数组")
    ports: List[int] = []
    for index, item in enumerate(raw):
        if isinstance(item, bool) or not isinstance(item, int):
            raise ConfigError(f"{label}[{index}] 必须是整数端口")
        ports.append(check_port(item, f"{label}[{index}]"))
    if len(set(ports)) != len(ports):
        raise ConfigError(f"{label} 存在重复端口")
    return ports


def parse_mapping(raw: Any, label: str = "mapping") -> List[MappingRule]:
    """把原始数组解析成 :class:`config.MappingRule` 列表。

    * 不允许为空——把最后一条映射删掉等于"服务端一个访客端口都不监听"，
      这种情况应当走显式的停机流程，而不是一条空映射悄悄生效；
    * ``public_port`` 不允许重复：同一个公网端口只能有一条规则，
      否则监听该端口的那个 ``asyncio.Server`` 该用哪条规则就成了未定义行为。
    """
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{label} 不能为空，至少保留一条映射")
    rules: List[MappingRule] = []
    seen: Dict[int, int] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"{label}[{index}] 必须是对象")
        rule = MappingRule.from_dict(item, f"{label}[{index}]")
        if rule.public_port in seen:
            raise ConfigError(
                f"{label}[{index}].public_port={rule.public_port} 与 {label}[{seen[rule.public_port]}] 重复"
            )
        seen[rule.public_port] = index
        rules.append(rule)
    return rules
