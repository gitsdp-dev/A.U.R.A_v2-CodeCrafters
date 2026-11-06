"""Bounded file operations that cannot run without the confirmation broker."""

from __future__ import annotations

import errno
import fnmatch
import json
import os
import shutil
import stat
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from core.confirmation import (
    ConfirmationDecision,
    ConfirmationPlan,
    RiskLevel,
    get_confirmation_broker,
)
from core.path_guard import (
    MAX_BATCH_ITEMS,
    PathGuardError,
    enforce_batch_limit,
    validate_operation_path,
)

MAX_READ_BYTES = 2_000_000
MAX_SEARCH_DEPTH = 5
MAX_SEARCH_SCANNED = 5_000
MAX_SEARCH_RESULTS = 100
MAX_LIST_SCANNED = 5_000


class FileAccessError(ValueError):
    """A user-understandable file action error."""


class UserDeniedError(FileAccessError):
    """A user explicitly declined an operation."""


_audit_context = threading.local()


def classify_file_risk(operation: str, protected: bool = False) -> RiskLevel:
    if protected:
        return RiskLevel.CRITICAL
    if operation in {"delete", "move", "rename"}:
        return RiskLevel.HIGH
    if operation in {"list_folder", "read_file", "search_files", "get_file_info"}:
        return RiskLevel.LOW
    return RiskLevel.MEDIUM


def _result(success: bool, what: str, errors: list[str] | None = None, **extra) -> str:
    payload = {
        "success": success,
        "what": what,
        "errors": errors or [],
        "summary": what if success else (errors or ["The request could not be completed."])[0],
        **extra,
    }
    return json.dumps(payload, ensure_ascii=False)


def _error_message(exc: Exception) -> str:
    if isinstance(exc, PermissionError):
        return "Windows or the operating system denied access to that location."
    if isinstance(exc, FileNotFoundError):
        return "The requested file or folder could not be found."
    if isinstance(exc, FileExistsError):
        return "A file or folder with that name already exists."
    if isinstance(exc, OSError) and getattr(exc, "winerror", None) == 206:
        return "That path is too long for this operation."
    if isinstance(exc, PathGuardError):
        return str(exc)
    if isinstance(exc, (OSError, UnicodeError)):
        return f"The file operation failed: {exc}"
    return str(exc)


def _broker_decision(plan: ConfirmationPlan) -> ConfirmationDecision:
    broker = get_confirmation_broker()
    if broker is None:
        return ConfirmationDecision.DENY
    try:
        return broker.request_confirmation(plan)
    except RuntimeError:
        raise
    except OSError as exc:
        raise FileAccessError(f"Could not record the permission request: {exc}") from exc


def _confirm(plan: ConfirmationPlan) -> bool:
    decision = _broker_decision(plan)
    _audit_context.plan = plan
    _audit_context.decision = decision
    return decision in {
        ConfirmationDecision.APPROVE,
        ConfirmationDecision.SESSION_READ,
    }


def _approved_snapshot(
    make_plan: Callable[[object], ConfirmationPlan],
    snapshot: Callable[[], object],
) -> object:
    """Approve one exact inventory; changed files require a fresh approval."""
    for _attempt in range(2):
        before = snapshot()
        plan = make_plan(before)
        if not _confirm(plan):
            raise UserDeniedError("User denied this request.")
        after = snapshot()
        if before == after:
            return before
    raise FileAccessError(
        "The files changed while permission was being requested. Nothing was done; please review again."
    )


def _inventory(path: Path) -> tuple[tuple[str, int, int, bool], ...]:
    records: list[tuple[str, int, int, bool]] = []
    stack = [path]
    while stack:
        current = stack.pop()
        if _is_link_or_reparse(current):
            raise FileAccessError("Symbolic links are not included in batch operations.")
        file_stat = current.stat(follow_symlinks=False)
        is_directory = current.is_dir()
        if not is_directory and not stat.S_ISREG(file_stat.st_mode):
            raise FileAccessError("Special files are not included in batch operations.")
        records.append((str(current), file_stat.st_size, file_stat.st_mtime_ns, is_directory))
        enforce_batch_limit(len(records))
        if current.is_dir():
            with os.scandir(current) as entries:
                for entry in entries:
                    if _is_link_or_reparse(Path(entry.path)):
                        raise FileAccessError("Symbolic links are not included in batch operations.")
                    stack.append(Path(entry.path))
                    if len(records) + len(stack) > MAX_BATCH_ITEMS:
                        enforce_batch_limit(len(records) + len(stack))
    return tuple(sorted(records))


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _size(inventory: tuple[tuple[str, int, int, bool], ...]) -> int:
    return sum(size for _, size, _, is_dir in inventory if not is_dir)


def _operations_for_inventory(label: str, paths: list[Path], inventories) -> tuple[str, ...]:
    operations: list[str] = []
    for path, inventory in zip(paths, inventories):
        if path.is_dir():
            operations.append(f"{label} folder {path} ({len(inventory)} items)")
        else:
            operations.append(f"{label} file {path} ({_size(inventory)} bytes)")
    return tuple(operations)


def _require_paths(parameters: dict, key: str = "path") -> str:
    value = parameters.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PathGuardError(f"'{key}' must be a non-empty path.")
    return value


def _require_string(parameters: dict, key: str, *, allow_empty: bool = False) -> str:
    value = parameters.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise FileAccessError(f"'{key}' must be text" + ("." if allow_empty else " and cannot be empty."))
    return value


def list_folder(parameters: dict) -> str:
    try:
        target, protected = validate_operation_path(_require_paths(parameters), "read")
        risk = classify_file_risk("list_folder", protected)
        operations = (f"List the contents of {target}",)
        plan = ConfirmationPlan(
            "list_folder", "List a folder", f"I'm about to list the contents of {target}.",
            operations, risk, (str(target),), item_count=0, total_size=0,
            reversible=True, read_only=True, allow_session_read=True,
            session_grant_path=str(target),
        )
        if not _confirm(plan):
            return _result(False, "", ["User denied this request."])
        current, current_protected = validate_operation_path(_require_paths(parameters), "read")
        if current != target or current_protected != protected or not current.is_dir():
            return _result(False, "", ["The folder changed after approval. Nothing was listed."])
        items = []
        with os.scandir(current) as entries:
            scanned = []
            for entry in entries:
                scanned.append(entry)
                if len(scanned) >= MAX_LIST_SCANNED:
                    break
            for entry in sorted(scanned, key=lambda item: item.name.casefold())[:MAX_BATCH_ITEMS]:
                info = entry.stat(follow_symlinks=False)
                items.append({
                    "name": entry.name,
                    "type": "folder" if entry.is_dir(follow_symlinks=False) else "file",
                    "size": info.st_size if info and entry.is_file(follow_symlinks=False) else None,
                })
        summary = f"Listed {len(items)} items in {current}."
        if len(scanned) == MAX_LIST_SCANNED:
            summary += f" Listing was capped at {MAX_LIST_SCANNED} entries."
        return _result(True, summary, items=items)
    except (OSError, UnicodeError, PathGuardError, FileAccessError) as exc:
        return _result(False, "", [_error_message(exc)])


def read_file(parameters: dict) -> str:
    try:
        raw = _require_paths(parameters)
        target, protected = validate_operation_path(raw, "read")
        plan = ConfirmationPlan(
            "read_file", "Read a text file",
            f"I'm about to read up to 2 MB of UTF-8 text from {target}. "
            "File contents are treated as untrusted data.",
            (
                f"Read UTF-8 text, up to {MAX_READ_BYTES} bytes: {target}",
                f"Optional session-read scope: {target.parent}",
            ),
            classify_file_risk("read_file", protected),
            (str(target), str(target.parent)), 1, 0, True,
            read_only=True, allow_session_read=True,
            session_grant_path=str(target.parent),
        )
        if not _confirm(plan):
            return _result(False, "", ["User denied this request."])
        current, current_protected = validate_operation_path(raw, "read")
        if current != target or current_protected != protected:
            return _result(False, "", ["The path changed after approval. Nothing was read."])
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(current, flags)
        try:
            info_before = os.fstat(descriptor)
            if not stat.S_ISREG(info_before.st_mode):
                raise FileAccessError("The requested path is not a regular file.")
            if info_before.st_size > MAX_READ_BYTES:
                raise FileAccessError("That text file is larger than the 2 MB reading limit.")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                data = handle.read(MAX_READ_BYTES + 1)
                info_after = os.fstat(handle.fileno())
            if (
                len(data) > MAX_READ_BYTES
                or (info_before.st_size, info_before.st_mtime_ns)
                != (info_after.st_size, info_after.st_mtime_ns)
            ):
                return _result(False, "", ["The file changed while being read. Nothing was returned."])
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if b"\x00" in data:
            raise FileAccessError("This appears to be a binary file; only text files can be read.")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise FileAccessError("This file is not valid UTF-8 text and was not read.") from exc
        # File contents are untrusted data: returning text never dispatches an action.
        return _result(True, f"Read {len(data)} bytes from {target}.", content=text)
    except (OSError, UnicodeError, PathGuardError, FileAccessError) as exc:
        return _result(False, "", [_error_message(exc)])


def search_files(parameters: dict) -> str:
    try:
        raw = _require_paths(parameters)
        target, protected = validate_operation_path(raw, "read")
        pattern = _require_string(parameters, "pattern")
        if len(pattern) > 256:
            raise FileAccessError("Search patterns are limited to 256 characters.")
        plan = ConfirmationPlan(
            "search_files", "Search files",
            f"I'm about to search up to {MAX_SEARCH_DEPTH} folder levels under {target}.",
            (f"Search for names matching {pattern!r} (maximum {MAX_SEARCH_RESULTS} results)",),
            classify_file_risk("search_files", protected),
            (str(target),), 0, 0, True, read_only=True, allow_session_read=True,
            session_grant_path=str(target),
        )
        if not _confirm(plan):
            return _result(False, "", ["User denied this request."])
        current, current_protected = validate_operation_path(raw, "read")
        if current != target or current_protected != protected or not current.is_dir():
            return _result(False, "", ["The folder changed after approval. Nothing was searched."])
        found: list[str] = []
        scanned = 0
        stack: list[tuple[Path, int]] = [(current, 0)]
        while stack and len(found) < MAX_SEARCH_RESULTS and scanned < MAX_SEARCH_SCANNED:
            folder, depth = stack.pop()
            with os.scandir(folder) as entries:
                for entry in entries:
                    scanned += 1
                    if fnmatch.fnmatchcase(entry.name.casefold(), pattern.casefold()):
                        found.append(entry.path)
                        if len(found) >= MAX_SEARCH_RESULTS:
                            break
                    if (
                        depth < MAX_SEARCH_DEPTH
                        and entry.is_dir(follow_symlinks=False)
                        and not entry.is_symlink()
                    ):
                        stack.append((Path(entry.path), depth + 1))
                    if scanned >= MAX_SEARCH_SCANNED:
                        break
        return _result(True, f"Found {len(found)} matching items.", items=found, scanned=scanned)
    except (OSError, UnicodeError, PathGuardError, FileAccessError) as exc:
        return _result(False, "", [_error_message(exc)])


def get_file_info(parameters: dict) -> str:
    try:
        raw = _require_paths(parameters)
        target, protected = validate_operation_path(raw, "read")
        plan = ConfirmationPlan(
            "get_file_info", "View file information",
            f"I'm about to view basic information about {target}.",
            (f"Read file or folder metadata: {target}",),
            classify_file_risk("get_file_info", protected),
            (str(target),), 1, 0,
            True, read_only=True, allow_session_read=False,
        )
        if not _confirm(plan):
            return _result(False, "", ["User denied this request."])
        current, current_protected = validate_operation_path(raw, "read")
        if current != target or current_protected != protected:
            return _result(False, "", ["The path changed after approval. No metadata was read."])
        info = current.lstat()
        kind = "folder" if stat.S_ISDIR(info.st_mode) else "file"
        return _result(True, f"Retrieved information for {target}.", info={
            "path": str(current), "type": kind, "size": info.st_size,
            "modified": info.st_mtime, "created": info.st_ctime,
        })
    except (OSError, UnicodeError, PathGuardError, FileAccessError) as exc:
        return _result(False, "", [_error_message(exc)])


def _target_for_change(raw: str, operation: str) -> tuple[Path, bool]:
    return validate_operation_path(raw, operation)


def create_folder(parameters: dict) -> str:
    try:
        target, _ = _target_for_change(_require_paths(parameters), "create")
        plan = ConfirmationPlan(
            "create_folder", "Create a folder",
            f"I'm about to create the folder {target}.",
            (f"Create folder {target}",), RiskLevel.MEDIUM,
            (str(target),), 1, 0, True,
        )
        if not _confirm(plan):
            return _result(False, "", ["User denied this request."])
        current, _ = _target_for_change(_require_paths(parameters), "create")
        if current != target or current.exists() or not current.parent.is_dir():
            return _result(False, "", ["The destination changed after approval. Nothing was created."])
        current.mkdir()
        return _result(True, f"Created folder {current}.")
    except (OSError, UnicodeError, PathGuardError, FileAccessError) as exc:
        return _result(False, "", [_error_message(exc)])


def _write_text(parameters: dict, create_only: bool) -> str:
    try:
        raw = _require_paths(parameters)
        target, _ = _target_for_change(raw, "write")
        content = _require_string(parameters, "content", allow_empty=True)
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_READ_BYTES:
            raise FileAccessError("Text writes are limited to 2 MB.")
        if not target.parent.is_dir():
            raise FileNotFoundError(str(target.parent))
        exists = target.exists()
        if create_only and exists:
            raise FileExistsError(str(target))
        previous_stat = target.stat() if exists and target.is_file() else None
        previous_size = previous_stat.st_size if previous_stat else 0
        if exists and not target.is_file():
            raise FileAccessError("A folder cannot be replaced with a text file.")
        label = "Replace existing file" if exists else "Create file"
        op = f"{label} {target} ({len(encoded)} bytes; size change {len(encoded) - previous_size:+d} bytes)"
        plan = ConfirmationPlan(
            "create_file" if create_only else "write_file",
            "Create a file" if not exists else "Replace a file",
            f"I'm about to {'replace' if exists else 'create'} the text file {target}.",
            (op,), RiskLevel.MEDIUM, (str(target),), 1, len(encoded), True,
        )
        if not _confirm(plan):
            return _result(False, "", ["User denied this request."])
        current, _ = _target_for_change(raw, "write")
        current_exists = current.exists()
        current_stat = current.stat() if current_exists and current.is_file() else None
        current_size = current_stat.st_size if current_stat else 0
        original_signature = (
            (previous_stat.st_size, previous_stat.st_mtime_ns) if previous_stat else None
        )
        current_signature = (
            (current_stat.st_size, current_stat.st_mtime_ns) if current_stat else None
        )
        if (
            current != target
            or current_exists != exists
            or current_size != previous_size
            or current_signature != original_signature
        ):
            return _result(False, "", ["The destination changed after approval. Nothing was written."])
        if create_only:
            with current.open("x", encoding="utf-8", newline="") as handle:
                handle.write(content)
        else:
            fd, temp_name = tempfile.mkstemp(prefix=".aura-write-", dir=str(current.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                if exists:
                    os.replace(temp_name, current)
                else:
                    os.link(temp_name, current)
                    os.unlink(temp_name)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        return _result(True, f"Wrote {len(encoded)} bytes to {current}.")
    except (OSError, UnicodeError, PathGuardError, FileAccessError) as exc:
        return _result(False, "", [_error_message(exc)])


def create_file(parameters: dict) -> str:
    return _write_text(parameters, create_only=True)


def write_file(parameters: dict) -> str:
    return _write_text(parameters, create_only=False)


def _copy_file_exclusive(source: Path, target: Path, expected_inventory=None):
    fd, temporary = tempfile.mkstemp(prefix=".aura-copy-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        shutil.copystat(source, temporary, follow_symlinks=False)
        if expected_inventory is not None and (
            _inventory_signature(_inventory(source), source)
            != _inventory_signature(expected_inventory, source)
        ):
            raise FileAccessError("The source changed while it was being copied.")
        os.link(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _inventory_signature(inventory, root: Path, *, include_mtime: bool = True):
    return tuple(
        sorted(
            (
                str(Path(path).relative_to(root)),
                size,
                modified if include_mtime else None,
                is_directory,
            )
            for path, size, modified, is_directory in inventory
        )
    )


def _copy_directory_exclusive(source: Path, target: Path, expected_inventory):
    container = Path(tempfile.mkdtemp(prefix=".aura-copy-", dir=str(target.parent)))
    staging = container / "contents"
    try:
        shutil.copytree(source, staging, symlinks=True)
        if (
            _inventory_signature(_inventory(source), source)
            != _inventory_signature(expected_inventory, source)
        ):
            raise FileAccessError("The source changed while it was being copied.")
        if (
            _inventory_signature(_inventory(staging), staging, include_mtime=False)
            != _inventory_signature(expected_inventory, source, include_mtime=False)
        ):
            raise FileAccessError("The copied folder did not match the approved item set.")
        os.rename(staging, target)
    finally:
        if container.exists():
            shutil.rmtree(container)


def _move_item(source: Path, target: Path, expected_inventory):
    if source.is_file():
        try:
            os.link(source, target, follow_symlinks=False)
        except OSError as exc:
            if getattr(exc, "winerror", None) in {80, 183} or exc.errno == errno.EEXIST:
                raise
            _copy_file_exclusive(source, target, expected_inventory)
        try:
            source.unlink()
        except OSError as removal_error:
            raise FileAccessError(
                f"The file was copied to {target}, but the original could not be removed."
            ) from removal_error
        return
    try:
        os.rename(source, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV and getattr(exc, "winerror", None) != 17:
            raise
        _copy_directory_exclusive(source, target, expected_inventory)
        try:
            shutil.rmtree(source)
        except OSError as removal_error:
            raise FileAccessError(
                f"The folder was copied to {target}, but the original could not be removed."
            ) from removal_error


def _batch_sources(parameters: dict, operation: str) -> tuple[list[Path], list[tuple]]:
    raw_sources = parameters.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise FileAccessError("'sources' must be a non-empty list of paths.")
    if len(raw_sources) > MAX_BATCH_ITEMS:
        enforce_batch_limit(len(raw_sources))
    sources: list[Path] = []
    inventories: list[tuple] = []
    for raw in raw_sources:
        source, _ = _target_for_change(raw, operation)
        if not source.exists():
            raise FileNotFoundError(str(source))
        sources.append(source)
        inventories.append(_inventory(source))
    for index, source in enumerate(sources):
        for other in sources[index + 1:]:
            if source == other or source in other.parents or other in source.parents:
                raise FileAccessError("Overlapping source paths are not allowed in one batch.")
    enforce_batch_limit(sum(len(items) for items in inventories))
    return sources, inventories


def _copy_move(parameters: dict, move: bool) -> str:
    operation = "move" if move else "copy"
    try:
        initial_sources, _ = _batch_sources(parameters, operation)
        initial_destination, _ = _target_for_change(
            _require_paths(parameters, "destination"), operation
        )
        if not initial_destination.is_dir():
            raise NotADirectoryError(str(initial_destination))
        if any(
            initial_destination == source or source in initial_destination.parents
            for source in initial_sources
        ):
            raise FileAccessError("A folder cannot be copied or moved into itself.")

        def make_plan(current):
            current_sources, current_inventories, current_destination = current
            count = sum(len(items) for items in current_inventories)
            return ConfirmationPlan(
                operation, "Move files" if move else "Copy files",
                f"I'm about to {operation} {len(current_sources)} item(s) into {current_destination}.",
                _operations_for_inventory("Move" if move else "Copy", current_sources, current_inventories)
                + (f"Destination: {current_destination}",),
                classify_file_risk(operation),
                tuple(str(path) for path in current_sources + [current_destination]),
                count, sum(_size(items) for items in current_inventories), move,
            )

        def snapshot():
            current_sources, current_inventories = _batch_sources(parameters, operation)
            current_destination, _ = _target_for_change(
                _require_paths(parameters, "destination"), operation
            )
            if not current_destination.is_dir() or any(
                current_destination == source or source in current_destination.parents
                for source in current_sources
            ):
                raise FileAccessError("The destination is no longer a safe folder.")
            return current_sources, current_inventories, current_destination

        approved = _approved_snapshot(make_plan, snapshot)
        current_sources, approved_inventories, destination = approved
        targets = [destination / source.name for source in current_sources]
        if len({os.path.normcase(str(target)) for target in targets}) != len(targets):
            return _result(False, "", ["Two sources have the same destination name. Nothing was copied or moved."])
        for source, inventory in zip(current_sources, approved_inventories):
            current, _ = _target_for_change(str(source), operation)
            if _inventory(current) != inventory:
                return _result(False, "", ["A source changed after approval. Nothing was copied or moved."])
        current_destination, _ = _target_for_change(
            _require_paths(parameters, "destination"), operation
        )
        if current_destination != destination or not current_destination.is_dir():
            return _result(False, "", ["The destination changed after approval. Nothing was copied or moved."])
        if any(target.exists() for target in targets):
            return _result(False, "", ["A destination appeared after approval. Nothing was copied or moved."])
        completed: list[str] = []
        for index, (source, target) in enumerate(zip(current_sources, targets)):
            current, _ = _target_for_change(str(source), operation)
            if (
                current != source
                or _inventory(current) != approved_inventories[index]
                or target.exists()
            ):
                return _result(
                    False,
                    f"Completed {len(completed)} item(s) before a later source changed.",
                    ["The batch changed after approval; remaining items were not processed."],
                    completed=completed,
                )
            if move:
                _move_item(current, target, approved_inventories[index])
            elif current.is_dir():
                _copy_directory_exclusive(
                    current, target, approved_inventories[index]
                )
            else:
                _copy_file_exclusive(current, target, approved_inventories[index])
            completed.append(str(target))
        return _result(True, f"{'Moved' if move else 'Copied'} {len(completed)} item(s) to {destination}.",
                        completed=completed)
    except (OSError, UnicodeError, PathGuardError, FileAccessError) as exc:
        return _result(False, "", [_error_message(exc)])


def copy_items(parameters: dict) -> str:
    return _copy_move(parameters, move=False)


def move_items(parameters: dict) -> str:
    return _copy_move(parameters, move=True)


def rename_item(parameters: dict) -> str:
    try:
        raw = _require_paths(parameters)
        source, _ = _target_for_change(raw, "rename")
        new_name = _require_string(parameters, "new_name")
        if new_name in {".", ".."} or Path(new_name).name != new_name or "/" in new_name or "\\" in new_name:
            raise PathGuardError("A new name must be a single filename, not a path.")
        target, _ = _target_for_change(str(source.parent / new_name), "rename")
        if not source.exists():
            raise FileNotFoundError(str(source))
        if target.exists():
            raise FileExistsError(str(target))
        snapshot = _inventory(source)
        plan = ConfirmationPlan(
            "rename", "Rename a file or folder",
            f"I'm about to rename {source.name} to {target.name}.",
            (f"Rename {source} to {target}",),
            classify_file_risk("rename"),
            (str(source), str(target)), len(snapshot), _size(snapshot), True,
        )
        if not _confirm(plan):
            return _result(False, "", ["User denied this request."])
        current, _ = _target_for_change(raw, "rename")
        if current != source or _inventory(current) != snapshot or target.exists():
            return _result(False, "", ["The file changed after approval. Nothing was renamed."])
        current.rename(target)
        return _result(True, f"Renamed {source.name} to {target.name}.")
    except (OSError, UnicodeError, PathGuardError, FileAccessError) as exc:
        return _result(False, "", [_error_message(exc)])


def delete_items(parameters: dict) -> str:
    try:
        raw_sources = parameters.get("paths")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise FileAccessError("'paths' must be a non-empty list of paths.")
        if len(raw_sources) > MAX_BATCH_ITEMS:
            enforce_batch_limit(len(raw_sources))
        sources: list[Path] = []
        for raw in raw_sources:
            target, _ = _target_for_change(raw, "delete")
            if not target.exists():
                raise FileNotFoundError(str(target))
            sources.append(target)
        for index, source in enumerate(sources):
            for other in sources[index + 1:]:
                if source == other or source in other.parents or other in source.parents:
                    raise FileAccessError("Overlapping paths are not allowed in one delete request.")

        def make_plan(current):
            current_sources, current_snapshots = current
            return ConfirmationPlan(
                "delete", "Move items to the Recycle Bin",
                f"I'm about to move {len(current_sources)} item(s) to the Recycle Bin.",
                _operations_for_inventory("Move to Recycle Bin", current_sources, current_snapshots),
                RiskLevel.HIGH, tuple(str(path) for path in current_sources),
                sum(len(items) for items in current_snapshots),
                sum(_size(items) for items in current_snapshots), True,
            )

        def snapshot():
            current_sources = [
                _target_for_change(raw, "delete")[0] for raw in raw_sources
            ]
            return current_sources, tuple(_inventory(path) for path in current_sources)

        approved = _approved_snapshot(make_plan, snapshot)
        current_sources, current_snapshots = approved
        try:
            from send2trash import send2trash
        except ImportError as exc:
            raise FileAccessError("The send2trash package is unavailable; nothing was deleted.") from exc
        completed: list[str] = []
        for path, expected in zip(current_sources, current_snapshots):
            current, _ = _target_for_change(str(path), "delete")
            if current != path or _inventory(current) != expected:
                return _result(
                    False,
                    f"Moved {len(completed)} item(s) before a later item changed.",
                    ["The batch changed after approval; remaining items were not deleted."],
                    completed=completed,
                )
            try:
                send2trash(str(current))
                completed.append(str(current))
            except OSError as exc:
                return _result(
                    False,
                    f"Moved {len(completed)} item(s) to the Recycle Bin; the remaining items were not moved.",
                    [_error_message(exc)],
                    completed=completed,
                )
        return _result(True, f"Moved {len(completed)} item(s) to the Recycle Bin.", completed=completed)
    except (OSError, UnicodeError, PathGuardError, FileAccessError) as exc:
        return _result(False, "", [_error_message(exc)])


def execute(operation: str, parameters: dict) -> str:
    _audit_context.plan = None
    _audit_context.decision = ConfirmationDecision.DENY
    handlers = {
        "list_folder": list_folder,
        "read_file": read_file,
        "search_files": search_files,
        "get_file_info": get_file_info,
        "create_folder": create_folder,
        "create_file": create_file,
        "write_file": write_file,
        "copy": copy_items,
        "move": move_items,
        "rename": rename_item,
        "delete": delete_items,
    }
    handler = handlers.get(operation)
    if handler is None:
        return _result(False, "", ["That file operation is not available."])
    result = handler(parameters)
    plan = getattr(_audit_context, "plan", None)
    broker = get_confirmation_broker()
    if plan is not None and broker is not None:
        try:
            broker.record_operation_result(
                plan, _audit_context.decision,
                json.loads(result).get("summary", "Completed"),
            )
        except OSError as exc:
            payload = json.loads(result)
            payload.setdefault("errors", []).append(
                f"The operation result could not be written to the audit log: {exc}"
            )
            if payload.get("success"):
                payload["summary"] = (
                    "The operation completed, but its final audit record could not be saved."
                )
            else:
                payload["summary"] = (
                    f"{payload.get('summary', 'The request failed')} "
                    "The final audit record could not be saved."
                )
            return json.dumps(payload, ensure_ascii=False)
    return result
