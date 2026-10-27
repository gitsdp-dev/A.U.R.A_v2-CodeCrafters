"""Safety policy for IDE-agent filesystem and command operations."""

from __future__ import annotations

import os
import shlex
from pathlib import Path


class ProjectPolicy:
    """Keep agent operations inside one project and protect sensitive paths."""

    protected_names = {".git", ".env", ".env.local", "id_rsa", "id_ed25519"}
    protected_suffixes = (".pem", ".key", ".p12", ".pfx")

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise NotADirectoryError(str(self.root))

    def path(self, value: str | Path, *, allow_missing: bool = True) -> Path:
        """Resolve a project-relative path and reject traversal or secrets."""
        candidate = Path(value)
        resolved = (self.root / candidate if not candidate.is_absolute() else candidate).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise PermissionError(f"path is outside project root: {value}")
        relative = resolved.relative_to(self.root)
        if self.is_protected(relative):
            raise PermissionError(f"protected path is not available to the IDE agent: {relative}")
        if not allow_missing and not resolved.exists():
            raise FileNotFoundError(str(relative))
        return resolved

    @classmethod
    def is_protected(cls, relative: str | Path) -> bool:
        """Return whether a project-relative path contains secrets or git data."""
        parts = [part.casefold() for part in Path(relative).parts]
        if not parts:
            return False
        if any(part in cls.protected_names for part in parts):
            return True
        return any(Path(parts[-1]).suffix.casefold() == suffix for suffix in cls.protected_suffixes)

    def command(self, value: str, *, allow_install: bool = False) -> list[str]:
        """Validate and tokenize a whitelisted project command."""
        command = str(value or "").strip()
        if not command:
            raise ValueError("command is required")
        if any(token in command for token in ("&&", "||", ";", "|", ">", "<", "`", "$(", "%")):
            raise PermissionError("shell operators and variable expansion are not allowed")
        parts = shlex.split(command, posix=os.name != "nt")
        if not parts:
            raise ValueError("command is empty")
        executable = Path(parts[0]).name.casefold()
        allowed = {"python", "py", "pytest", "ruff", "flake8", "mypy", "npm", "node", "git"}
        if allow_install:
            allowed.update({"pip", "poetry", "uv"})
        if executable not in allowed:
            raise PermissionError(f"command is not whitelisted: {executable}")
        if executable == "git" and any(part in {"reset", "clean", "push", "checkout"} for part in parts[1:]):
            raise PermissionError("destructive or remote git commands are blocked")
        if executable in {"pip", "poetry", "uv"} and not allow_install:
            raise PermissionError("package installation requires explicit approval")
        return parts