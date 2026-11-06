"""Small, bounded subprocess adapter for the agent-desktop JSON CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AgentDesktopError(RuntimeError):
    """A CLI failure with safe retry metadata from its JSON envelope."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "CLI_ERROR",
        suggestion: str = "",
        retry: str = "unknown",
    ):
        super().__init__(message)
        self.code = code
        self.suggestion = suggestion
        self.retry = retry


@dataclass(frozen=True)
class CommandResult:
    """A validated agent-desktop envelope."""

    data: dict[str, Any]
    envelope: dict[str, Any]


class AgentDesktopClient:
    """Run allow-listed CLI commands without a shell or visible console window."""

    COMMANDS = frozenset({
        "snapshot", "find", "click", "type", "set-value", "press", "scroll",
        "launch", "close-app", "focus-window", "list-apps", "list-windows",
        "wait", "batch", "clipboard-get", "clipboard-set", "clipboard-clear",
        "maximize", "minimize", "restore", "move-window", "resize-window",
    })

    def __init__(
        self,
        cancel_event: threading.Event | None = None,
        *,
        executable: str | None = None,
        headed: bool = False,
        timeout: float = 15.0,
    ):
        self.cancel_event = cancel_event or threading.Event()
        self.executable = executable or shutil.which("agent-desktop")
        self.headed = bool(headed)
        self.timeout = max(0.1, float(timeout))

    @property
    def available(self) -> bool:
        return bool(self.executable)

    def run(self, command: str, *args: str, timeout: float | None = None) -> CommandResult:
        """Run one CLI operation and parse only its single JSON response envelope."""
        if command not in self.COMMANDS:
            raise AgentDesktopError("Unsupported agent-desktop command.", code="INVALID_COMMAND")
        if self.cancel_event.is_set():
            raise AgentDesktopError("Screen Agent was stopped.", code="CANCELLED")
        if not self.executable:
            raise AgentDesktopError(
                "agent-desktop is not installed. Install it with `npm install -g agent-desktop`.",
                code="NOT_INSTALLED",
            )

        argv = self._argv(command, args)
        if self.headed:
            argv.insert(1, "--headed")

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                shell=False,
            )
        except OSError as exc:
            raise AgentDesktopError(
                f"Could not start agent-desktop ({type(exc).__name__}).",
                code="PROCESS_START_FAILED",
            ) from exc

        deadline = time.monotonic() + (timeout or self.timeout)
        stdout = ""
        while True:
            if self.cancel_event.is_set():
                process.terminate()
                try:
                    process.communicate(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise AgentDesktopError("Screen Agent was stopped.", code="CANCELLED")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.communicate()
                raise AgentDesktopError(
                    f"agent-desktop {command} timed out.",
                    code="CLIENT_TIMEOUT",
                )
            try:
                stdout, _ = process.communicate(timeout=min(0.15, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
            except OSError as exc:
                process.kill()
                process.communicate()
                raise AgentDesktopError(
                    f"Could not read agent-desktop output ({type(exc).__name__}).",
                    code="PROCESS_IO_ERROR",
                ) from exc

        try:
            envelope = json.loads(stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise AgentDesktopError(
                f"agent-desktop {command} returned invalid JSON.",
                code="INVALID_JSON",
            ) from exc
        if not isinstance(envelope, dict) or not isinstance(envelope.get("ok"), bool):
            raise AgentDesktopError(
                f"agent-desktop {command} returned an invalid response envelope.",
                code="INVALID_ENVELOPE",
            )
        if not envelope["ok"]:
            error = envelope.get("error")
            if not isinstance(error, dict):
                raise AgentDesktopError(
                    f"agent-desktop {command} failed without error details.",
                    code="MALFORMED_ERROR",
                )
            disposition = error.get("disposition")
            retry = disposition.get("retry", "unknown") if isinstance(disposition, dict) else "unknown"
            raise AgentDesktopError(
                f"agent-desktop {command} failed ({error.get('code') or 'CLI_ERROR'!s}).",
                code=str(error.get("code") or "CLI_ERROR"),
                suggestion=str(error.get("suggestion") or ""),
                retry=str(retry),
            )
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise AgentDesktopError(
                f"agent-desktop {command} returned no data object.",
                code="MALFORMED_DATA",
            )
        return CommandResult(data=data, envelope=envelope)

    def _argv(self, command: str, args: tuple[str, ...]) -> list[str]:
        """Resolve npm's Windows shim to its JavaScript entry point, avoiding cmd.exe."""
        assert self.executable is not None
        path = Path(self.executable)
        if os.name == "nt" and path.suffix.casefold() in {".cmd", ".bat"}:
            node = shutil.which("node")
            package_json = path.parent / "node_modules" / "agent-desktop" / "package.json"
            if not node or not package_json.is_file():
                raise AgentDesktopError(
                    "Could not safely resolve the agent-desktop npm launcher.",
                    code="CLI_RESOLUTION_FAILED",
                )
            try:
                package = json.loads(package_json.read_text(encoding="utf-8"))
                bin_entry = package.get("bin")
                if isinstance(bin_entry, dict):
                    script = bin_entry.get("agent-desktop")
                else:
                    script = bin_entry
                entry = (package_json.parent / str(script)).resolve()
            except (OSError, ValueError, TypeError) as exc:
                raise AgentDesktopError(
                    "Could not resolve the agent-desktop npm entry point.",
                    code="CLI_RESOLUTION_FAILED",
                ) from exc
            if not script or not entry.is_file():
                raise AgentDesktopError(
                    "Could not find the agent-desktop npm entry point.",
                    code="CLI_RESOLUTION_FAILED",
                )
            return [node, str(entry), command, *(str(arg) for arg in args)]
        return [self.executable, command, *(str(arg) for arg in args)]
