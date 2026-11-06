"""Strict, data-driven allowlist for supported Windows preference settings."""

from __future__ import annotations

import ctypes
import json
import os
import platform
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core.confirmation import (
    ConfirmationDecision,
    ConfirmationPlan,
    RiskLevel,
    get_confirmation_broker,
)


@dataclass(frozen=True)
class SettingSpec:
    setting_id: str
    display_name: str
    description: str
    registry_key: str
    value_name: str
    allowed_values: tuple[object, ...]
    requires_admin: bool
    risk_level: RiskLevel
    reversible: bool
    restart_required: bool
    getter: Callable[[], object]
    setter: Callable[[object], None]
    read_only: bool = False


def _registry_read(root_name: str, subkey: str, value_name: str):
    import winreg

    root = getattr(winreg, root_name)
    with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ) as key:
        value, _kind = winreg.QueryValueEx(key, value_name)
    return value


def _registry_write(subkey: str, value_name: str, value: int):
    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, value_name, 0, winreg.REG_DWORD, value)
    _notify_setting_change()


def _notify_setting_change():
    if platform.system() != "Windows":
        return
    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(
        ctypes.c_void_p(0xFFFF),
        0x001A,
        0,
        "Setting",
        0x0002,
        2000,
        ctypes.byref(result),
    )


_THEME_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_EXPLORER_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
_UAC_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"


def _theme_get():
    return "light" if _registry_read("HKEY_CURRENT_USER", _THEME_KEY, "AppsUseLightTheme") else "dark"


def _theme_set(value):
    _registry_write(_THEME_KEY, "AppsUseLightTheme", 1 if value == "light" else 0)


def _bool_get(value_name: str):
    return bool(_registry_read("HKEY_CURRENT_USER", _EXPLORER_KEY, value_name))


def _bool_set(value_name: str, inverted: bool = False):
    def setter(value):
        on_disk = not value if inverted else value
        _registry_write(_EXPLORER_KEY, value_name, 1 if on_disk else 0)
    return setter


def _show_hidden_get():
    return _registry_read("HKEY_CURRENT_USER", _EXPLORER_KEY, "Hidden") == 1


def _show_hidden_set(value):
    _registry_write(_EXPLORER_KEY, "Hidden", 1 if value else 2)


def _uac_get():
    admin_prompt = _registry_read(
        "HKEY_LOCAL_MACHINE", _UAC_KEY, "ConsentPromptBehaviorAdmin"
    )
    user_prompt = _registry_read(
        "HKEY_LOCAL_MACHINE", _UAC_KEY, "ConsentPromptBehaviorUser"
    )
    return {
        "enabled": bool(_registry_read("HKEY_LOCAL_MACHINE", _UAC_KEY, "EnableLUA")),
        "administrator_prompt_mode": admin_prompt,
        "standard_user_prompt_mode": user_prompt,
        "secure_desktop": bool(
            _registry_read("HKEY_LOCAL_MACHINE", _UAC_KEY, "PromptOnSecureDesktop")
        ),
    }


def _unsupported_set(_value):
    raise PermissionError("This setting is read-only.")


SETTINGS: dict[str, SettingSpec] = {
    "app_theme": SettingSpec(
        "app_theme", "Windows app theme", "Choose dark or light app colours.",
        rf"HKCU\{_THEME_KEY}", "AppsUseLightTheme", ("dark", "light"),
        False, RiskLevel.MEDIUM, True, False, _theme_get, _theme_set,
    ),
    "show_hidden_files": SettingSpec(
        "show_hidden_files", "Show hidden files", "Show or hide hidden files in File Explorer.",
        rf"HKCU\{_EXPLORER_KEY}", "Hidden", (True, False),
        False, RiskLevel.MEDIUM, True, False,
        _show_hidden_get, _show_hidden_set,
    ),
    "show_file_extensions": SettingSpec(
        "show_file_extensions", "Show file extensions", "Show or hide known file extensions.",
        rf"HKCU\{_EXPLORER_KEY}", "HideFileExt", (True, False),
        False, RiskLevel.MEDIUM, True, False,
        lambda: not _bool_get("HideFileExt"), _bool_set("HideFileExt", inverted=True),
    ),
    "uac_level": SettingSpec(
        "uac_level", "User Account Control configuration",
        "Read UAC enabled state and prompt modes; changing UAC is not supported.",
        rf"HKLM\{_UAC_KEY}", "UAC policy values", (),
        False, RiskLevel.LOW, False, False, _uac_get, _unsupported_set, read_only=True,
    ),
}

_undo_lock = threading.RLock()
_undo_stack: list[tuple[str, object]] = []
_undo_loaded = False
_UNDO_LOG = Path(__file__).resolve().parents[1] / "memory" / "settings_undo.jsonl"


def validate_setting_value(setting_id: str, value: object) -> SettingSpec:
    spec = SETTINGS.get(setting_id)
    if spec is None:
        raise ValueError(f"Unknown Windows setting: {setting_id}")
    if spec.read_only:
        raise ValueError(f"{spec.display_name} is read-only.")
    if type(value) is bool and type(value) in {type(item) for item in spec.allowed_values}:
        if value in spec.allowed_values:
            return spec
    elif type(value) is str and value in spec.allowed_values:
        return spec
    raise ValueError(f"Value is not allowed for {spec.display_name}.")


def get_setting(setting_id: str):
    spec = SETTINGS.get(setting_id)
    if spec is None:
        raise ValueError(f"Unknown Windows setting: {setting_id}")
    if platform.system() != "Windows":
        raise OSError("Windows settings are supported on Windows only.")
    return spec.getter()


def _append_undo(setting_id: str, previous: object, current: object):
    global _undo_loaded
    with _undo_lock:
        _UNDO_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "event": "set",
            "setting_id": setting_id,
            "previous_value": previous,
            "current_value": current,
        }
        with _UNDO_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _undo_stack.append((setting_id, previous))
        _undo_loaded = True


def _append_undo_completion(setting_id: str, restored_value: object):
    with _undo_lock:
        record = {
            "event": "undo",
            "setting_id": setting_id,
            "restored_value": restored_value,
        }
        with _UNDO_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _load_undo_stack():
    global _undo_loaded
    with _undo_lock:
        if _undo_loaded:
            return
        if _UNDO_LOG.is_file():
            loaded: list[tuple[str, object]] = []
            with _UNDO_LOG.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise TypeError(f"Undo log entry {line_number} is invalid.")
                    setting_id = record.get("setting_id")
                    spec = SETTINGS.get(setting_id)
                    if spec is None or spec.read_only:
                        raise ValueError(f"Undo log entry {line_number} names an unsupported setting.")
                    event = record.get("event", "set")
                    if event == "set":
                        previous = record.get("previous_value")
                        validate_setting_value(setting_id, previous)
                        loaded.append((setting_id, previous))
                    elif event == "undo":
                        restored = record.get("restored_value")
                        if loaded and loaded[-1] == (setting_id, restored):
                            loaded.pop()
                        else:
                            raise ValueError(f"Undo log entry {line_number} is inconsistent.")
                    else:
                        raise ValueError(f"Undo log entry {line_number} has an unknown event.")
            _undo_stack[:] = loaded
        _undo_loaded = True


def _record_result(broker, plan, decision, result: str) -> str:
    try:
        broker.record_operation_result(plan, decision, result)
    except OSError as exc:
        return f"{result} The operation result could not be saved to the audit log: {exc}"
    return result


def _confirm_read(broker, action_id: str, title: str, summary: str, path: str) -> tuple[bool, ConfirmationPlan]:
    plan = ConfirmationPlan(
        action_id=action_id,
        title=title,
        summary=summary,
        operations=(f"Read {path}",),
        risk_level=RiskLevel.LOW,
        paths=(path,),
        read_only=True,
        reversible=True,
    )
    return broker.request_confirmation(plan) is ConfirmationDecision.APPROVE, plan


def set_setting(setting_id: str, value: object) -> str:
    if platform.system() != "Windows":
        return "Windows settings are supported on Windows only."
    try:
        spec = validate_setting_value(setting_id, value)
        current = spec.getter()
        if current == value:
            return f"{spec.display_name} is already set to {value!s}."
        plan = ConfirmationPlan(
            action_id=f"setting:{setting_id}",
            title=f"Change {spec.display_name}",
            summary=f"I'm about to change {spec.display_name} from {current!s} to {value!s}.",
            operations=(
                f"Change {spec.registry_key}\\{spec.value_name}: {current!s} → {value!s}",
            ),
            risk_level=spec.risk_level,
            paths=(f"{spec.registry_key}\\{spec.value_name}",),
            item_count=1,
            reversible=spec.reversible,
            requires_admin=spec.requires_admin,
            restart_required=spec.restart_required,
        )
        broker = get_confirmation_broker()
        if broker is None:
            return "User denied this request: confirmation is unavailable."
        decision = broker.request_confirmation(plan)
        if decision is not ConfirmationDecision.APPROVE:
            return _record_result(broker, plan, decision, "User denied this request.")
        if spec.getter() != current:
            return _record_result(
                broker, plan, decision,
                "The setting changed after approval. Nothing was changed; please review again.",
            )
        try:
            spec.setter(value)
            verified = spec.getter()
        except OSError as exc:
            return _record_result(
                broker, plan, decision, f"Could not change the Windows setting: {exc}"
            )
        if verified != value:
            return _record_result(
                broker, plan, decision,
                f"Change was not verified: {spec.display_name} is still {verified!s}.",
            )
        try:
            _append_undo(setting_id, current, verified)
        except OSError as exc:
            return _record_result(
                broker, plan, decision,
                f"Verified: {spec.display_name} is now {verified!s}, but the undo log could not be saved: {exc}",
            )
        return _record_result(
            broker, plan, decision, f"Verified: {spec.display_name} is now {verified!s}."
        )
    except (OSError, ValueError, TypeError) as exc:
        return f"Could not change the Windows setting: {exc}"


def undo_last_setting() -> str:
    if platform.system() != "Windows":
        return "Windows settings are supported on Windows only."
    try:
        _load_undo_stack()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return f"Could not read the Windows settings undo log: {exc}"
    with _undo_lock:
        if not _undo_stack:
            return "There is no Windows setting change to undo in this session."
        setting_id, previous = _undo_stack[-1]
    spec = SETTINGS.get(setting_id)
    if spec is None or spec.read_only:
        return "The previous setting is no longer available to undo."
    try:
        current = spec.getter()
        plan = ConfirmationPlan(
            f"undo:{setting_id}",
            f"Undo {spec.display_name}",
            f"I'm about to restore {spec.display_name} from {current!s} to {previous!s}.",
            (f"Restore {spec.registry_key}\\{spec.value_name}: {current!s} → {previous!s}",),
            spec.risk_level,
            (f"{spec.registry_key}\\{spec.value_name}",),
            1, reversible=True,
        )
        broker = get_confirmation_broker()
        if broker is None or broker.request_confirmation(plan) is not ConfirmationDecision.APPROVE:
            decision = ConfirmationDecision.DENY
            return _record_result(broker, plan, decision, "User denied this undo request.") if broker else "User denied this undo request: confirmation is unavailable."
        if spec.getter() != current:
            return _record_result(
                broker, plan, ConfirmationDecision.APPROVE,
                "The setting changed after approval. Nothing was changed.",
            )
        try:
            spec.setter(previous)
            verified = spec.getter()
        except OSError as exc:
            return _record_result(
                broker, plan, ConfirmationDecision.APPROVE,
                f"Could not undo the Windows setting: {exc}",
            )
        if verified != previous:
            return _record_result(
                broker, plan, ConfirmationDecision.APPROVE,
                f"Undo was not verified: {spec.display_name} is {verified!s}.",
            )
        with _undo_lock:
            if _undo_stack and _undo_stack[-1] == (setting_id, previous):
                try:
                    _append_undo_completion(setting_id, verified)
                except OSError as exc:
                    return _record_result(
                        broker, plan, ConfirmationDecision.APPROVE,
                        f"Verified: {spec.display_name} restored to {verified!s}, but the undo log could not be updated: {exc}",
                    )
                _undo_stack.pop()
        return _record_result(
            broker, plan, ConfirmationDecision.APPROVE,
            f"Verified: {spec.display_name} restored to {verified!s}.",
        )
    except (OSError, ValueError, TypeError) as exc:
        return f"Could not undo the Windows setting: {exc}"


def execute(operation: str, parameters: dict) -> str:
    if platform.system() != "Windows":
        return "Windows settings are supported on Windows only."
    if operation == "list":
        broker = get_confirmation_broker()
        if broker is None:
            return "User denied this request: confirmation is unavailable."
        approved, plan = _confirm_read(
            broker, "settings:list", "List Windows settings",
            "I'm about to show the settings A.U.R.A is allowed to read or change.",
            "Windows settings allowlist",
        )
        decision = ConfirmationDecision.APPROVE if approved else ConfirmationDecision.DENY
        if not approved:
            return _record_result(broker, plan, decision, "User denied this request.")
        values = json.dumps([
            {
                "id": spec.setting_id,
                "name": spec.display_name,
                "description": spec.description,
                "allowed_values": list(spec.allowed_values),
                "read_only": spec.read_only,
            }
            for spec in SETTINGS.values()
        ], ensure_ascii=False)
        return _record_result(broker, plan, decision, values)
    if operation == "undo":
        return undo_last_setting()
    setting_id = parameters.get("setting_id")
    if not isinstance(setting_id, str) or setting_id not in SETTINGS:
        return "Unknown Windows setting; only allowlisted setting ids are available."
    if operation == "get":
        try:
            spec = SETTINGS[setting_id]
            broker = get_confirmation_broker()
            if broker is None:
                return "User denied this request: confirmation is unavailable."
            path = f"{spec.registry_key}\\{spec.value_name}"
            approved, plan = _confirm_read(
                broker, f"setting-read:{setting_id}", f"Read {spec.display_name}",
                f"I'm about to read the current value of {spec.display_name}.",
                path,
            )
            decision = ConfirmationDecision.APPROVE if approved else ConfirmationDecision.DENY
            if not approved:
                return _record_result(broker, plan, decision, "User denied this request.")
            value = get_setting(setting_id)
            return _record_result(
                broker, plan, decision, f"{spec.display_name}: {value!s}"
            )
        except (OSError, ValueError) as exc:
            return f"Could not read the Windows setting: {exc}"
    if operation == "set":
        return set_setting(setting_id, parameters.get("value"))
    return "That Windows settings operation is not available."
