"""Project inspection, safe editing, testing, and IDE integration services."""

from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from core import gemini

from .ide import IDEAdapter, active_window_title, detect_adapter
from .patches import apply_unified_diff, patch_files
from .policy import ProjectPolicy


class IDEAgentService:
    """Perform Phase 2 operations inside a detected or explicit project root."""

    def __init__(self, root: str | Path | None = None, adapter: IDEAdapter | None = None):
        self.root = self.detect_root(root)
        self.policy = ProjectPolicy(self.root)
        self.adapter = adapter or detect_adapter()

    @staticmethod
    def detect_root(explicit: str | Path | None = None) -> Path:
        """Detect a project root from explicit input, environment, cwd, or title."""
        if explicit:
            selected = Path(explicit).expanduser().resolve()
            if selected.is_file():
                selected = selected.parent
            if not selected.is_dir():
                raise NotADirectoryError(str(selected))
            return selected
        candidates = []
        if os.environ.get("AURA_PROJECT_ROOT"):
            candidates.append(Path(os.environ["AURA_PROJECT_ROOT"]))
        candidates.append(Path.cwd())
        title = active_window_title()
        if title:
            candidates.extend(Path(part) for part in title.split() if Path(part).is_dir())
        for candidate in candidates:
            current = candidate.expanduser().resolve()
            if current.is_file():
                current = current.parent
            for root in (current, *current.parents):
                if (root / ".git").exists() or any((root / marker).exists() for marker in (
                    "pyproject.toml", "setup.py", "package.json", "Cargo.toml", ".sln"
                )):
                    return root
        return candidates[0].expanduser().resolve()

    def list_tree(self, path: str = ".", max_entries: int = 500) -> str:
        """List project files while excluding protected and cache paths."""
        base = self.policy.path(path, allow_missing=False)
        if not base.is_dir():
            raise NotADirectoryError(str(base))
        rows = []
        for item in sorted(base.rglob("*")):
            if len(rows) >= max_entries:
                break
            relative = item.relative_to(self.root)
            if self.policy.is_protected(relative) or "__pycache__" in relative.parts:
                continue
            rows.append(str(relative).replace("\\", "/") + ("/" if item.is_dir() else ""))
        return "\n".join(rows)

    def read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        """Read a bounded UTF-8 line range from the project."""
        target = self.policy.path(path, allow_missing=False)
        if not target.is_file():
            raise FileNotFoundError(path)
        lines = target.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[max(0, start - 1):end])

    def search_code(self, query: str, glob: str = "*") -> str:
        """Search text files without invoking an untrusted shell."""
        if not query:
            raise ValueError("search query is required")
        rows = []
        for target in self.root.rglob(glob):
            if not target.is_file() or self.policy.is_protected(target.relative_to(self.root)):
                continue
            try:
                for number, line in enumerate(target.read_text(encoding="utf-8").splitlines(), 1):
                    if query.casefold() in line.casefold():
                        rows.append(f"{target.relative_to(self.root)}:{number}:{line[:240]}")
            except (OSError, UnicodeDecodeError):
                continue
        return "\n".join(rows[:200])

    def apply_patch(self, patch: str, confirmed: bool = False) -> str:
        """Preview or apply a unified diff with atomic rollback."""
        files = patch_files(patch)
        if not files:
            raise ValueError("patch contains no editable files")
        if not confirmed and (len(files) > 1 or patch.count("\n+") > 80):
            return "CONFIRMATION_REQUIRED\n" + self.preview_patch(patch)
        changed = apply_unified_diff(self.policy, patch)
        return "Applied patch:\n" + "\n".join(str(item.relative_to(self.root)) for item in changed)

    def preview_patch(self, patch: str) -> str:
        """Return a bounded patch preview for the UI."""
        return patch[:12000]

    def create_file(self, path: str, content: str, confirmed: bool = False) -> str:
        """Create a new file without overwriting an existing file."""
        target = self.policy.path(path)
        if target.exists():
            raise FileExistsError(path)
        if not confirmed and len(content) > 4000:
            return "CONFIRMATION_REQUIRED\n" + content[:12000]
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".aura-tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, target)
        return f"Created {path}"

    def run_command(self, command: str, timeout: int = 60, allow_install: bool = False) -> str:
        """Run one whitelisted command from the project root."""
        parts = self.policy.command(command, allow_install=allow_install)
        result = subprocess.run(parts, cwd=self.root, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=min(timeout, 600))
        output = (result.stdout + ("\n" + result.stderr if result.stderr else "")).strip()
        return f"exit_code={result.returncode}\n{output}".strip()

    def run_tests(self, command: str = "pytest -q", timeout: int = 300) -> str:
        """Run the project's configured test command through the whitelist."""
        return self.run_command(command, timeout=timeout)

    def git(self, operation: str) -> str:
        """Return git status/diff or create an explicitly requested local commit."""
        commands = {
            "status": ["git", "status", "--short"],
            "diff": ["git", "diff", "--"],
        }
        if operation in commands:
            result = subprocess.run(commands[operation], cwd=self.root, capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=30)
            return result.stdout.strip() or result.stderr.strip() or "No changes."
        raise PermissionError("only git status and diff are available in this operation")

    def git_commit(self, message: str, paths: list[str] | None = None) -> str:
        """Create a local commit for explicitly named project paths."""
        commit_message = str(message or "").strip()
        if not commit_message:
            raise ValueError("commit message is required")
        selected = paths or ["."]
        safe_paths = [str(self.policy.path(path).relative_to(self.root)) for path in selected]
        staged = subprocess.run(
            ["git", "add", "--", *safe_paths], cwd=self.root,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        if staged.returncode != 0:
            raise RuntimeError(staged.stderr.strip() or "git add failed")
        committed = subprocess.run(
            ["git", "commit", "-m", commit_message], cwd=self.root,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        output = (committed.stdout + ("\n" + committed.stderr if committed.stderr else "")).strip()
        if committed.returncode != 0:
            raise RuntimeError(output or "git commit failed")
        return output

    def code_task(self, request: str, confirmed: bool = False,
                  confirm: Callable[[str, str, Callable[[], str]], str] | None = None) -> str:
        """Ask Gemini for a minimal diff, preview it, and apply it safely."""
        if not request.strip():
            raise ValueError("coding request is required")
        tree = self.list_tree()
        prompt = (
            "You are editing an existing project. Return only JSON with keys "
            "summary, patch, tests. The patch must be a minimal unified diff "
            "using paths relative to the project root. Never edit .git, .env, "
            "credentials, or generated files.\n"
            f"Project tree:\n{tree[:12000]}\n"
            f"Request:\n{request}\n"
        )
        response = gemini.text(prompt, tier=gemini.SMART, timeout_ms=90_000)
        if not response:
            raise RuntimeError("Gemini returned no coding plan")
        payload = json.loads(response.strip().removeprefix("```json").removesuffix("```").strip())
        patch = str(payload.get("patch") or "")
        result = self.apply_patch(patch, confirmed=confirmed)
        if result.startswith("CONFIRMATION_REQUIRED") and confirm is not None:
            return confirm(
                "Apply IDE agent code changes",
                self.preview_patch(patch),
                lambda: self.apply_patch(patch, confirmed=True),
            )
        return f"{payload.get('summary', 'Coding task processed')}\n{result}"

    def open_file(self, path: str, line: int | None = None) -> str:
        """Open a project file through the detected IDE adapter."""
        target = self.policy.path(path, allow_missing=False)
        return "Opened in " + self.adapter.name if self.adapter.open_file(target, line) else "No supported IDE CLI found."