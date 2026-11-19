from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from core import outcome
from core.tasks.engine import ACTIVE, TaskEngine

_BASE = Path(__file__).resolve().parent.parent
_LOCK = threading.RLock()
_ENGINE: TaskEngine | None = None
_PLAYER = None


def _task_dir() -> Path:
    return _BASE / "memory" / "tasks"


def _reports_dir() -> Path:
    return _BASE / "reports"


def _agents_dir() -> Path:
    return _BASE / "agents"


def _notify(event: str, task: dict | None, text: str) -> None:
    if task is None:
        return

    player = _PLAYER
    if player is None:
        return

    title = task.get("title") or task.get("task") or "Background task"

    try:
        if event == "started":
            player.write_log(f"SYS: Background task started — {title}")

        elif event == "progress":
            player.write_log(f"SYS: {title} — {text}")

        elif event == "done":
            player.write_log(f"SYS: Background task finished — {title}")
            result = str(task.get("result") or "").strip()
            report = str(task.get("report") or "").strip()
            body = f"{title}\n\n{result}"
            if report:
                body += f"\n\nFull report: {report}"
            player.show_content("BACKGROUND TASK", body[:12000])

            if hasattr(player, "request_say"):
                player.request_say(f"The background task '{title}' is complete.")

        elif event == "failed":
            error = str(task.get("error") or text or "unknown error")
            player.write_log(f"ERR: Background task failed — {title}: {error}")

        elif event == "waiting":
            player.write_log(f"SYS: Background task waiting for the model — {title}")

    except Exception as exc:
        print(f"[Tasks] notification failed: {exc}")


def _engine(player=None) -> TaskEngine:
    global _ENGINE, _PLAYER

    with _LOCK:
        if player is not None:
            _PLAYER = player

        if _ENGINE is None:
            _ENGINE = TaskEngine(
                root=_task_dir(),
                reports_dir=_reports_dir(),
                agents_dirs=[_agents_dir()],
                notify=_notify,
                max_parallel=2,
            )

        return _ENGINE


def _when(value: str) -> float:
    text = str(value or "").strip()
    now = datetime.now()

    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue

    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError:
        raise ValueError(
            "at must be YYYY-MM-DD HH:MM or HH:MM"
        ) from None

    result = now.replace(
        hour=parsed.hour,
        minute=parsed.minute,
        second=0,
        microsecond=0,
    )

    if result <= now:
        result += timedelta(days=1)

    return result.timestamp()


def _minutes(task: dict) -> str:
    used = float(task.get("used", {}).get("seconds", 0))
    budget = task.get("budget", {}).get("minutes", 0)
    return f"{used / 60:.1f}/{budget} min"


def background_tasks(parameters: dict, player=None) -> str:
    args = dict(parameters or {})
    action = str(args.get("action") or "list").strip().lower()
    engine = _engine(player)

    if action == "agents":
        profiles = engine.agents(reload=True)
        return "\n".join(
            f"{profile.name}: {profile.description}"
            for profile in profiles.values()
        )

    if action == "start":
        task_text = str(args.get("task") or "").strip()
        if not task_text:
            return outcome.failed(
                "background_tasks",
                "start requires a task description",
            )

        start_at = 0.0
        if args.get("at"):
            try:
                start_at = _when(args["at"])
            except ValueError as exc:
                return outcome.failed("background_tasks", str(exc))

        try:
            task = engine.start(
                task=task_text,
                agent=str(args.get("agent") or "general"),
                minutes=args.get("minutes"),
                deliver=str(args.get("deliver") or "voice"),
                not_before=start_at,
            )
        except (TypeError, ValueError, OSError) as exc:
            return outcome.failed("background_tasks", str(exc))

        return outcome.started(
            "background_tasks",
            task_id=task["id"],
            agent=task["agent"],
            minutes=task["budget"]["minutes"],
            note="The task is running in the background.",
        )

    tasks = engine.list(50)
    task_id = str(args.get("task_id") or "").strip()

    if task_id:
        task = engine.get(task_id)
    else:
        running = [item for item in tasks if item["status"] in ACTIVE]
        task = running[0] if running else (tasks[0] if tasks else None)

    if task is None:
        return outcome.failed(
            "background_tasks",
            "no background task was found",
        )

    if action == "list":
        return "\n".join(
            f"{item['id']} [{item['status']}] "
            f"{item['agent']} — {item['title']} — {_minutes(item)}"
            for item in tasks[:10]
        ) or "No background tasks."

    if action == "status":
        return outcome.done(
            "background_tasks",
            task_id=task["id"],
            status=task["status"],
            agent=task["agent"],
            title=task["title"],
            time=_minutes(task),
        )

    if action == "result":
        if task["status"] != "done":
            return outcome.done(
                "background_tasks",
                task_id=task["id"],
                status=task["status"],
                note="The task is not finished yet.",
            )

        return outcome.done(
            "background_tasks",
            task_id=task["id"],
            report=task.get("report"),
            result=task.get("result"),
        )

    if action == "finish":
        return (
            outcome.started(
                "background_tasks",
                task_id=task["id"],
                note="The task is stopping and writing its current result.",
            )
            if engine.cancel(task["id"], finish=True)
            else outcome.failed("background_tasks", "task is not running")
        )

    if action == "cancel":
        return (
            outcome.done(
                "background_tasks",
                task_id=task["id"],
                cancelled=True,
            )
            if engine.cancel(task["id"])
            else outcome.failed("background_tasks", "task is not running")
        )

    if action == "steer":
        note = str(args.get("note") or "").strip()
        return (
            outcome.done(
                "background_tasks",
                task_id=task["id"],
                note_added=True,
            )
            if note and engine.steer(task["id"], note)
            else outcome.failed(
                "background_tasks",
                "a running task and a note are required",
            )
        )

    return outcome.failed(
        "background_tasks",
        f"unknown action: {action}",
    )


TOOL = {
    "name": "background_tasks",
    "description": (
        "Run long research, drafting, or file-processing work in the background. "
        "Use action=start to begin, agents to list profiles, list/status/result "
        "to inspect work, finish to write up early, cancel to stop, and steer "
        "to add an instruction."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": [
                    "start",
                    "agents",
                    "list",
                    "status",
                    "result",
                    "finish",
                    "cancel",
                    "steer",
                ],
            },
            "task": {"type": "STRING"},
            "agent": {"type": "STRING"},
            "minutes": {"type": "INTEGER"},
            "deliver": {
                "type": "STRING",
                "enum": ["voice", "panel", "silent"],
            },
            "at": {"type": "STRING"},
            "task_id": {"type": "STRING"},
            "note": {"type": "STRING"},
        },
        "required": ["action"],
    },
    "handler": background_tasks,
}