# -*- coding: utf-8 -*-
"""
tests/test_gui_model.py —— GUI 无头层的单元测试
================================================
被测对象是 :mod:`localtonet.gui.model` 与 :mod:`localtonet.gui.viewmodel`：
映射表编辑缓冲区、界面状态折叠、服务端推送与用户编辑的冲突策略。

**这里一个 tkinter 都不需要**——正是这个设计让"GUI 逻辑"能被自动化测试覆盖，
而不是只能靠人手点。
"""

from __future__ import annotations

import inspect
import sys

import pytest

from config import ConfigError
from localtonet.core.events import EventType
from localtonet.core.rules import parse_mapping
from localtonet.gui.model import (
    MAX_LOG_ENTRIES,
    GuiState,
    MappingRow,
    MappingTableModel,
    append_log,
    apply_event,
    note_mapping_result,
)
from localtonet.gui.viewmodel import REMOTE_STALE_NOTICE, GuiViewModel


def build_model(*, public_port: int = 9028, local_port: int = 8000) -> MappingTableModel:
    """构造一个"服务端已下发一条映射"的模型（干净状态）。"""
    model = MappingTableModel(default_local_port=local_port)
    model.load_remote([{"public_port": public_port, "local_port": local_port}])
    return model


# --------------------------------------------------------------------------- #
# MappingRow
# --------------------------------------------------------------------------- #


def test_row_round_trip_matches_config_rule() -> None:
    raw = {"public_port": 9028, "local_port": 8000, "host": "127.0.0.1", "remark": "web"}
    row = MappingRow.from_dict(raw)
    assert row.to_dict() == {**raw, "local_host": "127.0.0.1"}
    assert row.to_rule().to_dict() == row.to_dict()


def test_row_as_cells_are_all_strings() -> None:
    cells = MappingRow(9028, 8000).as_cells()
    assert cells == ("9028", "8000", "0.0.0.0", "127.0.0.1", "")
    assert all(isinstance(cell, str) for cell in cells)


def test_row_fields_is_not_a_data_field() -> None:
    """``FIELDS`` 必须是 ClassVar，否则会混进行对象的初始化与 ``repr``。"""
    assert "FIELDS" not in inspect.signature(MappingRow).parameters
    assert "FIELDS" not in repr(MappingRow(9028, 8000))


def test_row_signature_covers_every_field() -> None:
    row = MappingRow(9028, 8000)
    assert row.signature() != row.with_changes(remark="备注").signature()
    assert row.signature() != row.with_changes(host="127.0.0.1").signature()


# --------------------------------------------------------------------------- #
# 编辑：增删改查 + 即时校验
# --------------------------------------------------------------------------- #


def test_add_row_auto_picks_free_public_port() -> None:
    model = build_model()
    first = model.add_row()
    second = model.add_row()
    assert model.row(first).public_port == 9000
    assert model.row(second).public_port == 9001  # 连点两次不会撞重复规则


def test_update_row_returns_new_index_when_identity_changes() -> None:
    model = build_model(public_port=9028)
    model.add_row(public_port=9030)
    moved = model.update_row(1, public_port=9040)
    assert moved == 1
    assert model.row(1).public_port == 9040
    assert model.index_of(9028) == 0


def test_update_row_rejects_duplicate_public_port_without_mutating() -> None:
    model = build_model(public_port=9028)
    model.add_row(public_port=9030, local_port=8080)
    with pytest.raises(ConfigError, match="重复"):
        model.update_row(1, public_port=9028)
    assert [row.public_port for row in model.rows] == [9028, 9030]


def test_update_row_rejects_bool_port() -> None:
    """``bool`` 是 ``int`` 子类，必须显式挡掉，否则 ``True`` 会变成端口 1。"""
    model = build_model()
    with pytest.raises(ConfigError, match="必须是整数"):
        model.update_row(0, public_port=True)


def test_update_row_rejects_unknown_field() -> None:
    model = build_model()
    with pytest.raises(ConfigError, match="不支持修改的字段"):
        model.update_row(0, secret="x")


def test_update_row_out_of_range() -> None:
    model = build_model()
    with pytest.raises(ConfigError, match="越界"):
        model.update_row(5, remark="x")


def test_remove_last_row_rejected_like_server() -> None:
    """空映射表服务端也拒绝——两边用的是同一个函数，所以行为必然一致。"""
    model = build_model()
    with pytest.raises(ConfigError, match="不能为空"):
        model.remove_row(0)
    assert model.row_count == 1


def test_duplicate_row_uses_free_public_port() -> None:
    model = build_model(public_port=9028)
    index = model.duplicate_row(0)
    assert model.row(index).public_port == 9029
    assert model.row(index).local_port == 8000


def test_model_reuses_server_validation_verbatim() -> None:
    """GUI 的校验必须是服务端那一个函数，不是"看起来一样"的另一套。"""
    model = MappingTableModel()
    model.load_remote([{"public_port": 9028, "local_port": 8000}])

    with pytest.raises(ConfigError) as gui_exc:
        model.update_row(0, public_port=0)
    with pytest.raises(ConfigError) as server_exc:
        parse_mapping([{"public_port": 0, "local_port": 8000}])
    assert str(gui_exc.value) == str(server_exc.value)


# --------------------------------------------------------------------------- #
# 脏标记与派生信息
# --------------------------------------------------------------------------- #


def test_dirty_flag_tracks_edits_and_remark() -> None:
    model = build_model()
    assert not model.is_dirty
    model.update_row(0, remark="只改备注也算改过")
    assert model.is_dirty
    assert model.dirty_indexes() == [0]


def test_dirty_indexes_ignores_untouched_rows() -> None:
    model = build_model(public_port=9028)
    model.add_row(public_port=9030)
    model.mark_submitted()
    rows = model.rows
    model.replace_all([rows[0].with_changes(remark="改过的"), rows[1]])
    assert model.dirty_indexes() == [0]


def test_removed_ports_lists_server_only_ports() -> None:
    """用户删掉的端口要能被识别出来：提交时服务端会关掉它们对应的监听。"""
    model = MappingTableModel()
    model.load_remote(
        [
            {"public_port": 9028, "local_port": 8000},
            {"public_port": 9030, "local_port": 8000},
        ]
    )
    assert model.removed_ports() == []

    model.remove_row(1)

    assert model.removed_ports() == [9030]
    assert model.is_dirty


def test_local_ports_are_deduped_and_sorted() -> None:
    model = MappingTableModel()
    model.load_remote(
        [
            {"public_port": 9030, "local_port": 8080},
            {"public_port": 9028, "local_port": 8000},
            {"public_port": 9029, "local_port": 8080},
        ]
    )
    assert model.local_ports() == [8000, 8080]


def test_rules_validates_whole_table() -> None:
    model = MappingTableModel()
    model.load_remote([{"public_port": 9028, "local_port": 8000}])
    assert [rule.public_port for rule in model.rules()] == [9028]


# --------------------------------------------------------------------------- #
# 与服务端同步：绝不静默覆盖用户输入
# --------------------------------------------------------------------------- #


def test_load_remote_adopts_when_clean() -> None:
    model = MappingTableModel()
    assert model.load_remote([{"public_port": 9028, "local_port": 8000}]) is True
    assert [row.public_port for row in model.rows] == [9028]
    assert not model.remote_stale


def test_load_remote_keeps_user_edits_when_dirty() -> None:
    model = build_model(public_port=9028)
    model.add_row(public_port=9030)

    adopted = model.load_remote([{"public_port": 9028, "local_port": 8000, "remark": "服务端改的"}])

    assert adopted is False
    assert model.remote_stale is True
    # 用户新增的那一行必须还在
    assert [row.public_port for row in model.rows] == [9028, 9030]
    # 快照已经换成服务端版本
    assert [row.public_port for row in model.snapshot] == [9028]


def test_load_remote_force_overrides_user_edits() -> None:
    model = build_model(public_port=9028)
    model.add_row(public_port=9030)
    assert model.load_remote([{"public_port": 9028, "local_port": 8000}], force=True) is True
    assert [row.public_port for row in model.rows] == [9028]


def test_load_remote_skips_malformed_entries() -> None:
    model = MappingTableModel()
    model.load_remote(["不是对象", {"public_port": "x"}, {"public_port": 9028, "local_port": 8000}])
    assert [row.public_port for row in model.rows] == [9028]


def test_load_remote_accepts_empty_and_none() -> None:
    model = build_model()
    assert model.load_remote(None) is True
    assert model.row_count == 0


def test_reset_discards_edits() -> None:
    model = build_model(public_port=9028)
    model.add_row(public_port=9030)
    model.load_remote([{"public_port": 9028, "local_port": 8000}])
    assert model.remote_stale

    model.reset()

    assert [row.public_port for row in model.rows] == [9028]
    assert not model.is_dirty
    assert not model.remote_stale


def test_mark_submitted_makes_working_copy_the_baseline() -> None:
    model = build_model(public_port=9028)
    model.add_row(public_port=9030)
    assert model.is_dirty

    model.mark_submitted()

    assert not model.is_dirty
    assert not model.remote_stale
    assert [row.public_port for row in model.snapshot] == [9028, 9030]


# --------------------------------------------------------------------------- #
# 事件 → 状态
# --------------------------------------------------------------------------- #


def test_unknown_event_is_ignored() -> None:
    state = GuiState(notice="原样保留")
    assert apply_event(state, "某个未来才有的事件", x=1) is state


def test_control_lost_fatal_is_sticky_until_reconnect() -> None:
    state = GuiState()
    state = apply_event(state, EventType.CONTROL_LOST, reason="令牌错误", fatal=True)
    assert state.fatal is True
    assert state.log[-1].level == "error"
    assert "已停止重试" in state.notice

    state = apply_event(state, EventType.CONTROL_CONNECTED, host="1.2.3.4", port=7000)
    assert state.fatal is False


def test_reconnecting_records_attempt_and_delay() -> None:
    state = apply_event(GuiState(), EventType.RECONNECTING, reason="连接失败", delay=2.0, attempt=3)
    assert state.log[-1].level == "warn"
    assert "第 3 次" in state.log[-1].text
    assert "2.0s" in state.notice


def test_successful_request_is_not_logged() -> None:
    """隧道跑起来后成功请求极多，逐条记日志会把面板刷爆——由计数与字节数体现。"""
    state = GuiState()
    assert apply_event(state, EventType.REQUEST_END, ok=True, upload=100, download=200) is state
    assert state.log == ()


def test_failed_request_is_logged() -> None:
    state = apply_event(
        GuiState(), EventType.REQUEST_END, ok=False, reason="连接被拒绝", local_port=8000
    )
    assert state.log[-1].level == "warn"
    assert "8000" in state.log[-1].text


def test_client_registered_reports_conflicts() -> None:
    state = apply_event(
        GuiState(),
        EventType.CLIENT_REGISTERED,
        client_id="pc-1",
        claimed=[8000],
        conflicts=[8080],
    )
    assert [entry.level for entry in state.log] == ["ok", "warn"]
    assert "8080" in state.log[-1].text


def test_mapping_changed_is_logged_with_ports() -> None:
    state = apply_event(
        GuiState(),
        EventType.MAPPING_CHANGED,
        mapping=[{"public_port": 9028, "local_port": 8000}],
    )
    assert "9028->8000" in state.log[-1].text


def test_log_is_truncated_to_ring_size() -> None:
    state = GuiState()
    for _ in range(MAX_LOG_ENTRIES + 20):
        state = apply_event(state, EventType.REQUEST_END, ok=False, reason="x")
    assert len(state.log) == MAX_LOG_ENTRIES


def test_log_total_keeps_growing_past_ring_size() -> None:
    """累计条数不能跟着环形截断一起回退，否则日志面板到上限后就不刷新了。"""
    state = GuiState()
    for _ in range(MAX_LOG_ENTRIES + 20):
        state = apply_event(state, EventType.REQUEST_END, ok=False, reason="x")
    assert state.log_total == MAX_LOG_ENTRIES + 20


def test_append_log_advances_total_and_sets_notice_only_when_asked() -> None:
    state = append_log(GuiState(), "info", "只是记一条")
    assert (state.log_total, state.notice) == (1, "")

    state = append_log(state, "warn", "端口重复", notice="端口重复")
    assert (state.log_total, state.notice) == (2, "端口重复")
    assert state.log[-1].level == "warn"


def test_note_mapping_result_ok_and_fail() -> None:
    ok_state = note_mapping_result(GuiState(), {"ok": True, "msg": "新增 9028"})
    assert ok_state.log[-1].level == "ok"
    assert ok_state.last_diff == "新增 9028"

    fail_state = note_mapping_result(GuiState(), {"ok": False, "msg": "mapping 不能为空"})
    assert fail_state.log[-1].level == "error"
    assert "不能为空" in fail_state.notice


def test_status_line_shows_ports_and_conflicts() -> None:
    snapshot = {
        "client_id": "pc-1",
        "state": "online",
        "server": "127.0.0.1:7000",
        "local_ports": [8000],
        "claimed": [8000],
        "conflicts": [8080],
        "active_forwards": 2,
        "stats": {"forwards_total": 5, "forwards_failed": 1, "bytes_upload": 2048, "bytes_download": 0},
    }
    line = GuiState(snapshot=snapshot).status_line()
    assert "online" in line
    assert "2.0KB" in line
    assert "端口冲突 8080" in line


def test_status_line_tolerates_missing_stats() -> None:
    assert "状态 idle" in GuiState().status_line()


# --------------------------------------------------------------------------- #
# ViewModel
# --------------------------------------------------------------------------- #


def test_viewmodel_adopts_snapshot() -> None:
    vm = GuiViewModel()
    assert vm.apply(("snapshot", {"snapshot": {"state": "online", "client_id": "pc-1"}})) is True
    assert vm.state.connection == "online"
    assert vm.state.online


def test_viewmodel_mapping_changed_updates_table() -> None:
    vm = GuiViewModel()
    vm.apply(("event", {"name": EventType.MAPPING_CHANGED, "payload": {"mapping": [{"public_port": 9028, "local_port": 8000}]}}))
    assert [row.public_port for row in vm.table.rows] == [9028]
    assert not vm.table.is_dirty


def test_viewmodel_keeps_user_edits_but_notices_remote_change() -> None:
    vm = GuiViewModel()
    vm.apply(("event", {"name": EventType.MAPPING_CHANGED, "payload": {"mapping": [{"public_port": 9028, "local_port": 8000}]}}))
    vm.table.add_row(public_port=9030)

    vm.apply(("event", {"name": EventType.MAPPING_CHANGED, "payload": {"mapping": [{"public_port": 9028, "local_port": 8000}]}}))

    assert [row.public_port for row in vm.table.rows] == [9028, 9030]
    assert vm.notice == REMOTE_STALE_NOTICE


def test_viewmodel_mapping_result_marks_submitted() -> None:
    vm = GuiViewModel()
    vm.table.load_remote([{"public_port": 9028, "local_port": 8000}])
    vm.table.add_row(public_port=9030)
    assert vm.table.is_dirty

    vm.apply(("mapping_result", {"result": {"ok": True, "msg": "新增 9030"}}))

    assert not vm.table.is_dirty
    assert "新增 9030" in vm.state.notice


def test_viewmodel_mapping_result_failure_keeps_dirty() -> None:
    vm = GuiViewModel()
    vm.table.load_remote([{"public_port": 9028, "local_port": 8000}])
    vm.table.add_row(public_port=9030)

    vm.apply(("mapping_result", {"result": {"ok": False, "msg": "mapping 不能为空"}}))

    assert vm.table.is_dirty  # 失败不能把用户的输入当成已保存
    assert "提交失败" in vm.state.notice


def test_viewmodel_ignores_unknown_kind_and_malformed_payload() -> None:
    vm = GuiViewModel()
    assert vm.apply(("莫名其妙", {})) is False
    assert vm.apply(("snapshot", {"snapshot": "不是字典"})) is False
    assert vm.apply(("event", {"payload": {}})) is False


def test_viewmodel_handles_interface_local_notice() -> None:
    """界面自己产生的提示（按钮点错了、本地校验拦下）也要落进日志。"""
    vm = GuiViewModel()
    assert vm.apply(("local", {"level": "error", "text": "新增失败：端口重复"})) is True
    assert vm.state.log[-1].level == "error"
    assert "端口重复" in vm.state.notice

    assert vm.apply(("local", {"text": ""})) is False


def test_viewmodel_apply_all_reports_change() -> None:
    vm = GuiViewModel()
    assert vm.apply_all([]) is False
    assert vm.apply_all([("snapshot", {"snapshot": {"state": "online"}})]) is True


# --------------------------------------------------------------------------- #
# 无界面可用的硬约束
# --------------------------------------------------------------------------- #


def test_gui_headless_modules_never_import_tkinter() -> None:
    """无头层不许把 tkinter 拉进来——否则无显示环境连测试都跑不了。

    断言"界面模块没有被 __init__ 顺手导入"，这才是那条 import 边界本身。
    """
    assert "localtonet.gui.app" not in sys.modules
    assert "tkinter" not in sys.modules
