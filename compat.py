"""Runtime compatibility helpers shared by A.U.R.A launchers and features."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Callable

LOGGER = logging.getLogger("aura.compat")
MIN_PYTHON = (3, 10)
TESTED_THROUGH = (3, 14)


if hasattr(asyncio, "TaskGroup"):
    TaskGroup = asyncio.TaskGroup
else:
    class TaskGroup:
        """Small TaskGroup-compatible fallback for Python 3.10."""

        def __init__(self):
            self._tasks = set()

        async def __aenter__(self):
            return self

        def create_task(self, coroutine, *, name=None):
            task = asyncio.create_task(coroutine, name=name)
            self._tasks.add(task)
            return task

        async def __aexit__(self, exc_type, exc, traceback):
            if exc is not None:
                for task in self._tasks:
                    task.cancel()
                await asyncio.gather(*self._tasks, return_exceptions=True)
                return False

            done, pending = await asyncio.wait(
                self._tasks, return_when=asyncio.FIRST_EXCEPTION
            )
            failures = [
                task.exception()
                for task in done
                if not task.cancelled() and task.exception() is not None
            ]
            if failures:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                raise failures[0]
            if pending:
                await asyncio.gather(*pending)
            return False


def check_python_version(
    version_info=None, log: Callable[[str], None] | None = None
) -> bool:
    """Reject unsupported old runtimes and warn, without blocking, on newer ones."""
    version = tuple(version_info or sys.version_info)[:2]
    message = (
        f"Python {version[0]}.{version[1]} is below A.U.R.A's minimum "
        f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}."
    )
    if version < MIN_PYTHON:
        if log:
            log(message)
        else:
            print(f"[AURA] {message}", file=sys.stderr)
        raise SystemExit(1)
    if version > TESTED_THROUGH:
        message = (
            f"Python {version[0]}.{version[1]} is newer than the tested range "
            f"(3.10–3.14); continuing in best-effort mode."
        )
        (log or LOGGER.warning)(message)
    return True


def _venv_interpreter(root: Path) -> Path:
    if sys.platform == "win32":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def get_python_executable(project_root: str | Path | None = None) -> str | None:
    """Return the interpreter carrying A.U.R.A's packages, if one is available."""
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parent
    venv_python = _venv_interpreter(root)
    if venv_python.is_file():
        try:
            running = Path(sys.executable).resolve()
            preferred = venv_python.resolve()
            running_in_venv = running == preferred or running.parent == preferred.parent
        except OSError:
            running_in_venv = False
        if not running_in_venv:
            LOGGER.info("Using project virtual-environment interpreter: %s", venv_python)
        return str(venv_python)

    frozen = bool(getattr(sys, "frozen", False) or "__compiled__" in globals())
    if not frozen:
        current = Path(sys.executable)
        if os.name == "nt" and current.name.casefold() == "pythonw.exe":
            console_python = current.with_name("python.exe")
            if console_python.is_file():
                return str(console_python)
        return sys.executable

    configured = os.environ.get("AURA_PYTHON_EXECUTABLE")
    candidates = [Path(configured)] if configured else []
    bundled = (
        (root / "python.exe", root / "python" / "python.exe")
        if os.name == "nt"
        else (root / "bin" / "python", root / "python" / "bin" / "python")
    )
    candidates.extend(path for path in bundled if path.is_file())
    names = ("python.exe", "python3.exe", "python") if os.name == "nt" else ("python3", "python")
    candidates.extend(Path(found) for name in names if (found := shutil.which(name)))
    for candidate in candidates:
        if candidate.is_file():
            LOGGER.info("Using external Python for a frozen-build feature: %s", candidate)
            return str(candidate)
    LOGGER.warning(
        "This frozen A.U.R.A build has no bundled or system Python; "
        "Python-script features are unavailable."
    )
    return None
