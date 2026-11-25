"""
core/learning/learning_database.py — SQLite Engine for Adaptive Intelligence.

Features:
- Versioned migrations via PRAGMA user_version.
- WAL mode, foreign keys, and busy timeout for high-concurrency read/write.
- Single dedicated writer thread fed by a queue; thread-safe reads.
- FTS5 full-text search with automatic sync triggers; fallback to LIKE if FTS5 unavailable.
- Append-only audit log for status transitions (provisional -> verified -> rejected).
- Automatic corrupt/locked database recovery with timestamped backup.
- Microsoft Store safe writable path resolution.
"""
from __future__ import annotations

import logging
import os
import queue
import shutil
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

LOGGER = logging.getLogger("aura.learning.database")

CURRENT_SCHEMA_VERSION = 1


def get_default_db_path() -> Path:
    """Resolve writable database path, optimized for the Official GitHub release.
    
    In the Official GitHub version, learning data is stored portably in the repository's
    `memory/` directory alongside `chat_history.db` and long-term memory.
    """
    custom = os.environ.get("AURA_LEARNING_DB_PATH")
    if custom:
        p = Path(custom)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    # Official GitHub Version: store portably in repo / executable root / memory / learning.db
    if getattr(sys, "frozen", False):
        base_dir = Path(sys.executable).parent
    else:
        base_dir = Path(__file__).resolve().parent.parent.parent

    target_dir = base_dir / "memory"

    # Verify write access in memory/
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        test_file = target_dir / ".write_test"
        test_file.touch()
        test_file.unlink()
        return target_dir / "learning.db"
    except (OSError, PermissionError):
        # Fallback if running from a strictly read-only directory
        app_data = os.environ.get("LOCALAPPDATA")
        root = Path(app_data) if app_data else Path.home() / ".aura"
        fallback_dir = root / "AURA" / "memory"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        return fallback_dir / "learning.db"


class DatabaseCorruptedError(Exception):
    """Raised when SQLite detects malformed or unrecoverable disk structure."""
    pass


class LearningDatabase:
    """
    Thread-safe SQLite database manager for AURA's adaptive learning subsystem.
    Dispatches all writes through a single background worker thread to guarantee
    zero lock contention, and provides isolated read connections for high throughput.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path or get_default_db_path())
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._fts_available = False
        self._fts_warned = False

        # Initialize schema and verify integrity first
        self._init_database()

        # Dedicated writer thread
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name="aura-learning-db-writer",
            daemon=True,
        )
        self._writer_thread.start()

    def _get_connection(self) -> sqlite3.Connection:
        """Create a properly configured SQLite connection."""
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=10.0,
            check_same_thread=False,
            isolation_level=None,  # Autocommit mode for finer transaction control
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA synchronous = NORMAL")
            return conn
        except Exception:
            conn.close()
            raise

    def _init_database(self) -> None:
        """Run migrations and verify integrity, recovering automatically on corruption."""
        try:
            self._run_migrations()
        except sqlite3.DatabaseError as exc:
            LOGGER.error("Database initialization failed (%s). Attempting corruption recovery...", exc)
            self._recover_corrupted_database()
            self._run_migrations()

    def _recover_corrupted_database(self) -> None:
        """Backup corrupted database with timestamp and recreate cleanly."""
        import gc
        gc.collect()
        try:
            if self.db_path.exists():
                timestamp = int(time.time())
                backup_path = self.db_path.with_name(f"{self.db_path.stem}_corrupt_{timestamp}.db")
                shutil.copy2(self.db_path, backup_path)
                LOGGER.warning("Corrupted database backed up to: %s", backup_path)

                # Clean up WAL and SHM files
                wal = self.db_path.with_name(f"{self.db_path.name}-wal")
                shm = self.db_path.with_name(f"{self.db_path.name}-shm")
                for f in (self.db_path, wal, shm):
                    if f.exists():
                        for _attempt in range(3):
                            try:
                                f.unlink()
                                break
                            except OSError:
                                gc.collect()
                                time.sleep(0.05)
        except Exception as exc:
            LOGGER.error("Recovery backup failed: %s", exc)

    def _run_migrations(self) -> None:
        """Apply versioned migrations using PRAGMA user_version."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA user_version")
            current_version = cursor.fetchone()[0]

            if current_version < 1:
                self._migrate_v1(conn)
                cursor.execute("PRAGMA user_version = 1")
                LOGGER.info("Learning database migrated to schema v1.")

    def _migrate_v1(self, conn: sqlite3.Connection) -> None:
        """Create baseline schema v1."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at REAL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS experiences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                task_description TEXT NOT NULL,
                approach TEXT,
                outcome TEXT,
                success INTEGER NOT NULL DEFAULT 1,
                failure_type TEXT,
                user_feedback TEXT,
                lesson TEXT NOT NULL,
                duration_ms INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'provisional',
                scope TEXT NOT NULL DEFAULT 'global',
                confidence REAL NOT NULL DEFAULT 0.5,
                evidence_count INTEGER NOT NULL DEFAULT 1,
                source TEXT NOT NULL DEFAULT 'observation',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_used_at REAL NOT NULL,
                expires_at REAL,
                reuse_count INTEGER NOT NULL DEFAULT 0,
                why_learned TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_status ON experiences(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_scope ON experiences(scope)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_task_type ON experiences(task_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_confidence ON experiences(confidence)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS learned_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pref_key TEXT UNIQUE NOT NULL,
                pref_value TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                status TEXT NOT NULL DEFAULT 'provisional',
                scope TEXT NOT NULL DEFAULT 'global',
                confidence REAL NOT NULL DEFAULT 0.5,
                evidence_count INTEGER NOT NULL DEFAULT 1,
                source TEXT NOT NULL DEFAULT 'observation',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_used_at REAL NOT NULL,
                expires_at REAL,
                reuse_count INTEGER NOT NULL DEFAULT 0,
                why_learned TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pref_key ON learned_preferences(pref_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pref_status ON learned_preferences(status)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS learned_skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                procedure TEXT NOT NULL,
                conditions TEXT,
                success_count INTEGER NOT NULL DEFAULT 1,
                failure_count INTEGER NOT NULL DEFAULT 0,
                risk_level TEXT NOT NULL DEFAULT 'low',
                status TEXT NOT NULL DEFAULT 'provisional',
                scope TEXT NOT NULL DEFAULT 'global',
                confidence REAL NOT NULL DEFAULT 0.7,
                evidence_count INTEGER NOT NULL DEFAULT 1,
                source TEXT NOT NULL DEFAULT 'verified_execution',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_used_at REAL NOT NULL,
                expires_at REAL,
                reuse_count INTEGER NOT NULL DEFAULT 0,
                why_learned TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_skills_name ON learned_skills(name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_skills_status ON learned_skills(status)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_text TEXT NOT NULL,
                feedback_type TEXT NOT NULL,
                sentiment REAL DEFAULT 0.0,
                target_task TEXT,
                target_id INTEGER,
                created_at REAL NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                trials INTEGER NOT NULL DEFAULT 0,
                successes INTEGER NOT NULL DEFAULT 0,
                total_duration_ms INTEGER NOT NULL DEFAULT 0,
                last_attempt_at REAL NOT NULL,
                UNIQUE(task_type, strategy_name)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS learning_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                table_name TEXT NOT NULL,
                record_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                old_status TEXT,
                new_status TEXT,
                reason TEXT,
                timestamp REAL NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS budget_counters (
                date_str TEXT PRIMARY KEY,
                reflections_used INTEGER NOT NULL DEFAULT 0,
                tokens_used INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            )
        """)

        # Attempt FTS5 Virtual Table setup
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS experiences_fts USING fts5(
                    task_description,
                    lesson,
                    approach,
                    outcome,
                    content='experiences',
                    content_rowid='id'
                )
            """)

            # Triggers to keep FTS5 automatically in sync
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS exp_ai AFTER INSERT ON experiences BEGIN
                    INSERT INTO experiences_fts(rowid, task_description, lesson, approach, outcome)
                    VALUES (new.id, new.task_description, new.lesson, new.approach, new.outcome);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS exp_ad AFTER DELETE ON experiences BEGIN
                    INSERT INTO experiences_fts(experiences_fts, rowid, task_description, lesson, approach, outcome)
                    VALUES('delete', old.id, old.task_description, old.lesson, old.approach, old.outcome);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS exp_au AFTER UPDATE ON experiences BEGIN
                    INSERT INTO experiences_fts(experiences_fts, rowid, task_description, lesson, approach, outcome)
                    VALUES('delete', old.id, old.task_description, old.lesson, old.approach, old.outcome);
                    INSERT INTO experiences_fts(rowid, task_description, lesson, approach, outcome)
                    VALUES (new.id, new.task_description, new.lesson, new.approach, new.outcome);
                END
            """)
            self._fts_available = True
        except sqlite3.OperationalError as exc:
            self._fts_available = False
            if not self._fts_warned:
                LOGGER.warning("SQLite FTS5 not available (%s); falling back to LIKE queries.", exc)
                self._fts_warned = True

    @property
    def fts_available(self) -> bool:
        return self._fts_available

    def _writer_loop(self) -> None:
        """Dedicated background thread executing all write mutations sequentially."""
        conn = None
        try:
            conn = self._get_connection()
            while not self._stop_event.is_set():
                try:
                    task = self._queue.get(timeout=0.2)
                except queue.Empty:
                    continue

                if task is None:  # Sentinel
                    self._queue.task_done()
                    break

                fn, args, kwargs, event, result_holder = task
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    res = fn(conn, *args, **kwargs)
                    conn.execute("COMMIT")
                    if result_holder is not None:
                        result_holder["result"] = res
                except Exception as exc:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    LOGGER.exception("Error executing database write task: %s", exc)
                    if result_holder is not None:
                        result_holder["error"] = exc
                finally:
                    if event is not None:
                        event.set()
                    self._queue.task_done()
        except Exception as exc:
            LOGGER.error("Fatal error in DB writer loop: %s", exc)
        finally:
            if conn:
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    conn.close()
                except Exception:
                    pass

    def execute_write(self, fn: Callable[..., Any], *args, wait: bool = True, **kwargs) -> Any:
        """
        Submit a write transaction to the writer queue.
        If wait=True, blocks until the write completes and returns the result.
        """
        if self._stop_event.is_set():
            return None

        event = threading.Event() if wait else None
        result_holder: dict[str, Any] = {} if wait else None

        self._queue.put((fn, args, kwargs, event, result_holder))

        if wait and event:
            event.wait(timeout=10.0)
            if "error" in result_holder:
                raise result_holder["error"]
            return result_holder.get("result")
        return None

    def execute_read(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        """Execute a read-only query using a short-lived connection."""
        try:
            with self._get_connection() as conn:
                return fn(conn)
        except Exception as exc:
            LOGGER.error("Database read error: %s", exc)
            return None

    def close(self) -> None:
        """Stop writer thread and checkpoint WAL."""
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        self._queue.put(None)
        self._writer_thread.join(timeout=3.0)
        LOGGER.info("Learning database closed cleanly.")
