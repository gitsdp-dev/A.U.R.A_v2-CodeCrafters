"""
tests/test_presence.py — Unit tests for Presence Awareness (Phase 1).
"""

import json
from datetime import datetime, timezone
from pathlib import Path
import pytest

from aura_presence import (
    QueueItem,
    match_presence_intent,
    group_queued_items,
    format_catchup_speech,
    format_catchup_details,
    PresenceManager,
)


def test_match_presence_intent():
    # User's exact prompt test cases
    assert match_presence_intent("AURA, I'm Heading Out") == "away"
    assert match_presence_intent("AURA, I'm leaving") == "away"
    assert match_presence_intent("AURA, brb") == "away"
    assert match_presence_intent("AURA, stepping out") == "away"
    assert match_presence_intent("AURA, gotta run") == "away"
    assert match_presence_intent("I'm Heading Out, Aura") == "away"
    assert match_presence_intent("Aura lock screen") == "away"
    assert match_presence_intent("AURA, I'm back") == "back"
    assert match_presence_intent("AURA, I have returned") == "back"
    assert match_presence_intent("AURA, back now") == "back"
    assert match_presence_intent("Hey Aura, unlock") == "back"
    assert match_presence_intent("Aura unlock screen") == "back"
    assert match_presence_intent("Aura what did i miss") == "read_details"

    # User's specified away phrases
    away_phrases = [
        "i'm heading out", "im heading out", "i am heading out", "heading out",
        "i'm leaving", "im leaving", "i am leaving", "leaving now",
        "going out", "brb", "be right back", "stepping out", "stepping away",
        "i have to go", "gotta run", "see you later"
    ]
    for p in away_phrases:
        assert match_presence_intent(p) == "away", f"Failed to match away phrase: {p}"
        assert match_presence_intent(f"Hey Aura, {p}!") == "away", f"Failed with prefix: {p}"

    # User's specified return phrases
    return_phrases = [
        "i'm back", "im back", "i am back", "i have returned",
        "back now", "just got back", "returned"
    ]
    for p in return_phrases:
        assert match_presence_intent(p) == "back", f"Failed to match return phrase: {p}"
        assert match_presence_intent(f"Aura, {p}!") == "back", f"Failed with prefix: {p}"

    # User's specified details phrases
    details_phrases = [
        "read details", "read the details", "what are the details",
        "read away details", "tell me details", "read catch up", "read catchup"
    ]
    for p in details_phrases:
        assert match_presence_intent(p) == "read_details", f"Failed to match details phrase: {p}"
        assert match_presence_intent(f"Can you {p}?") == "read_details", f"Failed with prefix: {p}"

    # Negative cases
    assert match_presence_intent("open calculator") is None
    assert match_presence_intent("what is the weather today") is None
    assert match_presence_intent("") is None
    assert match_presence_intent("i returned the library book") is None


def test_group_queued_items():
    items = [
        QueueItem("1", "2026-11-18T00:00:00Z", "Reminder", "Doctor appointment", "At 2 PM", 70, "reminder"),
        QueueItem("2", "2026-11-18T00:01:00Z", "Reminder", "Call Alice", "Important", 60, "reminder"),
        QueueItem("3", "2026-11-18T00:02:00Z", "Telegram", "New message", "Hello", 50, "message"),
        QueueItem("4", "2026-11-18T00:03:00Z", "Task", "Code export", "Done", 40, "task"),
    ]
    grouped = group_queued_items(items)
    assert grouped["reminder"] == 2
    assert grouped["message"] == 1
    assert grouped["task"] == 1


def test_format_catchup_speech():
    # Empty queue
    assert "no new notifications" in format_catchup_speech([])

    # Single category
    single = [
        QueueItem("1", "2026-11-18T00:00:00Z", "Reminder", "Stand up", "", 50, "reminder")
    ]
    speech = format_catchup_speech(single)
    assert "1 reminder" in speech
    assert "Welcome back" in speech

    # Multiple categories
    multiple = [
        QueueItem("1", "2026-11-18T00:00:00Z", "Reminder", "Water plants", "", 50, "reminder"),
        QueueItem("2", "2026-11-18T00:01:00Z", "Reminder", "Call Bob", "", 50, "reminder"),
        QueueItem("3", "2026-11-18T00:02:00Z", "Telegram", "Ping", "", 50, "message"),
        QueueItem("4", "2026-11-18T00:03:00Z", "Worker", "Download complete", "", 40, "task"),
    ]
    speech_multi = format_catchup_speech(multiple)
    assert "2 reminders" in speech_multi
    assert "1 message" in speech_multi
    assert "1 completed task" in speech_multi


def test_format_catchup_details():
    items = [
        QueueItem("1", "2026-11-18T00:00:00Z", "Slack", "Team meeting", "Starting soon", 80),
        QueueItem("2", "2026-11-18T00:01:00Z", "System", "Battery low", "15% remaining", 90),
    ]
    details = format_catchup_details(items)
    assert len(details) == 2
    # Priority 90 item should come first
    assert "Battery low" in details[0]
    assert "Team meeting" in details[1]


def test_presence_manager_lifecycle(tmp_path):
    interrupted = []
    queue_file = tmp_path / "away_queue.json"

    pm = PresenceManager(
        persistence_path=queue_file,
        interrupt_callback=lambda: interrupted.append(True)
    )

    assert not pm.is_away
    assert pm.queue_length == 0

    # Enter away
    pm.enter_away("User said I'm heading out")
    assert pm.is_away
    assert len(interrupted) == 1

    # Enqueue items while away
    item1 = QueueItem("1", "2026-11-18T00:00:00Z", "Reminders", "Medicine", "Take vitamin C", 75, "reminder")
    pm.enqueue_item(item1)
    assert pm.queue_length == 1
    assert queue_file.exists()

    # Verify persistence file
    content = json.loads(queue_file.read_text(encoding="utf-8"))
    assert len(content) == 1
    assert content[0]["title"] == "Medicine"

    # Exit away
    catchup = pm.exit_away("User said I'm back")
    assert not pm.is_away
    assert pm.queue_length == 0
    assert not queue_file.exists()
    assert catchup["count"] == 1
    assert "1 reminder" in catchup["summary"]

    # Debounce test: immediate subsequent exit_away returns empty
    catchup_dup = pm.exit_away("Duplicate event")
    assert catchup_dup == {}
