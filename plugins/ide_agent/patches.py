"""Unified-diff parsing and atomic apply/rollback helpers."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from .policy import ProjectPolicy

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def patch_files(patch: str) -> list[str]:
    """Return project-relative files named by a unified diff."""
    files = []
    for line in patch.splitlines():
        if line.startswith("+++ "):
            name = line[4:].split("\t", 1)[0].strip()
            if name != "/dev/null":
                files.append(name.removeprefix("b/"))
    return list(dict.fromkeys(files))


def _apply_file(policy: ProjectPolicy, path: str, lines: list[str]) -> tuple[Path, str | None]:
    target = policy.path(path)
    original = target.read_text(encoding="utf-8") if target.exists() else None
    source = (original or "").splitlines(keepends=True)
    output: list[str] = []
    source_index = 0
    index = 0
    while index < len(lines):
        match = _HUNK.match(lines[index])
        if not match:
            index += 1
            continue
        start = int(match.group(1)) - 1
        output.extend(source[source_index:start])
        source_index = start
        index += 1
        while index < len(lines) and not lines[index].startswith("@@ "):
            line = lines[index]
            if line.startswith(" "):
                expected = line[1:] + ("\n" if not line[1:].endswith("\n") else "")
                if source_index >= len(source) or source[source_index].rstrip("\r\n") != expected.rstrip("\r\n"):
                    raise ValueError(f"patch context mismatch in {path}")
                output.append(source[source_index])
                source_index += 1
            elif line.startswith("-"):
                expected = line[1:]
                if source_index >= len(source) or source[source_index].rstrip("\r\n") != expected.rstrip("\r\n"):
                    raise ValueError(f"patch deletion mismatch in {path}")
                source_index += 1
            elif line.startswith("+"):
                output.append(line[1:] + ("\n" if not line[1:].endswith("\n") else ""))
            elif line != "\\ No newline at end of file":
                raise ValueError(f"invalid patch line in {path}")
            index += 1
    output.extend(source[source_index:])
    return target, "".join(output)


def apply_unified_diff(policy: ProjectPolicy, patch: str) -> list[Path]:
    """Apply a multi-file unified diff atomically, rolling back on failure."""
    if not patch.strip():
        raise ValueError("patch is empty")
    grouped: dict[str, list[str]] = {}
    current: str | None = None
    for line in patch.splitlines():
        if line.startswith("--- "):
            continue
        if line.startswith("+++ "):
            current = line[4:].split("\t", 1)[0].removeprefix("b/")
            if current == "/dev/null":
                raise PermissionError("file deletion patches are not supported")
            grouped[current] = []
            continue
        if current is not None:
            grouped[current].append(line)
    if not grouped:
        raise ValueError("patch contains no files")

    changes: list[tuple[Path, str | None, str]] = []
    for path, hunks in grouped.items():
        target, content = _apply_file(policy, path, hunks)
        previous = target.read_text(encoding="utf-8") if target.exists() else None
        changes.append((target, previous, content))
    backups: list[tuple[Path, Path | None]] = []
    try:
        for target, previous, content in changes:
            backup = None
            if previous is not None:
                fd, name = tempfile.mkstemp(prefix="aura-agent-", suffix=".bak")
                os.close(fd)
                backup = Path(name)
                backup.write_text(previous, encoding="utf-8")
            backups.append((target, backup))
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".aura-tmp")
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, target)
    except Exception:
        for target, backup in reversed(backups):
            if backup is not None and backup.exists():
                shutil.copyfile(backup, target)
        raise
    finally:
        for _, backup in backups:
            if backup is not None:
                backup.unlink(missing_ok=True)
    return [target for target, _, _ in changes]