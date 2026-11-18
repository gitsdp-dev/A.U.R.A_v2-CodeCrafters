"""
aura_chat_memory.py — High-Performance Conversation Recall for A.U.R.A. v2

Backed by SQLite with FTS5 full-text indexing and BM25 ranking (WAL mode).
Supports natural-language date parsing, asynchronous turn logging,
secret/credential redaction, retention pruning, and fast sub-10ms recall.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("aura.chat_memory")

# Redaction patterns for secrets, keys, and credentials
_SENSITIVE_PATTERNS = [
    re.compile(r"(?i)\b(?:password|passwd|pin|secret|api[_-]?key|access[_-]?token|auth[_-]?token|bearer)\s*[:=]\s*\S+"),
    re.compile(r"\b(?:sk-[a-zA-Z0-9]{20,}|AIza[0-9A-Za-z-_]{35})\b"),
    re.compile(r"\b(?:\d{4}[ -]?){3}\d{4}\b"),  # Credit cards
]


def sanitize_turn_text(text: str) -> str:
    """Redact sensitive keys, passwords, or credit card numbers from turn text."""
    if not text:
        return ""
    sanitized = str(text)
    for pat in _SENSITIVE_PATTERNS:
        sanitized = pat.sub("[REDACTED SECRETS]", sanitized)
    return sanitized.strip()


# ── Natural Language Date Parsing ─────────────────────────────────────────────

def parse_date_range(text: str, now: datetime | None = None) -> tuple[float, float] | None:
    """
    Parse natural language expressions like 'yesterday', 'last week', 'in March',
    'two days ago', 'on the 5th' into (start_timestamp, end_timestamp) in epoch seconds.
    Tries dateparser first if installed; otherwise uses a robust built-in engine.
    """
    if not text:
        return None
    raw = str(text).lower().strip()

    # 1. Try dateparser if available
    try:
        import dateparser
        parsed = dateparser.parse(raw)
        if parsed:
            # Expand to full day boundary
            start = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
            end = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
            return start.timestamp(), end.timestamp()
    except Exception:
        pass

    # 2. Built-in high-precision parser
    if now is None:
        now = datetime.now()

    # Today
    if "today" in raw:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        return start.timestamp(), end.timestamp()

    # Yesterday
    if "yesterday" in raw:
        y = now - timedelta(days=1)
        start = y.replace(hour=0, minute=0, second=0, microsecond=0)
        end = y.replace(hour=23, minute=59, second=59, microsecond=999999)
        return start.timestamp(), end.timestamp()

    # N days ago
    m = re.search(r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+days?\s+ago", raw)
    if m:
        word_to_num = {
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        }
        val = m.group(1)
        days = int(val) if val.isdigit() else word_to_num.get(val, 1)
        d = now - timedelta(days=days)
        start = d.replace(hour=0, minute=0, second=0, microsecond=0)
        end = d.replace(hour=23, minute=59, second=59, microsecond=999999)
        return start.timestamp(), end.timestamp()

    # Last week / past week / 7 days
    if "last week" in raw or "past week" in raw or "7 days" in raw or "this week" in raw:
        start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
        return start.timestamp(), end.timestamp()

    # Last month / past month / 30 days
    if "last month" in raw or "past month" in raw or "30 days" in raw:
        start = (now - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
        return start.timestamp(), end.timestamp()

    # Month by name, e.g. 'in march'
    months = [
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december"
    ]
    for i, mname in enumerate(months, 1):
        if mname in raw:
            year = now.year if i <= now.month else now.year - 1
            start = datetime(year, i, 1, 0, 0, 0)
            if i == 12:
                end = datetime(year, 12, 31, 23, 59, 59)
            else:
                next_m = datetime(year, i + 1, 1)
                end = next_m - timedelta(microseconds=1)
            return start.timestamp(), end.timestamp()

    # On the 5th / on the 12th
    m = re.search(r"(?:on\s+the\s+|on\s+)(\d{1,2})(?:st|nd|rd|th)?", raw)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            try:
                target = now.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
                if target > now:
                    target = (target.replace(day=1) - timedelta(days=1)).replace(day=day)
                end = target.replace(hour=23, minute=59, second=59, microsecond=999999)
                return target.timestamp(), end.timestamp()
            except ValueError:
                pass

    return None


def match_conversation_recall_intent(text: str) -> dict[str, str] | None:
    """
    Detect whether user utterance is asking to recall a past conversation.
    Returns {"query": str, "date_range": str} or None.
    """
    if not text:
        return None
    normalized = " ".join(re.sub(r"[^a-z0-9' ]+", " ", str(text).casefold()).split())

    # Trigger patterns
    recall_triggers = [
        r"(?:what\s+was\s+that|what\s+did\s+(?:we|i)\s+(?:talk\s+about|discuss|say|mention))\s+(?:about\s+)?(.+)",
        r"(?:do\s+you\s+remember|recall)\s+(?:what\s+(?:we|i)\s+(?:said|talked\s+about|discussed)\s+(?:about\s+)?)(.+)",
        r"(?:search\s+(?:chat\s+history|conversation\s+history|chat|conversation|history)\s+for\s+)(.+)",
        r"(?:what\s+did\s+i\s+say\s+)(.+)",
    ]

    matched_content = ""
    for pat in recall_triggers:
        m = re.search(pat, normalized)
        if m:
            matched_content = m.group(1).strip()
            break

    if not matched_content:
        # Check general recall keywords
        if any(w in normalized for w in ("remember when", "what did we discuss", "what did i mention", "recall conversation")):
            matched_content = normalized

    if not matched_content:
        return None

    # Separate date hints from query
    date_hints = ["yesterday", "last week", "two days ago", "last month", "today", "past week", "in march", "on the 5th"]
    detected_date = ""
    clean_query = matched_content

    for dh in date_hints:
        if dh in clean_query:
            detected_date = dh
            clean_query = clean_query.replace(dh, "").strip()

    # Clean query prefixes
    clean_query = re.sub(r"^(?:about|regarding|concerning|that|the)\s+", "", clean_query).strip()
    clean_query = re.sub(r"\s+(?:about|regarding)$", "", clean_query).strip()

    return {
        "query": clean_query or matched_content,
        "date_range": detected_date,
    }


# ── SQLite Chat Memory Engine ────────────────────────────────────────────────

class ChatMemoryManager:
    """
    Manages local conversation persistence, FTS5 BM25 search,
    and automatic asynchronous write queue.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            base_dir = Path(__file__).resolve().parent
            db_path = base_dir / "memory" / "chat_history.db"
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._queue: queue.Queue[tuple[str, str, str, float]] = queue.Queue()
        self._stop_event = threading.Event()
        self._fts_available = False
        self._lock = threading.RLock()

        self._init_database()

        # Start asynchronous turn logger thread
        self._writer_thread = threading.Thread(
            target=self._worker_loop,
            name="aura-chat-memory-writer",
            daemon=True,
        )
        self._writer_thread.start()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=15.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_database(self) -> None:
        """Create tables, indexes, and FTS5 virtual table if supported."""
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id TEXT PRIMARY KEY,
                        started_at REAL,
                        title TEXT
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT,
                        role TEXT,
                        content TEXT,
                        timestamp TEXT,
                        created_at REAL
                    );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);")

                # Test and setup FTS5
                try:
                    cur.execute("""
                        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                            content,
                            role UNINDEXED,
                            timestamp UNINDEXED,
                            session_id UNINDEXED,
                            created_at UNINDEXED,
                            content='messages',
                            content_rowid='id'
                        );
                    """)
                    # Setup triggers for auto-syncing FTS5
                    cur.execute("""
                        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                            INSERT INTO messages_fts(rowid, content, role, timestamp, session_id, created_at)
                            VALUES (new.id, new.content, new.role, new.timestamp, new.session_id, new.created_at);
                        END;
                    """)
                    cur.execute("""
                        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                            INSERT INTO messages_fts(messages_fts, rowid, content, role, timestamp, session_id, created_at)
                            VALUES ('delete', old.id, old.content, old.role, old.timestamp, old.session_id, old.created_at);
                        END;
                    """)
                    self._fts_available = True
                    _LOG.info("SQLite FTS5 full-text index initialized.")
                except Exception as exc:
                    self._fts_available = False
                    _LOG.warning("SQLite FTS5 not available (%s); falling back to LIKE index.", exc)

                conn.commit()
            finally:
                conn.close()

    def log_turn(self, role: str, text: str, session_id: str = "") -> None:
        """
        Asynchronously queue a turn for persistent indexing.
        Safe for rapid execution: never blocks UI or audio processing.
        """
        from memory.config_manager import get_chat_memory_config
        cfg = get_chat_memory_config()
        if not cfg.get("enabled", True) or cfg.get("paused", False):
            return

        cleaned = sanitize_turn_text(text)
        if not cleaned:
            return

        now_epoch = time.time()
        sid = session_id or time.strftime("%Y%m%d")
        self._queue.put((role, cleaned, sid, now_epoch))

    def _worker_loop(self) -> None:
        """Background worker: batches inserts into SQLite."""
        while not self._stop_event.is_set():
            batch = []
            try:
                item = self._queue.get(timeout=0.5)
                batch.append(item)
                # Pull any additional pending items up to 50
                while len(batch) < 50:
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
            except queue.Empty:
                continue

            if not batch:
                continue

            try:
                conn = self._get_connection()
                try:
                    cur = conn.cursor()
                    for role, content, sid, created_at in batch:
                        ts_str = datetime.fromtimestamp(created_at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        cur.execute(
                            "INSERT OR IGNORE INTO sessions (session_id, started_at, title) VALUES (?, ?, ?);",
                            (sid, created_at, f"Session {sid}"),
                        )
                        cur.execute(
                            "INSERT INTO messages (session_id, role, content, timestamp, created_at) VALUES (?, ?, ?, ?, ?);",
                            (sid, role, content, ts_str, created_at),
                        )
                    conn.commit()
                finally:
                    conn.close()
            except Exception as exc:
                _LOG.error("Failed to commit chat turn batch: %s", exc)

    def search(self, query: str, date_range: str = "", limit: int = 5) -> list[dict[str, Any]]:
        """
        Search past conversations with keyword matching (FTS5 BM25 or LIKE)
        and optional natural language date boundaries.
        Returns list of matching turns with content, role, timestamp.
        """
        from memory.config_manager import get_chat_memory_config
        cfg = get_chat_memory_config()
        if not cfg.get("enabled", True):
            return []

        # Flush queue before searching so freshly spoken turns are visible
        self._flush_queue()

        time_range = parse_date_range(date_range) if date_range else None
        start_t = time_range[0] if time_range else 0.0
        end_t = time_range[1] if time_range else 9999999999.0

        clean_query = query.strip()
        results: list[dict[str, Any]] = []

        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                if self._fts_available and clean_query:
                    # Clean query for FTS5 (escape special chars)
                    safe_query = re.sub(r'["\*\?\:\^]', " ", clean_query).strip()
                    if safe_query:
                        terms = [f'"{t}"' for t in safe_query.split() if t]
                        fts_expr = " AND ".join(terms) if terms else safe_query
                        try:
                            sql = """
                                SELECT m.id, m.session_id, m.role, m.content, m.timestamp, m.created_at, bm25(messages_fts) as rank
                                FROM messages_fts
                                JOIN messages m ON m.id = messages_fts.rowid
                                WHERE messages_fts MATCH ?
                                  AND m.created_at >= ? AND m.created_at <= ?
                                ORDER BY rank
                                LIMIT ?;
                            """
                            cur.execute(sql, (fts_expr, start_t, end_t, limit))
                            for row in cur.fetchall():
                                results.append(dict(row))
                        except Exception as fts_err:
                            _LOG.debug("FTS5 query failed; trying LIKE fallback: %s", fts_err)

                # Fallback to LIKE if FTS yielded nothing or is unavailable
                if not results and clean_query:
                    sql = """
                        SELECT id, session_id, role, content, timestamp, created_at, 0 as rank
                        FROM messages
                        WHERE content LIKE ?
                          AND created_at >= ? AND created_at <= ?
                        ORDER BY created_at DESC
                        LIMIT ?;
                    """
                    cur.execute(sql, (f"%{clean_query}%", start_t, end_t, limit))
                    for row in cur.fetchall():
                        results.append(dict(row))

                # If no query specified, retrieve recent turns within date range
                elif not clean_query and time_range:
                    sql = """
                        SELECT id, session_id, role, content, timestamp, created_at, 0 as rank
                        FROM messages
                        WHERE created_at >= ? AND created_at <= ?
                        ORDER BY created_at DESC
                        LIMIT ?;
                    """
                    cur.execute(sql, (start_t, end_t, limit))
                    for row in cur.fetchall():
                        results.append(dict(row))

            finally:
                conn.close()

        return results

    def recall_formatted(self, query: str, date_range: str = "", limit: int = 5) -> str:
        """
        Formats search results into a clean, honest context snippet for LLMs.
        Never hallucinates: clearly states when nothing is found.
        """
        matches = self.search(query, date_range=date_range, limit=limit)
        if not matches:
            if date_range and query:
                return f"No past conversation found matching '{query}' from {date_range}. Please honestly inform the user that no such conversation exists in memory."
            elif query:
                return f"No past conversation found matching '{query}'. Please honestly inform the user that no such conversation was found."
            return "No past conversation found matching that timeframe."

        # Order chronologically for natural conversational flow
        matches.sort(key=lambda x: x["created_at"])
        lines = [f"Found {len(matches)} relevant past turn(s):"]
        for m in matches:
            role_label = "User" if m["role"].lower() == "user" else "AURA"
            lines.append(f"[{m['timestamp']}] {role_label}: {m['content']}")

        return "\n".join(lines)

    def _flush_queue(self) -> None:
        """Drain write queue to ensure immediate visibility."""
        batch = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return

        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                for role, content, sid, created_at in batch:
                    ts_str = datetime.fromtimestamp(created_at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    cur.execute(
                        "INSERT OR IGNORE INTO sessions (session_id, started_at, title) VALUES (?, ?, ?);",
                        (sid, created_at, f"Session {sid}"),
                    )
                    cur.execute(
                        "INSERT INTO messages (session_id, role, content, timestamp, created_at) VALUES (?, ?, ?, ?, ?);",
                        (sid, role, content, ts_str, created_at),
                    )
                conn.commit()
            finally:
                conn.close()

    def get_stats(self) -> dict[str, Any]:
        """Return message count, session count, and database size."""
        self._flush_queue()
        with self._lock:
            if not self._db_path.exists():
                return {"messages": 0, "sessions": 0, "size_mb": 0.0, "display": "0 KB (0 messages)"}
            size_bytes = self._db_path.stat().st_size
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM messages;")
                msg_count = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM sessions;")
                sess_count = cur.fetchone()[0]
            finally:
                conn.close()

        size_kb = size_bytes / 1024.0
        size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb / 1024.0:.2f} MB"
        return {
            "messages": msg_count,
            "sessions": sess_count,
            "size_mb": size_bytes / (1024.0 * 1024.0),
            "display": f"{size_str} ({msg_count} messages)",
        }

    def delete_all(self) -> None:
        """Delete all conversation history."""
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM messages;")
                cur.execute("DELETE FROM sessions;")
                if self._fts_available:
                    try:
                        cur.execute("DELETE FROM messages_fts;")
                    except Exception:
                        pass
                conn.commit()
                cur.execute("VACUUM;")
            finally:
                conn.close()
        _LOG.info("All conversation history cleared.")

    def delete_older_than(self, days: int) -> int:
        """Delete conversation history older than N days."""
        if days <= 0:
            return 0
        cutoff = time.time() - (days * 86400.0)
        deleted = 0
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM messages WHERE created_at < ?;", (cutoff,))
                deleted = cur.fetchone()[0]
                cur.execute("DELETE FROM messages WHERE created_at < ?;", (cutoff,))
                conn.commit()
                if deleted > 0:
                    cur.execute("VACUUM;")
            finally:
                conn.close()
        _LOG.info("Pruned %d messages older than %d days.", deleted, days)
        return deleted

    def close(self) -> None:
        """Gracefully stop background writer thread."""
        self._stop_event.set()
        self._flush_queue()
        if self._writer_thread.is_alive():
            self._writer_thread.join(timeout=1.5)
