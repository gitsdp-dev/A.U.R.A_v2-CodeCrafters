"""Path validation and protected-location policy for confirmation-gated file tools."""

from __future__ import annotations

import os
import re
from pathlib import Path

MAX_BATCH_ITEMS = 500
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_DEVICE_PREFIX = "\\\\.\\"
_NT_DEVICE_PREFIX = "\\??\\"
_LONG_PATH_PREFIX = "\\\\?\\"
_GLOBAL_ROOT_PREFIX = "\\\\?\\GLOBALROOT"
_BROWSER_PATH_PARTS = {
    "google\\chrome\\user data",
    "microsoft\\edge\\user data",
    "mozilla\\firefox\\profiles",
    "opera software\\opera stable",
    "bravesoftware\\brave-browser\\user data",
}


class PathGuardError(ValueError):
    """Raised when a path is malformed, unsupported, or outside policy."""


def normalize_path(raw: str | os.PathLike[str]) -> Path:
    """Expand, resolve and validate one local filesystem path."""
    if not isinstance(raw, (str, os.PathLike)):
        raise PathGuardError("A filesystem path must be text.")
    value = os.fspath(raw)
    if not isinstance(value, str) or not value.strip():
        raise PathGuardError("A filesystem path cannot be empty.")
    value = value.strip()
    if "\x00" in value:
        raise PathGuardError("Paths containing null characters are not allowed.")
    value = os.path.expandvars(os.path.expanduser(value))
    if not value.strip() or "\x00" in value:
        raise PathGuardError("The expanded path is empty or invalid.")
    if (
        value.startswith((_DEVICE_PREFIX, _NT_DEVICE_PREFIX))
        or value.upper().startswith(_GLOBAL_ROOT_PREFIX)
    ):
        raise PathGuardError("Windows device and global-root paths are not supported.")
    if os.name == "nt" and value.startswith(_LONG_PATH_PREFIX):
        value = value.removeprefix(_LONG_PATH_PREFIX)
    elif value.startswith(("\\\\", "//")):
        raise PathGuardError("Network and UNC paths are not supported.")

    drive, tail = os.path.splitdrive(value)
    if drive and not Path(value).is_absolute():
        raise PathGuardError("Drive-relative paths are not supported.")
    if ":" in tail:
        raise PathGuardError("Alternate data streams are not supported.")
    for component in re.split(r"[\\/]", value):
        if not component:
            continue
        cleaned = component.rstrip(" .")
        base_name = cleaned.split(".", 1)[0].upper()
        if base_name in _RESERVED_NAMES:
            raise PathGuardError(f"Reserved Windows device name is not allowed: {component}")

    try:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathGuardError(f"Could not safely resolve that path: {exc}") from exc

    if os.name == "nt":
        text = str(resolved)
        text = text.removeprefix("\\\\?\\")
        resolved = Path(os.path.normcase(text))
    return resolved


def _protected_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    home = Path.home()
    app_root = Path(__file__).resolve().parents[1]
    system_drive = os.environ.get("SystemDrive", "C:\\")
    if not system_drive.endswith("\\"):
        system_drive += "\\"
    roots.extend((
        Path(os.environ.get("WINDIR", r"C:\Windows")),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        Path(os.environ.get("ProgramData", r"C:\ProgramData")),
        Path(system_drive) / "Boot",
        Path(system_drive) / "Recovery",
        Path(system_drive) / "EFI",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Boot",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "config",
        home / ".ssh",
        app_root,
        app_root / "config",
        app_root / "memory",
        Path(system_drive) / "$Recycle.Bin",
        Path(system_drive) / "System Volume Information",
    ))
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    roots.extend((
        local / "Google" / "Chrome" / "User Data",
        local / "Microsoft" / "Edge" / "User Data",
        local / "BraveSoftware" / "Brave-Browser" / "User Data",
        local / "Microsoft" / "Credentials",
        local / "Microsoft" / "Vault",
        roaming / "Mozilla" / "Firefox" / "Profiles",
        roaming / "Opera Software" / "Opera Stable",
        roaming / "Microsoft" / "Credentials",
        roaming / "Microsoft" / "Vault",
    ))
    return tuple(roots)


def is_drive_root(path: Path) -> bool:
    """Return whether path names a drive root rather than a child."""
    if os.name == "nt":
        drive, tail = os.path.splitdrive(str(path))
        return bool(drive) and tail in ("\\", "/")
    return path == Path(path.anchor)


def is_protected_path(path: str | os.PathLike[str]) -> bool:
    return _is_protected_resolved(normalize_path(path))


def _is_protected_resolved(target: Path) -> bool:
    target_text = os.path.normcase(str(target))
    for root in _protected_roots():
        try:
            root_text = os.path.normcase(os.path.realpath(str(root)))
            if target_text == root_text or target_text.startswith(root_text.rstrip("\\/") + os.sep):
                return True
        except (OSError, RuntimeError):
            continue

    if os.name == "nt":
        lowered = target_text.replace("/", "\\")
        if any(part in lowered for part in _BROWSER_PATH_PARTS):
            return True
        if target.name.casefold() in {"pagefile.sys", "hiberfil.sys", "swapfile.sys"}:
            return True
        if any(
            part.casefold() in {"$recycle.bin", "system volume information"}
            for part in target.parts
        ):
            return True
    return False


def validate_operation_path(
    path: str | os.PathLike[str], operation: str, *, allow_protected_read: bool = True
) -> tuple[Path, bool]:
    """Resolve a path and return (path, protected); refuse protected mutations."""
    target = normalize_path(path)
    protected = _is_protected_resolved(target) or (
        operation == "read" and is_drive_root(target)
    )
    modifying = operation in {"create", "write", "copy", "move", "rename", "delete"}
    if modifying:
        raw_value = os.path.expandvars(os.path.expanduser(os.fspath(path).strip()))
        candidate = Path(raw_value)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        try:
            attributes = getattr(os.lstat(candidate), "st_file_attributes", 0)
        except FileNotFoundError:
            attributes = 0
        except OSError as exc:
            raise PathGuardError(f"Could not safely inspect that path: {exc}") from exc
        if candidate.is_symlink() or attributes & 0x400:
            raise PathGuardError(
                "Mutations through symbolic links and reparse points are not supported."
            )
    if modifying and (protected or is_drive_root(target)):
        raise PathGuardError("Changes to protected locations and drive roots are blocked.")
    if protected and operation == "read" and not allow_protected_read:
        raise PathGuardError("Reading this protected location is not allowed.")
    return target, protected


def enforce_batch_limit(item_count: int, maximum: int = MAX_BATCH_ITEMS) -> None:
    if item_count > maximum:
        raise PathGuardError(
            f"This request includes {item_count} items; the limit is {maximum}. Please narrow it."
        )
    if item_count < 0:
        raise PathGuardError("Item counts cannot be negative.")
