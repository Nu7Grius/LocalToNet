# -*- coding: utf-8 -*-
"""
localtonet.server.tokenstore —— 令牌表（**唯一出处**）
======================================================
鉴权二期的核心数据：一张"令牌 → 身份 + 允许认领的内网端口"的表。

和 ``server/mapping.py``（映射存储）、``core/tls.py``（SSL 上下文）同一个定位：
**只有这一个地方解析令牌表格式**，别处一律不许自己 ``json.load`` 一遍。

五条刻意的设计
--------------

**1. 只放独立文件，不内联进 ``config.json``。**
令牌是机密，而配置文件常被提交进仓库、贴进工单和文档。独立文件才好 chmod 600、
好单独轮换、好在部署时挂一个 secret 卷。

**2. 明文与哈希二选一。**
``token``（明文，便于本地演示）**或** ``token_sha256``（hex，推荐生产）。
同时给两者直接报错——两个凭据来源就是"让运维猜哪个生效"，本项目一贯 fail fast。

**3. 重载失败保留旧表，绝不降级为放行。**
文件损坏 / 消失 / 解析失败 → 记 ERROR、沿用旧表、返回 ``False``。
安全组件的失败方向必须是"更严"而不是"更松"：一旦降级放行，
运维会在完全无感的情况下把服务端暴露成无鉴权。

**4. 比对必须走完全部条目。**
``lookup`` 里刻意**不 break**：命中即 ``return`` 会让"第几条匹配上了""前缀匹配了多久"
从耗时上泄漏出去（时序侧信道）。被 ``enabled: false`` 吊销的条目同样要参与比较——
否则"这个令牌存在但被吊销"与"这个令牌根本不存在"在耗时上可区分。

**5. 映射表写权限默认关闭（fail closed）。**
``can_manage_mapping`` 省略时为 ``False``：映射表是**全局**的，能改它就能把任意访客端口
指向自己的机器。它和 ``enabled`` 一样属于安全开关，默认值必须落在更严的一侧。

令牌明文**绝不进日志**（连 DEBUG 都不行，见 MEMORY 不变量 1）。
``TokenEntry`` 里也因此**不含**密钥字段：可以被放心地塞进日志、事件与快照。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple

from config import ConfigError, check_port
from logging_setup import get_logger

__all__ = ["TokenEntry", "TokenStore", "TOKEN_TABLE_VERSION", "compare_secret"]

TOKEN_TABLE_VERSION = 1
"""令牌表顶层 ``version`` 只接受这一个值。将来改格式时用它把老文件挡住。"""

_ROOT_FIELDS = ("version", "tokens")
_ENTRY_FIELDS = (
    "name",
    "token",
    "token_sha256",
    "client_id",
    "ports",
    "enabled",
    "can_manage_mapping",
)

_HEX64 = re.compile(r"[0-9a-fA-F]{64}")

CompareFn = Callable[[str, str], bool]
"""常量时间比较函数的签名；测试可注入假实现来数调用次数。"""


def compare_secret(left: str, right: str) -> bool:
    """常量时间字符串比较。

    不用裸 ``==``：普通比较会提前返回，泄漏"匹配到第几个字符"。
    也刻意**不**直接把 str 交给 :func:`hmac.compare_digest`——
    它对非 ASCII 的 str 会抛 ``TypeError``，一个中文令牌就能把服务端打成 500。
    """
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TokenEntry:
    """一条令牌记录**可公开的部分**（不含密钥，可放心进日志/事件/快照）。"""

    name: str
    """身份标签。进日志、事件与 ``ClientSession.identity``——运维据此知道"是谁"在线。"""

    client_id: str = ""
    """可选：给出则注册消息里的 ``client_id`` 必须完全相等（防冒名顶替）。空＝不校验。"""

    ports: Tuple[int, ...] = ()
    """允许认领的**内网端口**（``local_ports``）。**空＝不限**（与 ``limits`` 的 ``0``＝不限一致）。
    要禁止一切认领请用 ``enabled: false``，而不是 ``ports: []``。"""

    enabled: bool = True
    """``false`` ＝吊销。条目仍参与比较（见模块 docstring 第 4 条）。"""

    can_manage_mapping: bool = False
    """能否修改**服务端的映射表**（``set_mapping`` 指令）。

    默认 ``False`` ＝ **fail closed**。映射表是全局的：一张表决定"哪个公网端口
    通往哪台内网机器"，一个能改它的客户端就能把任意访客端口指到自己的机器上。
    要放行必须在条目里显式写 ``"can_manage_mapping": true``——
    忘了表态的结果是"改不动映射表"，而不是"悄悄放开全局映射表"。

    判定时机是**注册**，不是每次提交：注册通过后这个值被快照进
    ``ClientSession``，会话期间不再回查令牌表（那时令牌已被抹掉，见
    ``server/core.py`` 的注册段）。因此**改文件后需要该客户端重连才生效**——
    与"已建立的连接不因令牌轮换而断开"是同一条语义。
    """

    def allows(self, local_port: int) -> bool:
        """该身份能否认领某个内网端口。空 ``ports`` ＝不限。"""
        return not self.ports or local_port in self.ports

    def describe_ports(self) -> str:
        return ",".join(str(port) for port in self.ports) if self.ports else "不限"


@dataclass(frozen=True)
class _StoredToken:
    """令牌表里的原始密钥 + 它的身份。

    密钥只活在这里：``lookup`` 只把 :class:`TokenEntry` 交出去。
    """

    entry: TokenEntry
    kind: str  # "plain" | "sha256"
    secret: str  # 明文令牌本身，或 token_sha256 的 64 位 hex

    def matches(self, provided: str, compare: CompareFn) -> bool:
        candidate = provided if self.kind == "plain" else _sha256_hex(provided)
        return compare(candidate, self.secret)


# --------------------------------------------------------------------------- #
# 解析（fail fast）
# --------------------------------------------------------------------------- #


def _require_object(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where} 必须是对象，实际为 {type(value).__name__}")
    return value


def _check_unknown(data: Mapping[str, Any], allowed: Sequence[str], where: str) -> None:
    """与 ``config._check_unknown`` 同风格：未知字段直接报错，不静默忽略。

    静默忽略意味着"写错了字段名但服务端照跑"，排查起来极其昂贵。
    """
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ConfigError(f"{where} 存在未知字段 {unknown}，允许的字段为 {list(allowed)}")


def _as_ports(value: Any, where: str) -> Tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{where}.ports 必须是数组（省略或 [] 都表示不限）")
    ports: List[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ConfigError(f"{where}.ports 里必须都是整数端口，实际为 {item!r}")
        port = check_port(item, f"{where}.ports")
        if port not in ports:
            ports.append(port)
    return tuple(sorted(ports))


def _as_flag(value: Any, where: str, field: str, *, default: bool) -> bool:
    """三态布尔：省略取 ``default``，给了就必须是布尔。

    ``0`` / ``"true"`` / ``"yes"`` 一律报错，不做隐式转换——本项目一贯 fail fast，
    而权限字段最怕的就是"我写了 can_manage_mapping: 1 以为开了，其实没开/没生效"。
    """
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{field} 必须是布尔值，实际为 {type(value).__name__}")
    return value


def _parse_entry(item: Any, where: str, seen_names: set[str]) -> _StoredToken:
    data = _require_object(item, where)
    _check_unknown(data, _ENTRY_FIELDS, where)

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{where}.name 必须是非空字符串（身份标签，会进日志与事件）")
    name = name.strip()
    if name in seen_names:
        raise ConfigError(f"{where}.name 与前面的条目重复：{name!r}（身份标签必须唯一，否则日志分不清是谁）")
    seen_names.add(name)

    plain = data.get("token")
    hashed = data.get("token_sha256")
    if plain is None and hashed is None:
        raise ConfigError(
            f"{where} 必须提供 token（明文，便于本地演示）或 token_sha256（64 位 hex，推荐生产）之一"
        )
    if plain is not None and hashed is not None:
        raise ConfigError(f"{where} 同时给出了 token 与 token_sha256，无法判断以哪个为准（二选一）")

    if plain is not None:
        if not isinstance(plain, str) or not plain:
            raise ConfigError(f"{where}.token 必须是非空字符串")
        kind, secret = "plain", plain
    else:
        if not isinstance(hashed, str) or not _HEX64.fullmatch(hashed):
            raise ConfigError(f"{where}.token_sha256 必须是 64 位十六进制字符串（sha256 的 hexdigest）")
        kind, secret = "sha256", hashed.lower()

    client_id = data.get("client_id", "")
    if not isinstance(client_id, str):
        raise ConfigError(f"{where}.client_id 必须是字符串")

    return _StoredToken(
        entry=TokenEntry(
            name=name,
            client_id=client_id.strip(),
            ports=_as_ports(data.get("ports"), where),
            enabled=_as_flag(data.get("enabled"), where, "enabled", default=True),
            # 省略＝不允许改映射表（fail closed，理由见 TokenEntry 的字段注释）
            can_manage_mapping=_as_flag(
                data.get("can_manage_mapping"), where, "can_manage_mapping", default=False
            ),
        ),
        kind=kind,
        secret=secret,
    )


def _parse_table(path: Path) -> List[_StoredToken]:
    """读文件 + 解析 + 校验。**任何问题都抛 ``ConfigError``**（fail fast）。"""
    if not path.is_file():
        raise ConfigError(f"令牌表文件不存在或不是普通文件：{path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取令牌表文件 {path}：{exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"令牌表 {path} 不是合法 JSON：{exc}") from exc

    root = _require_object(data, f"令牌表 {path}")
    _check_unknown(root, _ROOT_FIELDS, f"令牌表 {path}")

    version = root.get("version", TOKEN_TABLE_VERSION)
    if version != TOKEN_TABLE_VERSION:
        raise ConfigError(f"令牌表 {path} 的 version 只支持 {TOKEN_TABLE_VERSION}，实际为 {version!r}")

    raw_tokens = root.get("tokens")
    if not isinstance(raw_tokens, list):
        raise ConfigError(f"令牌表 {path} 的 tokens 必须是数组，实际为 {type(raw_tokens).__name__}")
    if not raw_tokens:
        # 空表会拒绝**所有**客户端，却长得像"配好了"。这属于配置错误，不是"关闭鉴权"。
        raise ConfigError(
            f"令牌表 {path} 的 tokens 是空数组。空表会拒绝所有客户端；"
            "要关闭鉴权请用 --no-auth（或 auth.enabled=false），别用空表"
        )

    seen_names: set[str] = set()
    return [
        _parse_entry(item, f"{path.name}.tokens[{index}]", seen_names)
        for index, item in enumerate(raw_tokens)
    ]


# --------------------------------------------------------------------------- #
# 令牌表
# --------------------------------------------------------------------------- #


class TokenStore:
    """令牌表：解析、比对、按 ``mtime + size`` 惰性热重载。

    热重载**不加后台轮询任务**：每次注册尝试在 :meth:`load` 出来的表上先调一次
    :meth:`reload_if_changed`，比一下文件戳就够了。语义因此很清楚——
    "改文件 → 最迟在下一次注册尝试生效"，而客户端的退避重试会自然触发下一次尝试。
    少一个活动部件，也就少一处能出错的并发面。
    """

    def __init__(
        self,
        path: str | Path,
        tokens: Sequence[_StoredToken],
        *,
        compare: Optional[CompareFn] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._path = Path(path)
        self._tokens: List[_StoredToken] = list(tokens)
        self._compare: CompareFn = compare or compare_secret
        self._log = logger or get_logger("server.tokenstore")
        self._loaded_at = time.time()
        self._stamp = self._stat_stamp()

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        compare: Optional[CompareFn] = None,
        logger: Optional[logging.Logger] = None,
    ) -> "TokenStore":
        """读入令牌表。文件缺失 / 格式错 / 语义错一律抛 :class:`ConfigError`。"""
        resolved = Path(path)
        tokens = _parse_table(resolved)
        log = logger or get_logger("server.tokenstore")
        # 只打条数与路径：令牌明文绝不落日志（不变量 1）
        log.info("已载入令牌表 %s（%d 条）", resolved, len(tokens))
        return cls(resolved, tokens, compare=compare, logger=logger)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    @property
    def path(self) -> Path:
        return self._path

    @property
    def entry_count(self) -> int:
        return len(self._tokens)

    @property
    def loaded_at(self) -> float:
        """最近一次成功载入的时刻（``time.time()``），供日志与测试用。"""
        return self._loaded_at

    @property
    def names(self) -> Tuple[str, ...]:
        """全部身份标签（含被吊销的），供启动日志打一行"都有谁"。"""
        return tuple(stored.entry.name for stored in self._tokens)

    def lookup(self, provided: str) -> Optional[TokenEntry]:
        """按令牌找身份；没有任何条目匹配则返回 ``None``。

        三条刻意行为：

        1. **遍历全部条目**、每一条都做一次常量时间比较，最后才取命中项。
           命中即 ``return`` 会泄漏"第几条匹配了多久"（时序侧信道）。
        2. 被吊销（``enabled=False``）的条目**照样参与比较**。
        3. **不按 ``enabled`` 过滤**：命中即返回条目，由调用方区分"无效"与"已吊销"
           ——两者的日志与给客户端的说法不同，混在一起会把"你被吊销了"说成"令牌不对"。
        """
        candidate = provided if isinstance(provided, str) else ""
        hit: Optional[TokenEntry] = None
        for stored in self._tokens:
            if stored.matches(candidate, self._compare) and hit is None:
                hit = stored.entry
        return hit

    # ------------------------------------------------------------------ #
    # 热重载
    # ------------------------------------------------------------------ #

    def reload_if_changed(self) -> bool:
        """文件戳变了就重载。返回 ``True`` 表示这次真的换了表。

        **失败时保留旧表**并返回 ``False``：文件损坏 / 消失 / 解析失败都只记 ERROR，
        绝不降级为放行（不变量 2）。

        失败路径也会更新戳，免得同一个坏文件在每次注册尝试上都刷一条 ERROR；
        运维把文件修好后戳必然再变，重载照常发生。
        """
        stamp = self._stat_stamp()
        if stamp == self._stamp:
            return False

        try:
            tokens = _parse_table(self._path)
        except ConfigError as exc:
            self._stamp = stamp
            self._log.error(
                "令牌表 %s 重载失败：%s；继续沿用旧的 %d 条，绝不降级为放行",
                self._path,
                exc,
                len(self._tokens),
            )
            return False

        self._tokens = tokens
        self._stamp = stamp
        self._loaded_at = time.time()
        self._log.info("令牌表已重载：%d 条（%s）", len(tokens), self._path)
        return True

    def _stat_stamp(self) -> Optional[Tuple[int, int]]:
        """文件的 ``(mtime_ns, size)``；文件不在时返回 ``None``。

        用 ``mtime_ns`` 而不是 ``mtime``：秒级/百纳秒级整数更稳，
        避免"同一秒内改了两次且大小不变"被漏掉。
        """
        try:
            info = self._path.stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def __repr__(self) -> str:
        # 刻意只报条数与路径：repr 经常被日志与调试器顺手打出来
        return f"TokenStore(path={str(self._path)!r}, entries={len(self._tokens)})"
