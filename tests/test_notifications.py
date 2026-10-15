from datetime import datetime, timedelta, timezone
import sqlite3

from core.notifications.intent import match_notification_intent
from core.notifications.listener import _notification_from_xml, _read_windows_database
from core.notifications.models import Notification
from core.notifications.priority import HIGH, priority_level, score_notification, sort_notifications
from core.notifications.store import NotificationStore
from core.notifications.bus import notification_bus
from core.notifications.center import NotificationCenter
from memory import config_manager


def _notification(title: str, body: str = "", app: str = "Calendar",
                  minutes_ago: int = 0, priority: int = 40) -> Notification:
    return Notification(
        source="app",
        app_name=app,
        title=title,
        body=body,
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        priority=priority,
    )


def test_priority_uses_app_and_urgency_overrides():
    message = _notification("OTP verification code", app="Unknown App")
    assert score_notification(message, now=datetime.now(timezone.utc)) >= 70
    assert priority_level(message.priority) == HIGH

    configured = _notification("New item", app="Calendar")
    score_notification(
        configured,
        {"app_priority_overrides": {"Calendar": "CRITICAL"}},
        now=datetime.now(timezone.utc),
    )
    assert priority_level(configured.priority) == "CRITICAL"


def test_store_deduplicates_and_limits_recent_history(tmp_path):
    store = NotificationStore(tmp_path / "notifications.json", max_entries=2)
    first = _notification("Same", "Body")
    assert store.add(first) is first
    duplicate = _notification("Same", "Body", minutes_ago=1)
    assert store.add(duplicate) is first
    store.add(_notification("Second", priority=90))
    store.add(_notification("Third", priority=70))
    assert [item.title for item in store.list()] == ["Same", "Second", "Third"][-2:]
    reloaded = NotificationStore(tmp_path / "notifications.json", max_entries=2)
    assert [item.title for item in reloaded.list()] == ["Second", "Third"]


def test_store_sorted_by_priority_then_recency():
    oldest = _notification("Old", minutes_ago=5, priority=70)
    newest = _notification("New", priority=70)
    top = _notification("Top", priority=90, minutes_ago=10)
    assert sort_notifications([oldest, top, newest]) == [top, newest, oldest]


def test_notification_intent_matching_is_local_and_natural():
    assert match_notification_intent("AURA, show and read all notifications") == "show_read"
    assert match_notification_intent("read all notifications") == "show_read"
    assert match_notification_intent("show my notifications") == "show_read"
    assert match_notification_intent("stop") == "stop"
    assert match_notification_intent("skip this") == "skip"
    assert match_notification_intent("tell me a joke") is None


def test_windows_toast_xml_extracts_title_and_body():
    notification = _notification_from_xml(
        "<toast><visual><binding><text>New message</text>"
        "<text>Hello, AURA</text></binding></visual></toast>",
        "Messenger",
        12,
    )
    assert notification.source == "system"
    assert notification.app_name == "Messenger"
    assert notification.title == "New message"
    assert notification.body == "Hello, AURA"


def test_windows_database_fallback_reads_from_copy(tmp_path):
    database = tmp_path / "wpndatabase.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE Notification (Id INTEGER, HandlerId TEXT, Payload TEXT)"
        )
        connection.execute(
            "INSERT INTO Notification VALUES (?, ?, ?)",
            (15, "Messenger", "<toast><text>Title</text><text>Body</text></toast>"),
        )
    rows = _read_windows_database(database)
    assert len(rows) == 1
    assert rows[0][1].app_name == "Messenger"
    assert rows[0][1].title == "Title"
    assert rows[0][1].body == "Body"


def test_center_filters_blocked_apps_and_publishes_local_items(tmp_path):
    class ListenerStub:
        def on_notification(self, _callback):
            pass

        def stop(self):
            pass

    store = NotificationStore(tmp_path / "history.json")
    center = NotificationCenter(
        config={
            "notifications_enabled": True,
            "max_stored_notifications": 200,
            "blocked_apps": ["Blocked App"],
            "app_priority_overrides": {},
        },
        store=store,
        listener=ListenerStub(),
    )
    forwarded = []
    center.subscribe(forwarded.append)
    try:
        notification_bus.publish(_notification("Blocked", app="Blocked App"))
        notification_bus.publish(_notification("Allowed", app="Messenger"))
        assert [item.title for item in store.list()] == ["Allowed"]
        assert [item.title for item in forwarded] == ["Allowed"]
    finally:
        center.stop()


def test_notification_config_defaults_and_clamps(monkeypatch):
    monkeypatch.setattr(
        config_manager,
        "load_api_keys",
        lambda: {
            "toast_timeout_seconds": 9,
            "max_visible_toasts": 0,
            "blocked_apps": ["Messenger"],
        },
    )
    settings = config_manager.get_notifications_config()
    assert settings["notifications_enabled"] is True
    assert settings["toast_timeout_seconds"] == 5.0
    assert settings["max_visible_toasts"] == 1
    assert settings["blocked_apps"] == ["Messenger"]

