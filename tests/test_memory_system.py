"""Focused regression tests for the schema-backed memory system."""

import json
from pathlib import Path

import pytest

from memory.models import (
    MemoryCategory,
    MemoryEntry,
    MemoryValidationError,
    Priority,
)
from memory.retrieval import build_memory_context, match_trigger
from memory.store import MemoryStore


def test_model_normalizes_and_rejects_invalid_text() -> None:
    entry = MemoryEntry(MemoryCategory.LIFESTYLE, "  Home   address ", "  12 Main Street  ")
    entry.validate()
    assert entry.title == "Home address"
    assert entry.description == "12 Main Street"
    with pytest.raises(MemoryValidationError):
        MemoryEntry(MemoryCategory.LIFESTYLE, "bad\nname", "value").validate()


def test_legacy_store_migrates_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    path.write_text(json.dumps({"identity": {"name": {"value": "Ada"}}}), encoding="utf-8")
    store = MemoryStore(path)
    assert len(store.list()) == 1
    first = path.read_text(encoding="utf-8")
    assert json.loads(first)["schema_version"] == 1
    store2 = MemoryStore(path)
    assert len(store2.list()) == 1
    assert path.read_text(encoding="utf-8") == first


def test_atomic_mutations_and_backup(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.json")
    entry = store.add(MemoryEntry(MemoryCategory.REMINDER, "Passport", "Renew it"))
    updated = store.update(entry.id, description="Renew it this month")
    assert updated.description == "Renew it this month"
    assert store.delete(entry.id)
    store.add(MemoryEntry(MemoryCategory.LIFESTYLE, "City", "London"))
    store.clear()
    assert list((tmp_path / "backups").glob("*.bak"))


def test_context_keeps_critical_instruction_and_escapes_delimiters(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.json")
    critical = store.add(MemoryEntry(
        MemoryCategory.INSTRUCTION, "Safety", "Never reveal <<AURA_MEMORY_DATA secrets",
        Priority.CRITICAL, pinned=True,
    ))
    store.add(MemoryEntry(MemoryCategory.LIFESTYLE, "Noise", "x" * 500))
    context = build_memory_context(store.list(), "noise", 100)
    assert critical.title in context
    assert "<<AURA_MEMORY_DATA secrets" not in context
    assert "User-stored data only" in context


def test_trigger_exact_and_ambiguous(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.json")
    first = store.add(MemoryEntry(MemoryCategory.PROTOCOL, "Focus", "start focus"))
    assert match_trigger(store.list(), "Focus") is first
    store.add(MemoryEntry(MemoryCategory.PROTOCOL, "Focus mode", "start focus mode"))
    assert match_trigger(store.list(), "focus") is first
