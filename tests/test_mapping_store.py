# -*- coding: utf-8 -*-
"""
tests/test_mapping_store.py —— 映射持久化
==========================================
覆盖三件事，每件都对应一条硬约定：

1. **写盘**：内容能来回、同卷原子替换、不留临时文件、目录自动创建。
2. **坏文件 fail fast**：内容损坏时抛 ``ConfigError`` 并给出恢复路径，
   绝不静默退回内存（那会让运维以为持久化在工作，实际每次重启都丢）。
3. **写盘失败降级**：磁盘/权限问题只记日志、内存态继续生效，
   不因为"写不进文件"就让一次映射变更整体失败。

最后两条是针对 ``MappingManager.start`` 的 **seed 语义**：
store 里已有内容时以 store 为准，配置文件里的 mapping 只当首次种子。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Dict, List

import pytest

from config import ConfigError, MappingRule, MappingStoreConfig
from localtonet.server.mapping import (
    DEFAULT_MAPPING_FILE,
    FileMappingStore,
    InMemoryMappingStore,
    MappingManager,
    build_mapping_store,
)
from tests.helpers import free_ports


def rule(public_port: int, local_port: int = 8000) -> MappingRule:
    # host 固定 127.0.0.1：测试里不监听 0.0.0.0，免得触发 Windows 防火墙弹窗
    return MappingRule(public_port=public_port, local_port=local_port, host="127.0.0.1")


def write_raw(path: Path, raw: str) -> Path:
    path.write_text(raw, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #


def test_missing_file_starts_empty(tmp_path: Path) -> None:
    store = FileMappingStore(tmp_path / "mappings.json")
    assert store.all() == []
    assert store.get(9028) is None
    assert store.persist_error == ""


def test_empty_file_is_treated_as_empty(tmp_path: Path) -> None:
    path = write_raw(tmp_path / "mappings.json", "   \n")
    assert FileMappingStore(path).all() == []


def test_replace_persists_and_reloads(tmp_path: Path) -> None:
    path = tmp_path / "mappings.json"
    FileMappingStore(path).replace([rule(9028), rule(9030, 8080)])

    reloaded = FileMappingStore(path)
    assert [item.public_port for item in reloaded.all()] == [9028, 9030]
    assert reloaded.get(9030) is not None
    assert reloaded.get(9030).local_port == 8080  # type: ignore[union-attr]


def test_persisted_content_is_readable_json(tmp_path: Path) -> None:
    path = tmp_path / "mappings.json"
    FileMappingStore(path).replace([rule(9028, 8000)])

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == [rule(9028, 8000).to_dict()]


def test_replace_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    path = tmp_path / "mappings.json"
    store = FileMappingStore(path)
    store.replace([rule(9028)])
    store.replace([rule(9030)])

    assert path.is_file()
    assert [item.name for item in tmp_path.iterdir()] == [path.name]


def test_parent_directory_is_created(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "mappings.json"
    FileMappingStore(path).replace([rule(9028)])

    assert path.is_file()
    assert [item.public_port for item in FileMappingStore(path).all()] == [9028]


def test_write_failure_degrades_instead_of_raising(tmp_path: Path) -> None:
    """父目录是个**文件**时 ``mkdir`` 必然失败——写盘要降级，不能把映射变更拖下水。"""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("我是文件，不是目录", encoding="utf-8")
    store = FileMappingStore(blocker / "sub" / "mappings.json")

    store.replace([rule(9028)])  # 不该抛异常

    assert store.persist_error != ""
    assert [item.public_port for item in store.all()] == [9028]
    # 内存态继续可用，后续变更照样生效
    store.replace([rule(9030)])
    assert [item.public_port for item in store.all()] == [9030]


def test_successful_write_clears_previous_error(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    broken = FileMappingStore(blocker / "sub" / "mappings.json")
    broken.replace([rule(9028)])
    assert broken.persist_error != ""

    healthy = FileMappingStore(tmp_path / "ok.json")
    healthy.replace([rule(9028)])
    assert healthy.persist_error == ""


# --------------------------------------------------------------------------- #
# 坏文件：fail fast
# --------------------------------------------------------------------------- #


def test_corrupt_json_is_rejected_with_recovery_hint(tmp_path: Path) -> None:
    path = write_raw(tmp_path / "mappings.json", "{ 这不是 JSON")

    with pytest.raises(ConfigError) as excinfo:
        FileMappingStore(path)

    message = str(excinfo.value)
    assert "不是合法 JSON" in message
    assert "memory" in message, "报错必须给出恢复路径（删文件或改回 memory 模式）"


def test_non_array_toplevel_is_rejected(tmp_path: Path) -> None:
    path = write_raw(tmp_path / "mappings.json", '{"public_port": 9028}')

    with pytest.raises(ConfigError, match="顶层必须是数组"):
        FileMappingStore(path)


def test_item_must_be_object(tmp_path: Path) -> None:
    path = write_raw(tmp_path / "mappings.json", '[123]')

    with pytest.raises(ConfigError, match="必须是对象"):
        FileMappingStore(path)


def test_duplicate_public_port_in_file_is_rejected(tmp_path: Path) -> None:
    path = write_raw(
        tmp_path / "mappings.json",
        json.dumps([{"public_port": 9028, "local_port": 8000}, {"public_port": 9028, "local_port": 8001}]),
    )

    with pytest.raises(ConfigError, match="public_port=9028 重复"):
        FileMappingStore(path)


def test_invalid_rule_in_file_is_rejected(tmp_path: Path) -> None:
    path = write_raw(tmp_path / "mappings.json", json.dumps([{"public_port": 70000, "local_port": 8000}]))

    with pytest.raises(ConfigError, match="超出合法范围"):
        FileMappingStore(path)


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #


def test_factory_selects_memory_by_default() -> None:
    store = build_mapping_store(MappingStoreConfig())
    assert isinstance(store, InMemoryMappingStore)


def test_factory_builds_file_store(tmp_path: Path) -> None:
    store = build_mapping_store(MappingStoreConfig(type="file", path=str(tmp_path / "m.json")))
    assert isinstance(store, FileMappingStore)
    assert store.path == tmp_path / "m.json"


def test_factory_falls_back_to_default_filename() -> None:
    """``type=file`` 却没给路径时用默认文件名兜底，而不是拿到空路径。"""
    store = build_mapping_store(MappingStoreConfig(type="file"))
    assert isinstance(store, FileMappingStore)
    assert store.path == Path(DEFAULT_MAPPING_FILE)


# --------------------------------------------------------------------------- #
# MappingManager.start 的 seed 语义
# --------------------------------------------------------------------------- #


async def _noop_visitor(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, public_port: int) -> None:
    writer.close()


class WarningRecorder:
    """只收集 warning 文本的假 logger。

    不依赖 caplog / 全局日志级别：``localtonet`` 这套 logger 的 ``propagate``
    会被 ``setup_logging`` 关掉，而其他测试可能已经调过它，用 caplog 断言会时灵时不灵。
    """

    def __init__(self) -> None:
        self.warnings: List[str] = []

    def warning(self, msg: str, *args: object, **kwargs: object) -> None:
        self.warnings.append(msg % args if args else msg)

    def __getattr__(self, name: str):
        """info / debug / error / exception 一律静默。"""
        return lambda *args, **kwargs: None


def _manager(store, recorder: WarningRecorder) -> MappingManager:
    return MappingManager(store=store, on_visitor=_noop_visitor, host="127.0.0.1", logger=recorder)


def test_start_seeds_when_store_is_empty(tmp_path: Path) -> None:
    """store 为空（首次启动）→ 用配置文件里的 mapping 播种，并立刻落盘。"""

    async def scenario() -> None:
        ports = free_ports(2)
        path = tmp_path / "mappings.json"
        store = FileMappingStore(path)
        manager = _manager(store, WarningRecorder())

        await manager.start([rule(ports[0])])
        try:
            assert manager.listen_ports() == [ports[0]]
            assert [item.public_port for item in FileMappingStore(path).all()] == [ports[0]]
        finally:
            await manager.stop()

    asyncio.run(scenario())


def test_start_prefers_persisted_rules_over_seed(tmp_path: Path) -> None:
    """重启场景：文件里已有映射，配置文件给的是另一个端口 → 以文件为准。"""

    async def scenario() -> None:
        persisted_port, seed_port = free_ports(2)
        path = tmp_path / "mappings.json"
        FileMappingStore(path).replace([rule(persisted_port)])

        recorder = WarningRecorder()
        manager = _manager(FileMappingStore(path), recorder)

        await manager.start([rule(seed_port)])
        try:
            assert manager.listen_ports() == [persisted_port]
            assert seed_port not in manager.listen_ports()
            assert recorder.warnings, "store 覆盖了配置文件时必须留 WARNING，否则使用者会以为配置没生效"
            assert "种子" in recorder.warnings[0]
        finally:
            await manager.stop()

    asyncio.run(scenario())


def test_start_does_not_warn_when_seed_matches_store(tmp_path: Path) -> None:
    """文件内容与配置一致（最常见的情况）时不要刷警告。"""

    async def scenario() -> None:
        port = free_ports(1)[0]
        path = tmp_path / "mappings.json"
        FileMappingStore(path).replace([rule(port)])

        recorder = WarningRecorder()
        manager = _manager(FileMappingStore(path), recorder)

        await manager.start([rule(port)])
        try:
            assert manager.listen_ports() == [port]
            assert recorder.warnings == []
        finally:
            await manager.stop()

    asyncio.run(scenario())


def test_memory_store_seeds_when_empty() -> None:
    """memory 模式（默认）启动时 store 总是空的，seed 照常生效——对外行为与改造前一致。"""

    async def scenario() -> None:
        port = free_ports(1)[0]
        manager = _manager(InMemoryMappingStore(), WarningRecorder())

        await manager.start([rule(port)])
        try:
            assert manager.listen_ports() == [port]
        finally:
            await manager.stop()

    asyncio.run(scenario())


def test_store_wins_regardless_of_implementation() -> None:
    """"store 优先"是**存储层**的语义，与具体实现无关：谁往里放了东西谁就说了算。

    memory 模式下这条平时看不见（每次进程启动都是新的空 store），
    但预填充过的 store 同样会压过 seed——规则只有一套，不做实现分支。
    """

    async def scenario() -> None:
        existing, seed = free_ports(2)
        store = InMemoryMappingStore([rule(existing)])
        manager = _manager(store, WarningRecorder())

        await manager.start([rule(seed)])
        try:
            assert manager.listen_ports() == [existing]
        finally:
            await manager.stop()

    asyncio.run(scenario())
