from __future__ import annotations

import datetime as _datetime
import os
import platform
import subprocess
from pathlib import Path
from typing import Callable

try:
    from smolagents import tool
except ImportError:
    def tool(function):
        return function

_workspace = Path.cwd() / "workspace"
_confirm: Callable[[str], bool] | None = None


def configure_tools(workspace: str, confirmer: Callable[[str], bool]) -> None:
    global _workspace, _confirm
    _workspace = Path(workspace).expanduser().resolve()
    _workspace.mkdir(parents=True, exist_ok=True)
    _confirm = confirmer


def _within_workspace(path: str) -> Path:
    target = (_workspace / path).resolve()
    if not target.is_relative_to(_workspace):
        raise ValueError("Path is outside the configured workspace.")
    return target


def _confirm_action(question: str) -> bool:
    if _confirm is None:
        raise RuntimeError("Confirmation is unavailable; this action is blocked.")
    return _confirm(question)


@tool
def list_directory(path: str) -> str:
    """List entries in a workspace directory.

    Args:
        path: Directory path relative to the allowed workspace.

    Returns:
        Names of entries in the requested directory.
    """
    target = _within_workspace(path)
    if not target.is_dir():
        raise NotADirectoryError(str(target))
    return "\n".join(sorted(item.name for item in target.iterdir()))


@tool
def read_text_file(path: str) -> str:
    """Read a UTF-8 text file inside the allowed workspace.

    Args:
        path: File path relative to the allowed workspace.

    Returns:
        The file contents.
    """
    target = _within_workspace(path)
    if not target.is_file():
        raise FileNotFoundError(str(target))
    return target.read_text(encoding="utf-8")


@tool
def write_text_file(filename: str, content: str) -> str:
    """Create a workspace text file, asking before overwriting an existing file.

    Args:
        filename: File path relative to the allowed workspace.
        content: UTF-8 text to write.

    Returns:
        The relative path of the written file.
    """
    target = _within_workspace(filename)
    if target.exists() and not _confirm_action(
        f"The file {target.name} already exists. May I overwrite it?"
    ):
        return "The file was not overwritten."
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return str(target.relative_to(_workspace))


@tool
def create_folder(path: str) -> str:
    """Create a directory inside the allowed workspace.

    Args:
        path: New directory path relative to the allowed workspace.

    Returns:
        The relative path of the directory.
    """
    target = _within_workspace(path)
    target.mkdir(parents=True, exist_ok=True)
    return str(target.relative_to(_workspace))


@tool
def open_application(name: str) -> str:
    """Open an application by name after explicit user confirmation.

    Args:
        name: Application name or executable path.

    Returns:
        Status of the open request.
    """
    if not _confirm_action(f"May I open the application {name}?"):
        return "The application was not opened."
    if not name.strip() or any(char in name for char in "\r\n"):
        raise ValueError("A valid application name is required.")
    if os.name == "nt":
        os.startfile(name)
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", name])
    else:
        subprocess.Popen(["xdg-open", name])
    return f"Opening {name}."


@tool
def get_system_info() -> str:
    """Return basic operating system and Python runtime information.

    Returns:
        Platform name, release, and Python version.
    """
    return f"{platform.system()} {platform.release()}, Python {platform.python_version()}"


@tool
def get_time_date() -> str:
    """Return the current local time and date.

    Returns:
        Current local date and time.
    """
    return _datetime.datetime.now().astimezone().strftime("%A, %d %B %Y, %H:%M %Z")
