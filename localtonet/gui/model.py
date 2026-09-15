# -*- coding: utf-8 -*-
"""
localtonet.gui.model —— 界面的无头状态层
==========================================
本模块**不 import tkinter**，所以能在无显示环境（CI、SSH、没有 X 的服务器）里被
完整测试。界面层 :mod:`localtonet.gui.app` 只负责把这里的状态画出来。

两个对象，职责严格分开：

``MappingTableModel`` —— 映射表的**编辑缓冲区**
    用户改的是一份工作副本，服务端最近一次下发的版本另存一份快照。
    两边的字段级差异就是"有没有未提交改动"。
    每一次编辑都先过 :func:`localtonet.core.rules.parse_mapping`——**与服务端准入校验
    是同一个函数**。这一点是刻意的：如果 UI 自己写一套"看起来对"的规则，
    迟早出现"界面点得下去、服务端回执报错"的分裂，而那种 bug 取决于两边谁先被改动，
    极难排查。校验只有一份，两个调用方都从那里取。

``GuiState`` + :func:`apply_event` —— 界面要展示的状态
    状态字段直接采用 :meth:`TunnelClient.snapshot` 的返回值（它就是为 GUI 准备的公开 API），
    本模块**不重新统计**任何计数器。事件只用来生成**日志行与提示语**：
    "为什么断了""这是第几次重连"这类信息只存在于事件载荷里，快照里没有。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, Dict, List, Mapping, Optional, Sequence, Tuple

from config import ConfigError, MappingRule
from localtonet.client.core import describe_mapping
from localtonet.core.events import EventType
from localtonet.core.rules import parse_mapping

__all__ = [
    "MAX_LOG_ENTRIES",
    "LogEntry",
    "MappingRow",
    "MappingTableModel",
    "GuiState",
    "adopt_snapshot",
    "append_log",
    "apply_event",
    "describe_tls",
    "note_mapping_result",
]
MAX_LOG_ENTRIES = 500
"""日志面板保留的最近条目数。隧道跑起来后事件很密，必须做环形截断，
否则内存会随着运行时间线性增长。"""

DEFAULT_PUBLIC_PORT_BASE = 9000
"""新增映射时公网端口的自动取值起点。"""

_TLS_LABELS: Dict[Any, str] = {None: "跟随", True: "开", False: "关"}
"""访客端口 TLS 的三态显示文本。键刻意是 ``None``/``True``/``False`` 三个对象。"""


def describe_tls(value: Optional[bool]) -> str:
    """把三态 ``tls`` 转成界面文案。

    ``MappingRule.tls`` 的 ``None`` 不是"没设置"，而是**有语义的一态**（跟随服务端默认），
    所以界面上必须显示成"跟随"而不是留空——留空会让人以为字段没生效。
    """
    return _TLS_LABELS[value]


# --------------------------------------------------------------------------- #
# 映射行
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MappingRow:
    """表格里的一行。字段与 :class:`config.MappingRule` 一一对应。

    做成不可变对象：编辑一律产生新实例，再由模型层统一校验后整体替换。
    这样"改了半个字段、校验失败、模型停在半新半旧"的状态根本不存在。

    ``tls`` 是**三态**（``None`` 跟随 / ``True`` 开 / ``False`` 关）。
    它必须跟着行一起透传：漏掉的话，用户在界面上改一次映射就会把该端口的
    per-port TLS 静默打回默认——那种缺陷在界面上完全看不出来，极难排查。
    """

    public_port: int
    local_port: int
    host: str = "0.0.0.0"
    local_host: str = "127.0.0.1"
    remark: str = ""
    tls: Optional[bool] = None

    FIELDS: ClassVar[Tuple[str, ...]] = (
        "public_port",
        "local_port",
        "host",
        "local_host",
        "remark",
        "tls",
    )
    """表格列顺序，界面直接用它建表头。标成 ``ClassVar``，否则会被当成数据字段
    混进 ``repr``、``__init__`` 与 ``replace``——那会让"比较两行是否相同"凭空多出一列。"""

    @classmethod
    def from_rule(cls, rule: MappingRule) -> "MappingRow":
        return cls(
            public_port=rule.public_port,
            local_port=rule.local_port,
            host=rule.host,
            local_host=rule.local_host,
            remark=rule.remark,
            tls=rule.tls,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "mapping[]") -> "MappingRow":
        """从原始 dict 构造。校验直接复用配置层的 ``MappingRule.from_dict``，
        所以"旧 payload 缺 ``tls`` 键"这件事在这里自动得到容忍（解析成 ``None``）。"""
        return cls.from_rule(MappingRule.from_dict(data, where))

    def to_rule(self) -> MappingRule:
        return MappingRule(
            public_port=self.public_port,
            local_port=self.local_port,
            host=self.host,
            local_host=self.local_host,
            remark=self.remark,
            tls=self.tls,
        )

    def to_dict(self) -> Dict[str, Any]:
        return self.to_rule().to_dict()

    def with_changes(self, **changes: Any) -> "MappingRow":
        """返回替换了若干字段的新行（**不做校验**，校验在模型层统一做）。"""
        return replace(self, **changes)

    def signature(self) -> Tuple[int, int, str, str, str, Optional[bool]]:
        """用于比较"用户改没改"。

        必须**逐字段**取全，不能只比端口：只改备注也算改过，
        否则用户改了备注却点不动"提交"，会以为界面坏了。
        三态 ``tls`` 同理——切了开关却不显示"已修改"会让人以为没生效。
        """
        return (self.public_port, self.local_port, self.host, self.local_host, self.remark, self.tls)

    def describe(self) -> str:
        return f"{self.public_port} -> {self.local_port}"

    def as_cells(self) -> Tuple[str, ...]:
        """表格一行里**配置数据**部分的显示文本（端口等都转字符串）。

        刻意不包含"访客 TLS"与"状态"这两列：它们由界面层在渲染时追加
        （见 ``app._render_table``），这样本方法保持"与 MappingRule 字段同构"，
        界面列怎么排都不用改这里。
        """
        return (
            str(self.public_port),
            str(self.local_port),
            self.host,
            self.local_host,
            self.remark,
        )


# --------------------------------------------------------------------------- #
# 映射表编辑缓冲区
# --------------------------------------------------------------------------- #


class MappingTableModel:
    """映射表的编辑缓冲区：工作副本 + 服务端快照。

    语义与界面上的三个按钮一一对应：

    * 编辑表格 → 改工作副本，立即校验；
    * 提交 → :meth:`rules` 取全表（再校验一次）交给 ``client.set_mapping``；
    * 放弃修改 → :meth:`reset` 回到服务端快照。
    """

    def __init__(
        self,
        rows: Sequence[MappingRow] = (),
        *,
        default_local_port: int = 8000,
        default_local_host: str = "127.0.0.1",
    ) -> None:
        self._rows: List[MappingRow] = list(rows)
        self._snapshot: List[MappingRow] = list(rows)
        self._remote_stale = False
        self._default_local_port = default_local_port
        self._default_local_host = default_local_host

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #

    @property
    def rows(self) -> List[MappingRow]:
        return list(self._rows)

    @property
    def row_count(self) -> int:
        return len(self._rows)

    @property
    def snapshot(self) -> List[MappingRow]:
        """服务端最近一次下发的版本（"已保存"的参照物）。"""
        return list(self._snapshot)

    @property
    def is_dirty(self) -> bool:
        """工作副本与快照是否存在差异。用于控制"提交/放弃修改"按钮的可用状态。"""
        return [row.signature() for row in self._rows] != [row.signature() for row in self._snapshot]

    @property
    def remote_stale(self) -> bool:
        """有未提交改动期间，服务端又更新过映射表。

        界面据此提示用户"远端变了，但你正在编辑，我没有覆盖你的输入"。
        """
        return self._remote_stale

    def row(self, index: int) -> Optional[MappingRow]:
        if 0 <= index < len(self._rows):
            return self._rows[index]
        return None

    def index_of(self, public_port: int) -> int:
        for index, row in enumerate(self._rows):
            if row.public_port == public_port:
                return index
        return -1

    def dirty_indexes(self) -> List[int]:
        """与快照相比内容不同的行号（快照里没有的端口也算）。"""
        by_port = {row.public_port: row.signature() for row in self._snapshot}
        changed: List[int] = []
        for index, row in enumerate(self._rows):
            if by_port.get(row.public_port) != row.signature():
                changed.append(index)
        return changed

    def removed_ports(self) -> List[int]:
        """被用户删掉、但服务端仍存在的公网端口。提交时服务端会关掉它们的监听。"""
        current = {row.public_port for row in self._rows}
        return sorted(row.public_port for row in self._snapshot if row.public_port not in current)

    def local_ports(self) -> List[int]:
        """本表涉及的全部内网端口（去重升序）。"""
        return sorted({row.local_port for row in self._rows})

    def rules(self) -> List[MappingRule]:
        """整表校验后转成 :class:`config.MappingRule` 列表。非法则抛 :class:`ConfigError`。"""
        self._validate(self._rows)
        return [row.to_rule() for row in self._rows]

    # ------------------------------------------------------------------ #
    # 编辑（每一步都即时校验，失败则模型保持不变）
    # ------------------------------------------------------------------ #

    def add_row(
        self,
        *,
        public_port: Optional[int] = None,
        local_port: Optional[int] = None,
        host: str = "0.0.0.0",
        local_host: Optional[str] = None,
        remark: str = "",
        tls: Optional[bool] = None,
    ) -> int:
        """新增一行，返回插入位置（追加在末尾）。

        公网端口留空时自动挑一个没被占用的值，这样连点两次"新增"不会立刻撞重复规则。
        ``tls`` 默认 ``None``（跟随服务端默认），不改变既有行为。
        """
        candidate = MappingRow(
            public_port=public_port
            if public_port is not None
            else self._next_free_public_port(DEFAULT_PUBLIC_PORT_BASE),
            local_port=local_port if local_port is not None else self._default_local_port,
            host=host,
            local_host=local_host if local_host is not None else self._default_local_host,
            remark=remark,
            tls=tls,
        )
        self._commit(self._rows + [candidate])
        return len(self._rows) - 1

    def update_row(self, index: int, **fields: Any) -> int:
        """修改一行并返回它的新位置。

        ``public_port`` 是行的身份，改它相当于换了一行——返回的 index 会跟着更新，
        调用方必须用它来重选表格行，否则光标会停在错误的位置上。
        """
        row = self.row(index)
        if row is None:
            raise ConfigError(f"行号 {index} 越界，当前共 {len(self._rows)} 行")
        unknown = sorted(set(fields) - set(MappingRow.FIELDS))
        if unknown:
            raise ConfigError(f"不支持修改的字段：{unknown}")
        candidate = self._rows.copy()
        candidate[index] = row.with_changes(**fields)
        self._commit(candidate)
        return self.index_of(candidate[index].public_port)

    def remove_row(self, index: int) -> MappingRow:
        """删除一行。删掉最后一行会被拒绝——空映射表对服务端无意义，
        服务端也会以同样的理由拒绝（同一份校验）。"""
        row = self.row(index)
        if row is None:
            raise ConfigError(f"行号 {index} 越界，当前共 {len(self._rows)} 行")
        candidate = self._rows.copy()
        del candidate[index]
        self._commit(candidate)
        return row

    def duplicate_row(self, index: int) -> int:
        """复制一行（自动换一个空闲公网端口），返回新行位置。"""
        row = self.row(index)
        if row is None:
            raise ConfigError(f"行号 {index} 越界，当前共 {len(self._rows)} 行")
        clone = row.with_changes(public_port=self._next_free_public_port(row.public_port + 1))
        self._commit(self._rows + [clone])
        return len(self._rows) - 1

    def replace_all(self, rows: Sequence[MappingRow]) -> None:
        """整体替换工作副本（校验通过才生效）。"""
        self._commit(list(rows))

    def reset(self) -> None:
        """放弃修改：工作副本回到服务端快照。"""
        self._rows = list(self._snapshot)
        self._remote_stale = False

    # ------------------------------------------------------------------ #
    # 与服务端同步
    # ------------------------------------------------------------------ #

    def load_remote(self, mapping: Optional[Sequence[Any]], *, force: bool = False) -> bool:
        """接收服务端下发的映射表。

        返回 ``True`` 表示工作副本已被服务端版本刷新；``False`` 表示用户有未提交的
        改动，**保留用户的输入**（只更新快照并置 :attr:`remote_stale`）。

        静默覆盖用户正在编辑的内容是最伤人的交互之一——他可能刚敲完十条映射，
        被另一个客户端的广播冲掉。所以这里选择"宁可提示，不可覆盖"。
        """
        dirty_before = self.is_dirty
        incoming: List[MappingRow] = []
        for index, item in enumerate(mapping or ()):
            if not isinstance(item, dict):
                continue
            try:
                incoming.append(MappingRow.from_dict(item, f"mapping[{index}]"))
            except ConfigError:
                # 服务端下发的数据本应合法；真遇到脏数据就跳过，不让界面崩掉
                continue
        incoming.sort(key=lambda row: row.public_port)
        self._snapshot = incoming

        if force or not dirty_before:
            self._rows = list(incoming)
            self._remote_stale = False
            return True

        self._remote_stale = True
        return False

    def mark_submitted(self) -> None:
        """提交成功后调用：当前工作副本成为新的"已保存"版本。"""
        self._snapshot = list(self._rows)
        self._remote_stale = False

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _commit(self, candidate: List[MappingRow]) -> None:
        """校验候选状态，通过后一次性写入。失败时抛异常且不动现有数据。"""
        self._validate(candidate)
        self._rows = candidate

    @staticmethod
    def _validate(rows: Sequence[MappingRow]) -> None:
        """复用服务端准入校验：同一份规则，两个调用方。"""
        parse_mapping([row.to_dict() for row in rows])

    def _next_free_public_port(self, start: int) -> int:
        used = {row.public_port for row in self._rows}
        port = max(start, 1)
        while port in used and port < 65535:
            port += 1
        return port

    def next_free_public_port(self, start: int = DEFAULT_PUBLIC_PORT_BASE) -> int:
        """在当前表里挑一个没被占用的公网端口。界面"新增"按钮用它填默认值。"""
        return self._next_free_public_port(start)


# --------------------------------------------------------------------------- #
# 界面状态
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LogEntry:
    """日志面板的一行。"""

    ts: str
    level: str  # ok / info / warn / error
    text: str


@dataclass(frozen=True)
class GuiState:
    """界面状态。``snapshot`` 就是 :meth:`TunnelClient.snapshot` 的返回值。"""

    snapshot: Dict[str, Any] = field(default_factory=dict)
    log: Tuple[LogEntry, ...] = ()
    log_total: int = 0
    """**累计**产生过多少条日志（不随环形截断回退）。

    界面靠它算出"这次要新画几条"。若改用 ``len(log)``，日志一旦达到上限
    就再也涨不上去，表现为"面板超过 500 条后不再刷新"——而内容其实在滚动。
    """
    notice: str = ""
    fatal: bool = False
    last_diff: str = ""

    # ---- 便捷读取：只从 snapshot 里取，不做任何二次统计 ---- #

    @property
    def connection(self) -> str:
        return str(self.snapshot.get("state") or "idle")

    @property
    def client_id(self) -> str:
        return str(self.snapshot.get("client_id") or "")

    @property
    def server(self) -> str:
        return str(self.snapshot.get("server") or "")

    @property
    def local_ports(self) -> List[int]:
        return [port for port in self.snapshot.get("local_ports") or [] if isinstance(port, int)]

    @property
    def claimed(self) -> List[int]:
        return [port for port in self.snapshot.get("claimed") or [] if isinstance(port, int)]

    @property
    def conflicts(self) -> List[int]:
        return [port for port in self.snapshot.get("conflicts") or [] if isinstance(port, int)]

    @property
    def active_forwards(self) -> int:
        value = self.snapshot.get("active_forwards")
        return value if isinstance(value, int) else 0

    def stat(self, name: str) -> int:
        stats = self.snapshot.get("stats")
        if not isinstance(stats, Mapping):
            return 0
        value = stats.get(name)
        return value if isinstance(value, int) else 0

    @property
    def online(self) -> bool:
        return self.connection == "online"

    def status_line(self) -> str:
        """状态栏文本。"""
        parts = [
            f"状态 {self.connection}",
            f"客户端 {self.client_id or '-'}",
            f"服务端 {self.server or '-'}",
            f"认领 {_fmt_ports(self.claimed)}",
            f"活跃转发 {self.active_forwards}",
            f"请求 {self.stat('forwards_total')}（失败 {self.stat('forwards_failed')}）",
            f"上行 {_fmt_bytes(self.stat('bytes_upload'))} / 下行 {_fmt_bytes(self.stat('bytes_download'))}",
            f"重连 {self.stat('reconnects')}",
        ]
        if self.conflicts:
            parts.append(f"端口冲突 {_fmt_ports(self.conflicts)}")
        if self.fatal:
            parts.append("已停止重试（鉴权失败）")
        return " ｜ ".join(parts)


def _fmt_ports(ports: Sequence[int]) -> str:
    return ",".join(str(port) for port in ports) if ports else "无"


def _fmt_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


# --------------------------------------------------------------------------- #
# 事件 → 状态（纯函数）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Outcome:
    logs: Tuple[Tuple[str, str], ...] = ()
    notice: Optional[str] = None
    fatal: Optional[bool] = None


def _describe(claimed: Any, conflicts: Any) -> str:
    text = f"认领端口 {_fmt_ports([p for p in claimed or [] if isinstance(p, int)])}"
    conflicts_list = [p for p in conflicts or [] if isinstance(p, int)]
    if conflicts_list:
        text += f"，冲突被拒 {_fmt_ports(conflicts_list)}"
    return text


def _on_control_connected(payload: Mapping[str, Any]) -> _Outcome:
    host = payload.get("host", "?")
    port = payload.get("port", "?")
    return _Outcome(
        logs=(("ok", f"已连上控制通道 {host}:{port}"),),
        notice=f"控制通道已连接 {host}:{port}",
        fatal=False,
    )


def _on_client_registered(payload: Mapping[str, Any]) -> _Outcome:
    logs = [("ok", f"注册成功：{_describe(payload.get('claimed'), payload.get('conflicts'))}")]
    conflicts = [p for p in payload.get("conflicts") or [] if isinstance(p, int)]
    if conflicts:
        logs.append(("warn", f"以下端口已被其他客户端占用，本次未认领：{_fmt_ports(conflicts)}"))
    return _Outcome(logs=tuple(logs), notice=f"在线：{_describe(payload.get('claimed'), payload.get('conflicts'))}")


def _on_reconnecting(payload: Mapping[str, Any]) -> _Outcome:
    reason = payload.get("reason") or "未知原因"
    delay = payload.get("delay")
    attempt = payload.get("attempt")
    return _Outcome(
        logs=(("warn", f"控制连接不可用（{reason}），{delay}s 后第 {attempt} 次重连"),),
        notice=f"重连中（第 {attempt} 次，{delay}s 后）",
    )


def _on_control_lost(payload: Mapping[str, Any]) -> _Outcome:
    reason = payload.get("reason") or "未知原因"
    fatal = bool(payload.get("fatal"))
    if fatal:
        return _Outcome(
            logs=(("error", f"注册被永久拒绝：{reason}；已停止重试，请检查令牌后重启"),),
            notice="已停止重试（鉴权失败）",
            fatal=True,
        )
    return _Outcome(logs=(("error", f"控制连接断开：{reason}"),), notice=f"连接断开：{reason}")


def _on_mapping_changed(payload: Mapping[str, Any]) -> _Outcome:
    mapping = [item for item in payload.get("mapping") or [] if isinstance(item, dict)]
    return _Outcome(
        logs=(("info", f"服务端映射表更新（{len(mapping)} 条）：{describe_mapping(mapping)}"),),
    )


def _on_request_end(payload: Mapping[str, Any]) -> _Outcome:
    """成功请求不写日志——隧道跑起来后每次访问都记一行会把面板刷爆。

    成功的部分由状态栏的计数与字节数体现（来自 ``snapshot()``），
    日志只留**失败**这种需要人看一眼的信息。"""
    if payload.get("ok"):
        return _Outcome()
    reason = payload.get("reason") or "未知原因"
    local_port = payload.get("local_port")
    return _Outcome(logs=(("warn", f"转发失败（内网端口 {local_port}）：{reason}"),))


def _on_conn_error(payload: Mapping[str, Any]) -> _Outcome:
    reason = payload.get("reason") or "未知原因"
    return _Outcome(
        logs=(("warn", f"客户端上报连接错误：{reason}"),),
        notice=str(reason),
    )


_EVENT_HANDLERS = {
    EventType.CONTROL_CONNECTED: _on_control_connected,
    EventType.CLIENT_REGISTERED: _on_client_registered,
    EventType.RECONNECTING: _on_reconnecting,
    EventType.CONTROL_LOST: _on_control_lost,
    EventType.MAPPING_CHANGED: _on_mapping_changed,
    EventType.REQUEST_END: _on_request_end,
    EventType.CONN_ERROR: _on_conn_error,
}
"""只登记**需要在界面上留下痕迹**的事件。

其余事件（``REQUEST_START``、``DATA_CHANNEL_OPENED`` 等）每次请求都会触发，
它们的价值已经被 ``snapshot()`` 的计数覆盖，记日志只会淹没真正重要的信息。
没有登记的事件不报错，直接忽略——保证脚本里新增事件不会把 GUI 搞崩。"""


def _append(state: GuiState, entries: Sequence[LogEntry]) -> GuiState:
    """追加日志并做环形截断，同时推进累计计数。"""
    if not entries:
        return state
    return replace(
        state,
        log=(state.log + tuple(entries))[-MAX_LOG_ENTRIES:],
        log_total=state.log_total + len(entries),
    )


def apply_event(state: GuiState, event: str, **payload: Any) -> GuiState:
    """把一个事件折叠进界面状态。未知事件原样返回。"""
    handler = _EVENT_HANDLERS.get(event)
    if handler is None:
        return state

    outcome = handler(payload)
    result = state
    if outcome.logs:
        result = _append(
            result,
            [LogEntry(ts=_now(), level=level, text=text) for level, text in outcome.logs],
        )
    if outcome.notice is not None:
        result = replace(result, notice=outcome.notice)
    if outcome.fatal is not None:
        result = replace(result, fatal=outcome.fatal)
    return result


def adopt_snapshot(state: GuiState, snapshot: Mapping[str, Any]) -> GuiState:
    """把 :meth:`TunnelClient.snapshot` 的结果并入界面状态。"""
    return replace(state, snapshot=dict(snapshot))


def append_log(state: GuiState, level: str, text: str, *, notice: Optional[str] = None) -> GuiState:
    """记一条**界面自己产生**的日志（按钮点错了、提交被本地校验拦下……）。

    隧道事件之外的信息也要有地方落，否则用户点了按钮没反应会以为界面坏了。
    """
    result = _append(state, [LogEntry(ts=_now(), level=level, text=text)])
    return replace(result, notice=text) if notice is not None else result


def note_mapping_result(state: GuiState, result: Mapping[str, Any]) -> GuiState:
    """把 ``mapping_result`` 回执写进日志与提示。回执原文来自服务端，不在这里改写。"""
    ok = bool(result.get("ok"))
    msg = str(result.get("msg") or ("已提交" if ok else "提交失败"))
    level = "ok" if ok else "error"
    updated = _append(state, [LogEntry(ts=_now(), level=level, text=f"映射提交{'成功' if ok else '失败'}：{msg}")])
    return replace(
        updated,
        notice=f"映射{'已生效' if ok else '提交失败'}：{msg}",
        last_diff=msg,
    )


def _now() -> str:
    return time.strftime("%H:%M:%S")
