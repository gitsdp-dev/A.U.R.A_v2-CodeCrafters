"""Regression tests for comma-separated protocol commands."""

from pathlib import Path

from memory.models import MemoryCategory, MemoryEntry
from memory.protocols import normalize_protocol_command, split_protocol_tasks
from memory.retrieval import match_trigger
from memory.service import MemoryService
from memory.store import MemoryStore


def test_protocol_command_normalization_and_order() -> None:
    assert normalize_protocol_command("Start the workspace protocol") == "workspace protocol"
    assert split_protocol_tasks("Open VS Code, Open YouTube, Open Spotify") == [
        "Open VS Code", "Open YouTube", "Open Spotify"
    ]


def test_protocol_trigger_accepts_start_command(tmp_path: Path) -> None:
    entry = MemoryEntry(
        category=MemoryCategory.PROTOCOL,
        title="workspace protocol",
        description="Open VS Code, Open YouTube, Open Spotify",
    )
    store = MemoryStore(tmp_path / "memory.json")
    store.add(entry)
    assert match_trigger(store.list(), "start the workspace protocol") is entry
    assert match_trigger(store.list(), "workspace") is entry
    service = MemoryService(store)
    assert service.protocol_tasks(entry) == [
        "Open VS Code", "Open YouTube", "Open Spotify"
    ]


def test_store_instances_see_each_others_writes(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    assistant_store = MemoryStore(path)
    dialog_store = MemoryStore(path)
    assert dialog_store.add(MemoryEntry(
        MemoryCategory.PROTOCOL, "workspace protocol", "Open VS Code"
    ))
    assert [entry.title for entry in assistant_store.list(MemoryCategory.PROTOCOL)] == [
        "workspace protocol"
    ]


def test_arbitrary_protocol_names_resolve_without_hardcoding(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.json")
    morning = store.add(MemoryEntry(
        MemoryCategory.PROTOCOL, "morning routine",
        "Open Spotify, Open YouTube",
    ))
    focus = store.add(MemoryEntry(
        MemoryCategory.PROTOCOL, "focus mode",
        "Open VS Code, Wait 2 seconds",
    ))
    service = MemoryService(store)
    assert service.resolve_protocol("start the morning routine") is morning
    assert service.resolve_protocol("focus mode") is focus
    assert service.resolve_protocol("run focus") is focus
    assert service.resolve_protocol("please execute the focus mode!") is focus
    assert service.resolve_protocol("execute focus mode!") is focus
    assert "morning routine" in service.protocol_catalog()
