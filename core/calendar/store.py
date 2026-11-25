"""SQLite persistence (WAL, versioned migrations, parameterized queries) for core.calendar."""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.calendar.models import Calendar, Event, ensure_aware, get_local_tz, get_user_data_dir

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS calendars (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            provider TEXT NOT NULL,
            read_only INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            color TEXT NOT NULL DEFAULT '#ff7b00',
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            calendar_id TEXT NOT NULL,
            title TEXT NOT NULL,
            start_iso TEXT NOT NULL,
            end_iso TEXT NOT NULL,
            start_utc_ts REAL NOT NULL,
            end_utc_ts REAL NOT NULL,
            all_day INTEGER NOT NULL DEFAULT 0,
            location TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            rrule TEXT NOT NULL DEFAULT '',
            exdates_json TEXT NOT NULL DEFAULT '[]',
            reminders_json TEXT NOT NULL DEFAULT '[15]',
            source TEXT NOT NULL DEFAULT 'local',
            etag TEXT NOT NULL DEFAULT '',
            uid TEXT NOT NULL DEFAULT '',
            last_modified_iso TEXT,
            FOREIGN KEY (calendar_id) REFERENCES calendars(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_events_cal_range
            ON events(calendar_id, start_utc_ts, end_utc_ts);
        CREATE INDEX IF NOT EXISTS idx_events_uid
            ON events(calendar_id, uid);

        CREATE TABLE IF NOT EXISTS reminders_sent (
            dedup_key TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            occurrence_iso TEXT NOT NULL,
            offset_minutes INTEGER NOT NULL,
            sent_at_utc REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL
        );
        """,
    ),
]

DEFAULT_SETTINGS: dict[str, Any] = {
    "default_reminder_minutes": 15,
    "work_start_hour": 9,
    "work_end_hour": 18,
    "ai_polish_enabled": False,
    "proactive_enabled": True,
    "proactive_lead_minutes": 15,
    "feed_refresh_minutes": 30,
    "hint_shown": False,
}


class CalendarStore:
    """Thread-safe SQLite store for calendars, events, reminder dedup, and settings."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else (get_user_data_dir() / "calendar.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA journal_mode = WAL;")
        except Exception:
            conn.close()
            raise
        return conn

    def _init_db(self) -> None:
        with self._lock:
            try:
                self._apply_migrations()
            except sqlite3.DatabaseError as exc:
                LOGGER.warning("Corrupt calendar database detected (%s); backing up and recreating.", exc)
                self._backup_and_recreate()
                self._apply_migrations()

    def _backup_and_recreate(self) -> None:
        if self.db_path.exists():
            backup = self.db_path.with_suffix(f".corrupt.{int(time.time())}.bak")
            try:
                shutil.move(str(self.db_path), str(backup))
            except OSError:
                self.db_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.db_path) + suffix)
            sidecar.unlink(missing_ok=True)

    def _apply_migrations(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            )
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            current_version = int(row["value"]) if row else 0
            for version, sql in _MIGRATIONS:
                if version > current_version:
                    conn.executescript(sql)
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                        (str(version),),
                    )
            # Ensure default local calendar exists (empty by default, enabled by default)
            existing = conn.execute(
                "SELECT id FROM calendars WHERE id = 'local'"
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO calendars (id, name, provider, read_only, enabled, color, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("local", "Personal (Local)", "local", 0, 1, "#ff7b00", "{}"),
                )
            conn.commit()
        finally:
            conn.close()

    # ── Settings ─────────────────────────────────────────────────────────────

    def get_settings(self) -> dict[str, Any]:
        out = dict(DEFAULT_SETTINGS)
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT key, value_json FROM settings").fetchall()
            for r in rows:
                try:
                    out[r["key"]] = json.loads(r["value_json"])
                except (json.JSONDecodeError, TypeError):
                    pass
        return out

    def get_setting(self, key: str, default: Any = None) -> Any:
        settings = self.get_settings()
        return settings.get(key, default)

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value_json) VALUES (?, ?)",
                (str(key), json.dumps(value)),
            )
            conn.commit()

    # ── Calendars ────────────────────────────────────────────────────────────

    def list_calendars(self, enabled_only: bool = False) -> list[Calendar]:
        sql = "SELECT id, name, provider, read_only, enabled, color FROM calendars"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY CASE WHEN id = 'local' THEN 0 ELSE 1 END, name ASC"
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [
            Calendar(
                id=r["id"],
                name=r["name"],
                provider=r["provider"],
                read_only=bool(r["read_only"]),
                enabled=bool(r["enabled"]),
                color=r["color"],
            )
            for r in rows
        ]

    def get_calendar(self, calendar_id: str) -> Calendar | None:
        with self._lock, self._connect() as conn:
            r = conn.execute(
                "SELECT id, name, provider, read_only, enabled, color FROM calendars WHERE id = ?",
                (calendar_id,),
            ).fetchone()
        if r is None:
            return None
        return Calendar(
            id=r["id"],
            name=r["name"],
            provider=r["provider"],
            read_only=bool(r["read_only"]),
            enabled=bool(r["enabled"]),
            color=r["color"],
        )

    def upsert_calendar(self, cal: Calendar, metadata: dict[str, Any] | None = None) -> Calendar:
        meta_json = json.dumps(metadata or {})
        with self._lock, self._connect() as conn:
            if metadata is None:
                existing = conn.execute(
                    "SELECT metadata_json FROM calendars WHERE id = ?",
                    (cal.id,),
                ).fetchone()
                if existing is not None:
                    meta_json = existing["metadata_json"]
            conn.execute(
                """
                INSERT OR REPLACE INTO calendars
                    (id, name, provider, read_only, enabled, color, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cal.id,
                    cal.name,
                    cal.provider,
                    1 if cal.read_only else 0,
                    1 if cal.enabled else 0,
                    cal.color,
                    meta_json,
                ),
            )
            conn.commit()
        return cal

    def get_calendar_metadata(self, calendar_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            r = conn.execute(
                "SELECT metadata_json FROM calendars WHERE id = ?",
                (calendar_id,),
            ).fetchone()
        if r is None:
            return {}
        try:
            data = json.loads(r["metadata_json"])
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def set_calendar_metadata(self, calendar_id: str, metadata: dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE calendars SET metadata_json = ? WHERE id = ?",
                (json.dumps(metadata), calendar_id),
            )
            conn.commit()

    def set_calendar_enabled(self, calendar_id: str, enabled: bool) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE calendars SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, calendar_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def delete_calendar(self, calendar_id: str) -> bool:
        if calendar_id == "local":
            # Never delete the local calendar row; just clear its events if requested.
            with self._lock, self._connect() as conn:
                conn.execute("DELETE FROM events WHERE calendar_id = 'local'")
                conn.commit()
            return True
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM events WHERE calendar_id = ?", (calendar_id,))
            cur = conn.execute("DELETE FROM calendars WHERE id = ?", (calendar_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── Events ───────────────────────────────────────────────────────────────

    def _row_to_event(self, r: sqlite3.Row) -> Event:
        tz = get_local_tz()
        try:
            reminders = json.loads(r["reminders_json"])
        except (json.JSONDecodeError, TypeError):
            reminders = [15]
        try:
            exdates = json.loads(r["exdates_json"])
        except (json.JSONDecodeError, TypeError):
            exdates = []
        lm = datetime.fromisoformat(r["last_modified_iso"]) if r["last_modified_iso"] else None
        return Event(
            id=r["id"],
            calendar_id=r["calendar_id"],
            title=r["title"],
            start=ensure_aware(datetime.fromisoformat(r["start_iso"]), tz),
            end=ensure_aware(datetime.fromisoformat(r["end_iso"]), tz),
            all_day=bool(r["all_day"]),
            location=r["location"],
            notes=r["notes"],
            rrule=r["rrule"],
            exdates=list(exdates),
            reminders=[int(x) for x in reminders],
            source=r["source"],
            etag=r["etag"],
            uid=r["uid"],
            last_modified=lm,
        )

    def upsert_event(self, event: Event) -> Event:
        tz = get_local_tz()
        start_aware = ensure_aware(event.start, tz)
        end_aware = ensure_aware(event.end, tz)
        start_ts = start_aware.astimezone(timezone.utc).timestamp()
        end_ts = end_aware.astimezone(timezone.utc).timestamp()
        lm_iso = event.last_modified.isoformat() if event.last_modified else datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO events (
                    id, calendar_id, title, start_iso, end_iso,
                    start_utc_ts, end_utc_ts, all_day, location, notes,
                    rrule, exdates_json, reminders_json, source, etag, uid, last_modified_iso
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.calendar_id,
                    event.title,
                    start_aware.isoformat(),
                    end_aware.isoformat(),
                    start_ts,
                    end_ts,
                    1 if event.all_day else 0,
                    event.location,
                    event.notes,
                    event.rrule,
                    json.dumps(event.exdates),
                    json.dumps(event.reminders),
                    event.source,
                    event.etag,
                    event.uid,
                    lm_iso,
                ),
            )
            conn.commit()
        return event

    def replace_calendar_events(self, calendar_id: str, events: list[Event]) -> int:
        """Atomically replace all cached events for a synced calendar."""
        tz = get_local_tz()
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM events WHERE calendar_id = ?", (calendar_id,))
            for event in events:
                start_aware = ensure_aware(event.start, tz)
                end_aware = ensure_aware(event.end, tz)
                start_ts = start_aware.astimezone(timezone.utc).timestamp()
                end_ts = end_aware.astimezone(timezone.utc).timestamp()
                lm_iso = (
                    event.last_modified.isoformat()
                    if event.last_modified
                    else datetime.now(timezone.utc).isoformat()
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO events (
                        id, calendar_id, title, start_iso, end_iso,
                        start_utc_ts, end_utc_ts, all_day, location, notes,
                        rrule, exdates_json, reminders_json, source, etag, uid, last_modified_iso
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.id,
                        calendar_id,
                        event.title,
                        start_aware.isoformat(),
                        end_aware.isoformat(),
                        start_ts,
                        end_ts,
                        1 if event.all_day else 0,
                        event.location,
                        event.notes,
                        event.rrule,
                        json.dumps(event.exdates),
                        json.dumps(event.reminders),
                        event.source,
                        event.etag,
                        event.uid,
                        lm_iso,
                    ),
                )
            conn.commit()
        return len(events)

    def get_event(self, event_id: str) -> Event | None:
        with self._lock, self._connect() as conn:
            r = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._row_to_event(r) if r is not None else None

    def delete_event(self, event_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
            conn.commit()
            return cur.rowcount > 0

    def list_raw_events(
        self,
        calendar_id: str | None = None,
        enabled_only: bool = True,
    ) -> list[Event]:
        """List stored events (including recurring master events) without RRULE expansion."""
        clauses: list[str] = []
        params: list[Any] = []
        if calendar_id is not None:
            clauses.append("e.calendar_id = ?")
            params.append(calendar_id)
        if enabled_only:
            clauses.append("c.enabled = 1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT e.* FROM events e
            JOIN calendars c ON e.calendar_id = c.id
            {where}
            ORDER BY e.start_utc_ts ASC
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_event(r) for r in rows]

    def query_non_recurring_in_range(
        self,
        range_start: datetime,
        range_end: datetime,
        calendar_id: str | None = None,
        enabled_only: bool = True,
    ) -> list[Event]:
        start_ts = ensure_aware(range_start).astimezone(timezone.utc).timestamp()
        end_ts = ensure_aware(range_end).astimezone(timezone.utc).timestamp()
        clauses = ["e.rrule = ''", "e.end_utc_ts > ?", "e.start_utc_ts < ?"]
        params: list[Any] = [start_ts, end_ts]
        if calendar_id is not None:
            clauses.append("e.calendar_id = ?")
            params.append(calendar_id)
        if enabled_only:
            clauses.append("c.enabled = 1")
        sql = f"""
            SELECT e.* FROM events e
            JOIN calendars c ON e.calendar_id = c.id
            WHERE {' AND '.join(clauses)}
            ORDER BY e.start_utc_ts ASC
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_event(r) for r in rows]

    def list_recurring_masters(
        self,
        calendar_id: str | None = None,
        enabled_only: bool = True,
    ) -> list[Event]:
        clauses = ["e.rrule != ''"]
        params: list[Any] = []
        if calendar_id is not None:
            clauses.append("e.calendar_id = ?")
            params.append(calendar_id)
        if enabled_only:
            clauses.append("c.enabled = 1")
        sql = f"""
            SELECT e.* FROM events e
            JOIN calendars c ON e.calendar_id = c.id
            WHERE {' AND '.join(clauses)}
            ORDER BY e.start_utc_ts ASC
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_event(r) for r in rows]

    # ── Reminder deduplication ───────────────────────────────────────────────

    def was_reminder_sent(self, dedup_key: str) -> bool:
        with self._lock, self._connect() as conn:
            r = conn.execute(
                "SELECT 1 FROM reminders_sent WHERE dedup_key = ?",
                (dedup_key,),
            ).fetchone()
        return r is not None

    def mark_reminder_sent(
        self,
        dedup_key: str,
        event_id: str,
        occurrence_iso: str,
        offset_minutes: int,
    ) -> None:
        now_ts = datetime.now(timezone.utc).timestamp()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO reminders_sent
                    (dedup_key, event_id, occurrence_iso, offset_minutes, sent_at_utc)
                VALUES (?, ?, ?, ?, ?)
                """,
                (dedup_key, event_id, occurrence_iso, int(offset_minutes), now_ts),
            )
            conn.commit()

    def clear_reminders_for_event(self, event_id: str) -> None:
        """Clear pending/sent dedup markers when an event is moved or cancelled."""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM reminders_sent WHERE event_id = ?", (event_id,))
            conn.commit()
