"""
updater.py — A.U.R.A in-app update system.

Architecture
------------
* Version parsing / comparison  — SemVer class
* Protected-path catalogue      — PROTECTED_PATHS, is_protected()
* Release fetching              — fetch_releases(), get_latest_release()
* Local-version detection       — read_version_json(), fingerprint_local_version()
* Download / stage              — ReleaseDownloader (QThread worker)
* Apply helper launcher         — launch_helper()
* Qt UI helpers (imported by ui.py):
    CheckUpdatesButton, UpdateDialog, UpdaterController

Only stdlib + PyQt6 are used.  ``requests`` is already in requirements.txt
but deliberately NOT used here — urllib keeps this module zero-dependency at
the updater level, consistent with the spec.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional

# ── PyQt6 imports (only the essentials; UI classes live below) ───────────────
from PyQt6.QtCore import QObject, QThread, pyqtSignal

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

REPO_OWNER = "gitsdp-dev"
REPO_NAME  = "A.U.R.A_v2-CodeCrafters"

RELEASES_API_URL = (
    f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/releases"
)

#: Per-request timeout in seconds for all network calls.
NETWORK_TIMEOUT = 12

#: Minimum tag we accept as a valid release (inclusive).
MIN_VERSION = (2, 0, 0)

#: Path of the file that records the installed version.
VERSION_JSON_NAME = "version.json"

#: State file that persists the "Later" badge state across restarts.
UPDATER_STATE_FILE = ".updater_state.json"

#: Staging + backup folder names (relative to BASE_DIR).
STAGING_DIR_NAME  = ".update_staging"
BACKUP_DIR_NAME   = ".update_backup"
HELPER_SCRIPT_NAME = "updater_helper.py"

#: User-Agent sent to GitHub.
USER_AGENT = f"AURA-Updater/1.0 ({platform.system()}; Python/{sys.version.split()[0]})"

log = logging.getLogger("aura.updater")


# ─────────────────────────────────────────────────────────────────────────────
# Base-dir helper (same pattern as the rest of the project)
# ─────────────────────────────────────────────────────────────────────────────

def _base_dir() -> Path:
    """Return the installation root directory."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR = _base_dir()


# ─────────────────────────────────────────────────────────────────────────────
# Protected paths
# ─────────────────────────────────────────────────────────────────────────────

#: Names / patterns of files and directories that must NEVER be overwritten
#: or deleted by an update.  Relative to BASE_DIR.
#: Patterns use fnmatch syntax (case-insensitive on Windows).
PROTECTED_PATHS: list[str] = [
    # Core user data
    "config",
    "config/*",
    "memory/long_term.json",
    "memory/notifications.json",
    # Telegram plugin stores
    "config/tg_chats.json",
    "config/tg_chat_log.json",
    # Environment / credentials
    ".env",
    "*.env",
    # Logs
    "logs",
    "logs/*",
    # Vector stores / databases
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.pkl",
    "*.pickle",
    # Memory-pattern files
    "memory_*",
    # Models downloaded at runtime
    "models/*",
    "vosk-model*",
    # Virtual environments
    "venv",
    "venv/*",
    ".venv",
    ".venv/*",
    # Git
    ".git",
    ".git/*",
    # Python byte-code cache
    "__pycache__",
    "__pycache__/*",
    # Updater own files — must not be trampled mid-run
    UPDATER_STATE_FILE,
    VERSION_JSON_NAME,
    STAGING_DIR_NAME,
    STAGING_DIR_NAME + "/*",
    BACKUP_DIR_NAME,
    BACKUP_DIR_NAME + "/*",
    HELPER_SCRIPT_NAME,
    # API keys
    "config/api_keys.json",
    # Face-unlock data
    "config/face_unlock.json",
    "config/face_unlock",
    "config/face_unlock/*",
    # Plugin state / tokens stored in config
    "config/*.json",
    "config/*.tmp",
]


def is_protected(rel_path: str | Path) -> bool:
    """Return True if *rel_path* (relative to BASE_DIR) matches any protected pattern."""
    p = Path(rel_path).as_posix()  # normalise to forward slashes
    p_lower = p.lower()
    for pat in PROTECTED_PATHS:
        pat_lower = pat.lower()
        # Exact prefix match for directories (e.g. "config" matches "config/foo")
        if p_lower == pat_lower:
            return True
        if pat_lower.endswith("/*"):
            prefix = pat_lower[:-2]
            if p_lower.startswith(prefix + "/") or p_lower == prefix:
                return True
        # fnmatch glob
        if fnmatch.fnmatch(p_lower, pat_lower):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Semantic version
# ─────────────────────────────────────────────────────────────────────────────

class SemVer(NamedTuple):
    """Immutable semantic version triple."""
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, tag: str) -> Optional["SemVer"]:
        """Parse a tag such as ``v2.1.0`` or ``2.1.0``.

        Returns ``None`` for anything that doesn't look like a semver tag.
        """
        cleaned = tag.strip().lstrip("vV")
        m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", cleaned)
        if not m:
            return None
        return cls(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    def __str__(self) -> str:
        return f"v{self.major}.{self.minor}.{self.patch}"

    def is_at_least_minimum(self) -> bool:
        return (self.major, self.minor, self.patch) >= MIN_VERSION


# ─────────────────────────────────────────────────────────────────────────────
# Version file I/O
# ─────────────────────────────────────────────────────────────────────────────

def read_version_json() -> Optional[SemVer]:
    """Read BASE_DIR/version.json and return its ``version`` field as a SemVer.

    Returns ``None`` if the file is missing, malformed, or has an unparseable
    version string.
    """
    path = BASE_DIR / VERSION_JSON_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tag = data.get("version", "")
        return SemVer.parse(str(tag))
    except Exception as exc:
        log.debug("read_version_json: %s", exc)
        return None


def write_version_json(version: SemVer) -> None:
    """Atomically write the installed version to ``version.json``."""
    path = BASE_DIR / VERSION_JSON_NAME
    tmp  = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": str(version)}, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    log.info("version.json updated → %s", version)


# ─────────────────────────────────────────────────────────────────────────────
# Updater state file (badge persistence)
# ─────────────────────────────────────────────────────────────────────────────

def read_updater_state() -> dict:
    """Load the persistent updater state (pending-update tag, badge flag, etc.)."""
    path = BASE_DIR / UPDATER_STATE_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_updater_state(state: dict) -> None:
    """Persist the updater state atomically."""
    path = BASE_DIR / UPDATER_STATE_FILE
    tmp  = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def clear_updater_badge() -> None:
    """Remove the pending-update badge flag from the state file."""
    state = read_updater_state()
    state.pop("pending_tag", None)
    state["badge"] = False
    write_updater_state(state)


def set_updater_badge(latest_tag: str) -> None:
    """Set the pending-update badge flag (user clicked Later)."""
    state = read_updater_state()
    state["badge"]       = True
    state["pending_tag"] = latest_tag
    write_updater_state(state)


def has_updater_badge() -> bool:
    """Return True if a pending-update badge should be shown."""
    return bool(read_updater_state().get("badge"))


# ─────────────────────────────────────────────────────────────────────────────
# GitHub API helpers
# ─────────────────────────────────────────────────────────────────────────────

class NetworkError(RuntimeError):
    """Raised for all network/HTTP problems during the update check."""


class RateLimitError(NetworkError):
    """Raised when GitHub returns 403/429 with rate-limit headers."""


def _make_request(url: str) -> urllib.request.Request:
    """Build a GET Request with the correct User-Agent and optional token."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def _open_url(url: str, timeout: int = NETWORK_TIMEOUT) -> Any:
    """Open *url* with SSL verification and a graceful fallback for broken CA stores.

    Returns the open response object.  Raises :class:`NetworkError` or one of
    its subclasses on any problem.
    """
    req = _make_request(url)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            reset = exc.headers.get("X-RateLimit-Reset", "")
            remaining = exc.headers.get("X-RateLimit-Remaining", "?")
            msg = (
                f"GitHub rate limit reached (HTTP {exc.code}). "
                f"Remaining: {remaining}. "
                + (f"Resets at: {time.strftime('%H:%M:%S', time.localtime(int(reset)))}" if reset else "")
            )
            raise RateLimitError(msg) from exc
        raise NetworkError(f"HTTP {exc.code}: {exc.reason} — {url}") from exc
    except urllib.error.URLError as exc:
        reason = str(exc.reason)
        if "ssl" in reason.lower() or "certificate" in reason.lower():
            # Try without verification (report it clearly)
            log.warning("SSL verification failed (%s) — retrying without verification", reason)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode    = ssl.CERT_NONE
            try:
                return urllib.request.urlopen(req, timeout=timeout, context=ctx)
            except Exception as inner:
                raise NetworkError(f"SSL error and insecure retry failed: {inner}") from inner
        if "timed out" in reason.lower() or isinstance(exc.reason, TimeoutError):
            raise NetworkError("Request timed out — check your internet connection.") from exc
        raise NetworkError(f"Connection error: {reason}") from exc
    except TimeoutError as exc:
        raise NetworkError("Request timed out — check your internet connection.") from exc


def fetch_releases() -> list[dict]:
    """Fetch the public releases list from the GitHub API.

    Returns the parsed JSON list.  Raises :class:`NetworkError` on failure.
    """
    log.debug("Fetching releases from %s", RELEASES_API_URL)
    try:
        with _open_url(RELEASES_API_URL) as resp:
            raw = resp.read()
    except NetworkError:
        raise
    except Exception as exc:
        raise NetworkError(f"Unexpected error fetching releases: {exc}") from exc

    try:
        releases = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise NetworkError(f"GitHub returned malformed JSON: {exc}") from exc

    if not isinstance(releases, list):
        raise NetworkError("GitHub API response is not a list of releases.")

    return releases


def get_latest_release(releases: list[dict]) -> Optional[dict]:
    """Find the highest valid semver release from *releases*.

    Rules:
    - Ignores drafts and pre-releases.
    - Only considers tags that parse as semver >= MIN_VERSION.
    - Returns the release with the highest semver regardless of list order.
    - Returns ``None`` if no valid release is found.
    """
    best_ver: Optional[SemVer] = None
    best_rel: Optional[dict]   = None

    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        tag = rel.get("tag_name", "")
        ver = SemVer.parse(tag)
        if ver is None or not ver.is_at_least_minimum():
            continue
        if best_ver is None or ver > best_ver:
            best_ver = ver
            best_rel = rel

    return best_rel


# ─────────────────────────────────────────────────────────────────────────────
# Release asset URL resolution
# ─────────────────────────────────────────────────────────────────────────────

#: Pattern for manually uploaded release assets.
#: Matches: source_code.zip, source_coe2.zip, source_code2.zip, source_code3.zip, …
_ASSET_NAME_RE = re.compile(
    r"^source[-_]?co(?:d|e)?e?[-_]?(\d*)\.zip$", re.IGNORECASE
)


def get_release_asset_url(release: dict) -> Optional[str]:
    """Return the ``browser_download_url`` of the best matching release asset.

    Priority:
    1. Any asset matching ``source_code.zip``, ``source_coe2.zip``, ``source_code2.zip``,
       ``source_code3.zip``, etc.  When multiple matching assets exist, the highest
       numbered and most recently updated asset is preferred.
    2. Fallback to any other ``.zip`` asset in the release.
    3. Returns ``None`` if no zip asset is attached (caller falls back to GitHub's
       auto-generated zipball).
    """
    assets: list[dict] = release.get("assets") or []
    if not assets:
        return None

    # (match_priority, numeric_suffix, updated_at, url)
    candidates: list[tuple[int, int, str, str]] = []

    for asset in assets:
        name = (asset.get("name") or "").strip()
        url  = (asset.get("browser_download_url") or "").strip()
        if not url:
            continue

        updated = str(asset.get("updated_at") or asset.get("created_at") or "")
        m = _ASSET_NAME_RE.match(name)
        if m:
            num_str = m.group(1)
            num = int(num_str) if num_str else 1
            candidates.append((2, num, updated, url))
        elif name.lower().endswith(".zip"):
            num_m = re.search(r"(\d+)\.zip$", name, re.IGNORECASE)
            num = int(num_m.group(1)) if num_m else 0
            candidates.append((1, num, updated, url))

    if not candidates:
        return None

    # Sort descending: highest match priority, highest number, latest update timestamp
    candidates.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    chosen = candidates[0][3]
    log.debug("Selected release asset: %s", chosen)
    return chosen


def _detect_zip_prefix(names: list[str]) -> str:
    """Detect and return the single root-folder prefix inside a zip, or ``""``
    if the zip is flat (files at the root level).

    GitHub auto-generated zips always have one top-level folder
    (e.g. ``A.U.R.A_v2-CodeCrafters-abc123/``).  Manually uploaded project
    zips may be flat (files directly at the root).  This function handles both
    cases so callers do not need to special-case them.

    Rules
    -----
    * If **every** entry starts with the same ``<folder>/`` prefix → return
      that prefix so it is stripped.
    * Otherwise (flat zip or mixed layout) → return ``""`` (strip nothing).
    """
    if not names:
        return ""

    norm_names = [n.replace("\\", "/") for n in names if n]
    if not norm_names:
        return ""

    # Collect the top-level component of each entry
    roots = {n.split("/")[0] for n in norm_names}

    # A single-root zip: all paths start with the same folder name
    if len(roots) == 1:
        root = next(iter(roots))
        # Make sure this is actually a directory prefix (not a lone root file)
        prefix = root + "/"
        # At least one entry must be deeper than just the folder itself
        has_children = any(
            n.startswith(prefix) and len(n) > len(prefix)
            for n in norm_names
        )
        if has_children:
            return prefix

    # Flat or mixed layout — no prefix to strip
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Local-version fingerprinting (fallback when version.json is absent/invalid)
# ─────────────────────────────────────────────────────────────────────────────

def _hash_local_files(
    base: Path,
    exclude_fn: Callable[[str], bool],
) -> dict[str, str]:
    """SHA-256 every non-excluded file under *base*.

    Returns ``{relative_posix_path: hex_digest}``.
    """
    result: dict[str, str] = {}
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(base).as_posix()
        if exclude_fn(rel):
            continue
        try:
            sha = hashlib.sha256(p.read_bytes()).hexdigest()
            result[rel] = sha
        except OSError as exc:
            log.debug("Cannot hash %s: %s", p, exc)
    return result


def _hash_zip_files(
    zip_path: Path,
    strip_prefix: str,
    exclude_fn: Callable[[str], bool],
) -> dict[str, str]:
    """SHA-256 every non-excluded file inside *zip_path*.

    GitHub zips wrap everything in a ``<repo>-<hash>/`` prefix; *strip_prefix*
    removes it so paths are relative to the project root.

    Returns ``{relative_posix_path: hex_digest}``.
    """
    result: dict[str, str] = {}
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                name = member.filename.replace("\\", "/")
                # strip the archive-root folder
                if strip_prefix and name.startswith(strip_prefix):
                    name = name[len(strip_prefix):]
                if not name:
                    continue
                if exclude_fn(name):
                    continue
                with zf.open(member) as f:
                    sha = hashlib.sha256(f.read()).hexdigest()
                result[name] = sha
    except zipfile.BadZipFile as exc:
        log.warning("Bad zip during fingerprinting: %s", exc)
    return result


def _download_to_temp(url: str, dest: Path, timeout: int = 60) -> None:
    """Download *url* to *dest* (streaming, with progress logs)."""
    tmp = dest.with_suffix(".part")
    try:
        with _open_url(url, timeout=timeout) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            got   = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        log.debug("Download %.1f%%", got * 100 / total)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    os.replace(tmp, dest)


def fingerprint_local_version(
    releases: list[dict],
    tmp_dir: Path,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> Optional[SemVer]:
    """Compare local source files against release archives to determine which
    release is installed.

    Downloads each release archive (starting from the lowest valid version),
    compares SHA-256 hashes, and returns the matching :class:`SemVer`.

    Returns ``None`` if no release matches (modified / custom build).
    Cleans up downloaded archives afterwards.
    """
    # Build sorted list of valid releases (ascending semver)
    valid: list[tuple[SemVer, dict]] = []
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        ver = SemVer.parse(rel.get("tag_name", ""))
        if ver and ver.is_at_least_minimum():
            valid.append((ver, rel))
    valid.sort(key=lambda t: t[0])

    if not valid:
        return None

    # Build local file hashes once (exclude protected + build artefacts)
    def _excl(p: str) -> bool:
        return is_protected(p) or any(
            fnmatch.fnmatch(p.lower(), pat)
            for pat in ["*.pyc", "*.pyo", "__pycache__/*", ".git/*"]
        )

    if progress_cb:
        progress_cb("Computing local file fingerprint…")

    local_hashes = _hash_local_files(BASE_DIR, _excl)

    for ver, rel in valid:
        tag  = rel.get("tag_name", str(ver))

        # Prefer the manually-uploaded source_code*.zip asset; fall back to
        # GitHub's auto-generated zipball, then to the archive tag URL.
        url = (
            get_release_asset_url(rel)
            or rel.get("zipball_url")
            or f"https://github.com/{REPO_OWNER}/{REPO_NAME}/archive/refs/tags/{tag}.zip"
        )
        log.debug("Fingerprint: downloading %s from %s", tag, url)

        dest = tmp_dir / f"release_{tag}.zip"
        if progress_cb:
            progress_cb(f"Checking release {tag}…")
        try:
            _download_to_temp(url, dest, timeout=60)
        except NetworkError as exc:
            log.warning("Fingerprint: cannot download %s: %s", tag, exc)
            continue

        # Detect archive root prefix — handles both flat zips (source_code.zip)
        # and GitHub-nested zips (A.U.R.A_v2-CodeCrafters-abc123/).
        prefix = ""
        try:
            with zipfile.ZipFile(dest) as zf:
                prefix = _detect_zip_prefix(zf.namelist())
        except zipfile.BadZipFile:
            dest.unlink(missing_ok=True)
            continue

        release_hashes = _hash_zip_files(dest, prefix, _excl)
        dest.unlink(missing_ok=True)

        # Compare: every file in the release must match local
        match = True
        for rel_path, rel_sha in release_hashes.items():
            local_sha = local_hashes.get(rel_path)
            if local_sha != rel_sha:
                match = False
                break

        if match:
            log.info("Fingerprint matched release %s", ver)
            return ver

    return None



# ─────────────────────────────────────────────────────────────────────────────
# Download / staging worker (QThread)
# ─────────────────────────────────────────────────────────────────────────────

class ReleaseDownloader(QObject):
    """Worker that downloads a release zip and places it in a staging directory.

    Runs inside a QThread; all communication via signals.
    """

    progress     = pyqtSignal(int, str)   # (percent 0-100, status message)
    finished     = pyqtSignal(str)        # staging_dir path on success
    error        = pyqtSignal(str)        # human-readable error message
    cancelled    = pyqtSignal()

    def __init__(self, release: dict, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._release    = release
        self._cancelled  = False
        self._staging: Optional[Path] = None

    def cancel(self) -> None:
        """Request cancellation (safe to call from any thread)."""
        self._cancelled = True

    def run(self) -> None:  # noqa: C901 — linear pipeline, acceptable length
        """Execute the download + extraction pipeline."""
        try:
            self._do_run()
        except Exception as exc:
            log.exception("ReleaseDownloader.run crashed")
            self.error.emit(str(exc))
            self._cleanup_staging()

    def _do_run(self) -> None:  # noqa: C901
        rel      = self._release
        tag      = rel.get("tag_name", "unknown")
        zip_url  = (
            get_release_asset_url(rel)
            or rel.get("zipball_url")
            or f"https://github.com/{REPO_OWNER}/{REPO_NAME}/archive/refs/tags/{tag}.zip"
        )
        log.info("Downloading release %s from %s", tag, zip_url)

        # ── 1. Free disk space check ──────────────────────────────────────────
        self.progress.emit(2, "Checking disk space…")
        try:
            usage = shutil.disk_usage(BASE_DIR)
            if usage.free < 200 * 1024 * 1024:  # 200 MB minimum
                self.error.emit(
                    "Not enough free disk space (need at least 200 MB). "
                    "Please free up space and try again."
                )
                return
        except OSError as exc:
            log.warning("disk_usage failed: %s", exc)

        if self._cancelled:
            self.cancelled.emit(); return

        # ── 2. Create temp download dir ───────────────────────────────────────
        self.progress.emit(5, f"Preparing download for {tag}…")
        tmp_dir = Path(tempfile.mkdtemp(prefix="aura_update_"))
        zip_path = tmp_dir / f"release_{tag}.zip"

        # ── 3. Download ───────────────────────────────────────────────────────
        self.progress.emit(8, "Connecting to GitHub…")
        try:
            req = _make_request(zip_url)
            with _open_url(zip_url) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                got   = 0
                with open(zip_path, "wb") as f:
                    while True:
                        if self._cancelled:
                            f.close()
                            shutil.rmtree(tmp_dir, ignore_errors=True)
                            self.cancelled.emit()
                            return
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            pct = int(got * 100 / total)
                            pct = max(8, min(70, int(8 + pct * 0.62)))
                            self.progress.emit(pct, f"Downloading… {got // 1024} / {total // 1024} KB")
        except NetworkError as exc:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            self.error.emit(str(exc))
            return

        if self._cancelled:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            self.cancelled.emit(); return

        # ── 4. Verify zip integrity ───────────────────────────────────────────
        self.progress.emit(72, "Verifying archive integrity…")
        if not zipfile.is_zipfile(zip_path):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            self.error.emit("Downloaded archive is corrupt or not a valid zip file.")
            return

        # ── 5. Determine archive root prefix ─────────────────────────────────
        with zipfile.ZipFile(zip_path) as zf:
            root_prefix = _detect_zip_prefix(zf.namelist())

        # ── 6. Extract to staging (zip-slip guard) ────────────────────────────
        self.progress.emit(75, "Extracting files…")
        staging_dir = BASE_DIR / STAGING_DIR_NAME
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True)
        self._staging = staging_dir

        try:
            with zipfile.ZipFile(zip_path) as zf:
                members = zf.infolist()
                for i, member in enumerate(members):
                    if self._cancelled:
                        shutil.rmtree(staging_dir, ignore_errors=True)
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                        self.cancelled.emit()
                        return

                    name = member.filename.replace("\\", "/")
                    # Strip archive root prefix
                    if root_prefix and name.startswith(root_prefix):
                        name = name[len(root_prefix):]
                    if not name:
                        continue

                    # ── Zip-slip / path traversal guard ──────────────────────
                    target = (staging_dir / name).resolve()
                    try:
                        target.relative_to(staging_dir.resolve())
                    except ValueError:
                        log.error("Zip-slip attempt blocked: %s", member.filename)
                        continue

                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue

                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)

                    pct = int(75 + (i / max(len(members), 1)) * 20)
                    if i % 20 == 0:
                        self.progress.emit(pct, f"Extracting… {i}/{len(members)}")

        except zipfile.BadZipFile as exc:
            shutil.rmtree(staging_dir, ignore_errors=True)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            self.error.emit(f"Archive extraction failed: {exc}")
            return

        # ── 7. Clean up temp zip ──────────────────────────────────────────────
        shutil.rmtree(tmp_dir, ignore_errors=True)

        self.progress.emit(97, "Staging complete — ready to apply.")
        self.finished.emit(str(staging_dir))

    def _cleanup_staging(self) -> None:
        if self._staging and self._staging.exists():
            try:
                shutil.rmtree(self._staging)
            except OSError:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Check-releases worker (QThread)
# ─────────────────────────────────────────────────────────────────────────────

class ReleaseChecker(QObject):
    """Fetches and parses the GitHub releases list off the UI thread."""

    finished = pyqtSignal(object, object, object)
    # (local_ver: SemVer|None, latest_ver: SemVer|None, latest_rel: dict|None)
    error    = pyqtSignal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)

    def run(self) -> None:
        """Execute the check on the worker thread."""
        try:
            releases    = fetch_releases()
            latest_rel  = get_latest_release(releases)
            latest_ver  = (
                SemVer.parse(latest_rel["tag_name"]) if latest_rel else None
            )

            local_ver = read_version_json()
            if local_ver is None:
                log.info("version.json absent/invalid — attempting fingerprint")
                # Use a temp dir for fingerprint archives
                tmp = Path(tempfile.mkdtemp(prefix="aura_fp_"))
                try:
                    local_ver = fingerprint_local_version(releases, tmp)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)

            self.finished.emit(local_ver, latest_ver, latest_rel)

        except NetworkError as exc:
            self.error.emit(str(exc))
        except Exception as exc:
            log.exception("ReleaseChecker.run crashed")
            self.error.emit(f"Unexpected error during update check: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Helper-script launcher
# ─────────────────────────────────────────────────────────────────────────────

def _write_helper_script() -> Path:
    """Write updater_helper.py next to this module and return its path.

    The helper is always re-written to ensure it is current.
    """
    dest = BASE_DIR / HELPER_SCRIPT_NAME
    # Read the helper source embedded in the same package directory.
    helper_src = Path(__file__).with_name("updater_helper.py")
    if helper_src.exists() and helper_src.resolve() != dest.resolve():
        shutil.copy2(helper_src, dest)
    return dest


def launch_helper(
    staging_dir: Path,
    new_version: str,
    entry_point: Path,
) -> None:
    """Launch the standalone apply-helper as a detached process, then quit.

    The helper:
    1. Waits for AURA to exit.
    2. Backs up overwritten files.
    3. Copies staged files (skipping protected paths).
    4. Writes version.json.
    5. Installs requirements if requirements.txt changed.
    6. Relaunches AURA.
    7. On any failure: rolls back and relaunches the old version.

    This function **does not return** — it calls ``QApplication.quit()``
    after spawning the detached helper.
    """
    from PyQt6.QtWidgets import QApplication

    helper = _write_helper_script()

    args = [
        sys.executable,
        str(helper),
        "--staging",  str(staging_dir),
        "--base",     str(BASE_DIR),
        "--version",  new_version,
        "--entry",    str(entry_point),
        "--pid",      str(os.getpid()),
    ]

    log.info("Launching update helper: %s", " ".join(args))

    kw: dict[str, Any] = {
        "close_fds": True,
    }
    if platform.system() == "Windows":
        # Detach completely; no console flash.
        kw["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kw["start_new_session"] = True

    subprocess.Popen(args, **kw)  # noqa: S603

    # Give the helper a moment to start before we exit.
    time.sleep(0.4)
    app = QApplication.instance()
    if app:
        app.quit()
