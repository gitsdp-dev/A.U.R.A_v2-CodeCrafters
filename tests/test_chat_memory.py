"""
tests/test_chat_memory.py — Unit tests for Conversation Recall (Phase 2).
"""

import time
from datetime import datetime, timedelta
import pytest

from aura_chat_memory import (
    ChatMemoryManager,
    parse_date_range,
    sanitize_turn_text,
    match_conversation_recall_intent,
)


def test_sanitize_turn_text():
    # Credentials and secrets should be redacted
    text1 = "My password is password=Secret123! and my pin is pin: 4920"
    sanitized1 = sanitize_turn_text(text1)
    assert "[REDACTED SECRETS]" in sanitized1
    assert "Secret123!" not in sanitized1

    # API keys
    text2 = "Here is my key: sk-abcdef123456789012345678"
    sanitized2 = sanitize_turn_text(text2)
    assert "[REDACTED SECRETS]" in sanitized2
    assert "sk-abcdef" not in sanitized2

    # Normal text should remain untouched
    normal = "What was that sci-fi book about the Martian astronaut?"
    assert sanitize_turn_text(normal) == normal


def test_parse_date_range():
    ref = datetime(2026, 11, 18, 14, 0, 0)

    # Yesterday
    y_range = parse_date_range("yesterday", now=ref)
    assert y_range is not None
    y_start, y_end = y_range
    assert datetime.fromtimestamp(y_start).day == 17
    assert datetime.fromtimestamp(y_end).day == 17

    # Two days ago
    d_range = parse_date_range("two days ago", now=ref)
    assert d_range is not None
    d_start, d_end = d_range
    assert datetime.fromtimestamp(d_start).day == 16

    # Last week
    w_range = parse_date_range("last week", now=ref)
    assert w_range is not None
    w_start, w_end = w_range
    assert (w_end - w_start) >= 6 * 86400

    # In March
    m_range = parse_date_range("in march", now=ref)
    assert m_range is not None
    m_start, m_end = m_range
    assert datetime.fromtimestamp(m_start).month == 3

    # On the 5th
    f_range = parse_date_range("on the 5th", now=ref)
    assert f_range is not None
    f_start, f_end = f_range
    assert datetime.fromtimestamp(f_start).day == 5


def test_match_conversation_recall_intent():
    res1 = match_conversation_recall_intent("What was that book I mentioned last week?")
    assert res1 is not None
    assert "book" in res1["query"]
    assert res1["date_range"] == "last week"

    res2 = match_conversation_recall_intent("What did we talk about yesterday?")
    assert res2 is not None
    assert res2["date_range"] == "yesterday"

    res3 = match_conversation_recall_intent("Search chat history for flight confirmation")
    assert res3 is not None
    assert "flight" in res3["query"]

    # Negative case
    assert match_conversation_recall_intent("Turn on the kitchen lights") is None
    assert match_conversation_recall_intent("What is the temperature in Tokyo?") is None


def test_chat_memory_manager_crud(tmp_path):
    db_file = tmp_path / "test_chat.db"
    cm = ChatMemoryManager(db_path=db_file)

    try:
        # 1. Log turns
        cm.log_turn("user", "I am reading Project Hail Mary by Andy Weir.", session_id="sess-1")
        cm.log_turn("assistant", "Project Hail Mary is a fantastic space novel!", session_id="sess-1")
        cm.log_turn("user", "My favorite programming language is Python.", session_id="sess-2")
        cm.log_turn("assistant", "Python is powerful and expressive.", session_id="sess-2")

        # 2. Search
        results = cm.search("Project Hail Mary", limit=5)
        assert len(results) >= 1
        assert any("Hail Mary" in r["content"] for r in results)

        results_py = cm.search("Python", limit=5)
        assert len(results_py) >= 1
        assert any("Python" in r["content"] for r in results_py)

        # 3. Formatted recall
        formatted = cm.recall_formatted("Hail Mary")
        assert "Found" in formatted
        assert "Project Hail Mary" in formatted

        # 4. Honest empty recall
        empty_res = cm.recall_formatted("Quantum teleportation algorithm")
        assert "No past conversation found" in empty_res

        # 5. Stats
        stats = cm.get_stats()
        assert stats["messages"] >= 4

        # 6. Delete all
        cm.delete_all()
        stats_after = cm.get_stats()
        assert stats_after["messages"] == 0

    finally:
        cm.close()
