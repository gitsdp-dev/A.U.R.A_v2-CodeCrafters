"""
updater_helper.py — Standalone staged-apply helper for A.U.R.A updates.

This script is launched as a *detached* process by the main app.  It:
  1. Waits for the parent AURA process to exit.
  2. Creates a backup of every file that will be overwritten.
  3. Copies staged files into BASE_DIR (skipping protected paths).
  4. Writes the new version to version.json.
  5. If requirements.txt changed, installs new dependencies.
  6. Deletes the staging folder and backup on success.
  7. On any failure, rolls back from the backup and relaunches AURA.

Uses ONLY the standard library — no third-party packages.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [AURA-Helper] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("aura.updater_helper")


# ─────────────────────────────────────────────────────────────────────────────
# Protected-path logic (duplicated here so this file is self-contained)
# ─────────────────────────────────────────────────────────────────────────────

#: Must stay in sync with updater.PROTECTED_PATHS.
_PROTECTED_PATTERNS: list[str] = [
    "config",
    "config/*",
    "memory/long_term.json",
    "memory/notifications.json",
    "config/tg_chats.json",
    "config/tg_chat_log.json",
    ".env",
    "*.env",
    "logs",
    "logs/*",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.pkl",
    "*.pickle",
    "memory_*",
    "models/*",
    "vosk-model*",
    "venv",
    "venv/*",
    ".venv",
    ".venv/*",
    ".git",
    ".git/*",
    "__pycache__",
    "__pycache__/*",
    ".updater_state.json",
    "version.json",          # only written AFTER successful apply
    ".update_staging",
    ".update_staging/*",
    ".update_backup",
    ".update_backup/*",
    "updater_helper.py",
    "config/api_keys.json",
    "config/face_unlock.json",
    "config/face_unlock",
    "config/face_unlock/*",
    "config/*.json",
    "config/*.tmp",
]


def _is_protected(rel: str) -> bool:
    p = Path(rel).as_posix().lower()
    for pat in _PROTECTED_PATTERNS:
        pl = pat.lower()
        if p == pl:
            return True
        if pl.endswith("/*"):
            prefix = pl[:-2]
            if p.startswith(prefix + "/") or p == prefix:
                return True
        if fnmatch.fnmatch(p, pl):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Process-wait helper
# ─────────────────────────────────────────────────────────────────────────────

def _wait_for_pid(pid: int, timeout: float = 30.0) -> bool:
    """Wait up to *timeout* seconds for process *pid* to exit.

    Returns True when it has exited, False if still alive after the timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = _pid_alive(pid)
        if not alive:
            return True
        time.sleep(0.25)
    return not _pid_alive(pid)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if platform.system() == "Windows":
        try:
            import ctypes
            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not handle:
                return False
            result = ctypes.windll.kernel32.WaitForSingleObject(handle, 0)
            ctypes.windll.kernel32.CloseHandle(handle)
            return result != 0  # 0 = WAIT_OBJECT_0 (already exited)
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # process exists but we can't signal it


# ─────────────────────────────────────────────────────────────────────────────
# Backup / apply / rollback
# ─────────────────────────────────────────────────────────────────────────────

def _backup_files(base: Path, staging: Path, backup: Path) -> list[str]:
    """Copy every file from *staging* that exists in *base* into *backup*.

    Returns the list of relative paths that were backed up.
    """
    backed_up: list[str] = []
    for src in staging.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(staging).as_posix()
        if _is_protected(rel):
            continue
        dest_in_base = base / rel
        if dest_in_base.exists():
            bk = backup / rel
            bk.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest_in_base, bk)
            backed_up.append(rel)
    return backed_up


def _apply_files(base: Path, staging: Path) -> list[str]:
    """Copy staging → base, skipping protected paths.

    Returns the list of relative paths that were applied.
    Raises on any I/O error.
    """
    applied: list[str] = []
    for src in staging.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(staging).as_posix()
        if _is_protected(rel):
            # Only add it if it doesn't exist locally yet (first-run defaults).
            dest = base / rel
            if dest.exists():
                log.debug("Protected, skipping: %s", rel)
                continue
            else:
                log.debug("Protected but absent locally — adding default: %s", rel)
        dest = base / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        applied.append(rel)
    return applied


def _rollback(base: Path, backup: Path, applied: list[str]) -> None:
    """Restore every file in *applied* from *backup*, or remove it if no backup."""
    for rel in applied:
        dest  = base / rel
        bk    = backup / rel
        if bk.exists():
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bk, dest)
                log.info("Rolled back: %s", rel)
            except OSError as exc:
                log.error("Rollback copy failed for %s: %s", rel, exc)
        else:
            try:
                dest.unlink(missing_ok=True)
                log.info("Removed (new file, no backup): %s", rel)
            except OSError as exc:
                log.error("Rollback delete failed for %s: %s", rel, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:  # noqa: C901
    parser = argparse.ArgumentParser(description="A.U.R.A update helper")
    parser.add_argument("--staging",  required=True, help="Path to staging directory")
    parser.add_argument("--base",     required=True, help="A.U.R.A installation directory")
    parser.add_argument("--version",  required=True, help="New version string (e.g. v2.1.0)")
    parser.add_argument("--entry",    required=True, help="Entry-point .py to relaunch")
    parser.add_argument("--pid",      required=True, type=int, help="PID of AURA to wait for")
    args = parser.parse_args()

    base    = Path(args.base).resolve()
    staging = Path(args.staging).resolve()
    backup  = base / ".update_backup"
    entry   = Path(args.entry).resolve()

    log.info("=== A.U.R.A Update Helper starting ===")
    log.info("Waiting for AURA (PID %d) to exit…", args.pid)

    if not _wait_for_pid(args.pid, timeout=45.0):
        log.error("AURA did not exit within 45 s — aborting.")
        _relaunch(entry, base)
        return 1

    log.info("AURA has exited. Proceeding with update.")

    # ── Back up ──────────────────────────────────────────────────────────────
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    backup.mkdir(parents=True)

    log.info("Creating backup in %s", backup)
    try:
        backed = _backup_files(base, staging, backup)
        log.info("Backed up %d files.", len(backed))
    except Exception as exc:
        log.error("Backup phase failed: %s", exc)
        _relaunch(entry, base)
        return 1

    # ── Apply ─────────────────────────────────────────────────────────────────
    log.info("Applying staged files…")
    applied: list[str] = []
    try:
        applied = _apply_files(base, staging)
        log.info("Applied %d files.", len(applied))
    except Exception as exc:
        log.error("Apply phase failed: %s — rolling back.", exc)
        _rollback(base, backup, applied)
        _relaunch(entry, base)
        return 1

    # ── Install requirements if changed ──────────────────────────────────────
    req_path = base / "requirements.txt"
    old_req  = backup / "requirements.txt"
    req_changed = False
    if req_path.exists():
        if not old_req.exists():
            req_changed = True
        else:
            try:
                req_changed = (req_path.read_bytes() != old_req.read_bytes())
            except Exception:
                req_changed = True

    if req_changed:
        log.info("requirements.txt changed — installing/updating dependencies…")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(req_path)],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                log.error("pip install failed (exit %d):\n%s", result.returncode, result.stderr)
                log.info("Rolling back due to pip failure…")
                _rollback(base, backup, applied)
                _relaunch(entry, base)
                return 1
            log.info("Requirements installed successfully.")
        except Exception as exc:
            log.error("pip install raised an exception: %s — rolling back.", exc)
            _rollback(base, backup, applied)
            _relaunch(entry, base)
            return 1
    else:
        log.info("requirements.txt unchanged — skipping dependency installation.")

    # ── Write version.json ────────────────────────────────────────────────────
    version_file = base / "version.json"
    try:
        tmp = version_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": args.version}, indent=2), encoding="utf-8")
        os.replace(tmp, version_file)
        log.info("version.json written: %s", args.version)
    except Exception as exc:
        log.error("Could not write version.json: %s", exc)
        # Not fatal — AURA can fingerprint, but log it clearly.

    # ── Clear updater badge state ─────────────────────────────────────────────
    state_file = base / ".updater_state.json"
    try:
        state: dict = {}
        if state_file.exists():
            state = json.loads(state_file.read_text(encoding="utf-8"))
        state["badge"]       = False
        state.pop("pending_tag", None)
        tmp = state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, state_file)
    except Exception as exc:
        log.warning("Could not clear updater state: %s", exc)

    # ── Cleanup staging and backup ────────────────────────────────────────────
    try:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(backup,  ignore_errors=True)
        log.info("Staging and backup directories removed.")
    except Exception as exc:
        log.warning("Cleanup failed: %s", exc)

    log.info("=== Update applied successfully. Relaunching A.U.R.A ===")
    _relaunch(entry, base)
    return 0


def _relaunch(entry: Path, cwd: Path) -> None:
    """Relaunch A.U.R.A with the same Python interpreter, no console window."""
    cmd = [sys.executable, str(entry)]
    log.info("Relaunching: %s", " ".join(cmd))

    kw: dict = {"cwd": str(cwd)}
    if platform.system() == "Windows":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True

    try:
        subprocess.Popen(cmd, **kw)  # noqa: S603
    except Exception as exc:
        log.error("Failed to relaunch AURA: %s", exc)


if __name__ == "__main__":
    sys.exit(main())
