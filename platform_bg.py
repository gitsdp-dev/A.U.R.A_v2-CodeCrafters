"""Platform adapters for A.U.R.A.'s background residency features."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
SERVER_LABEL = "com.codecrafters.aura"
_LOG = logging.getLogger("aura.background.platform")


def is_windows() -> bool:
    return sys.platform == "win32"


def is_macos() -> bool:
    return sys.platform == "darwin"


def autostart_supported() -> bool:
    return is_windows() or is_macos() or sys.platform.startswith("linux")


def _is_wayland_session() -> bool:
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    return session == "wayland" or (
        not session and bool(os.environ.get("WAYLAND_DISPLAY"))
    )


def tray_icon_path() -> Path:
    filename = "aura-tray.svg" if is_macos() else "aura.ico"
    return APP_ROOT / "config" / filename


@dataclass
class ClickHook:
    fallback: bool = False
    message: str | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    listeners: list = field(default_factory=list)


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False) or "__compiled__" in globals())


def _launch_arguments(
    background: bool = True,
    open_window: bool = False,
) -> list[str]:
    if sys.platform.startswith("linux") and _is_frozen():
        appimage = os.environ.get("APPIMAGE")
        if appimage and Path(appimage).is_file():
            option = "--background" if background else "--open" if open_window else None
            return [appimage, *([option] if option else [])]
    arguments = [sys.executable]
    if not _is_frozen():
        arguments.append(str(APP_ROOT / "main.py"))
    if background:
        arguments.append("--background")
    elif open_window:
        arguments.append("--open")
    return arguments


def _working_directory() -> Path:
    return Path(sys.executable).resolve().parent if _is_frozen() else APP_ROOT


def autostart_enable() -> None:
    if sys.platform == "win32":
        _windows_autostart(True)
    elif sys.platform == "darwin":
        _macos_autostart(True)
    elif sys.platform.startswith("linux"):
        _linux_autostart(True)
    else:
        raise OSError(f"Autostart is unsupported on {platform.system()}.")


def autostart_disable() -> None:
    if sys.platform == "win32":
        _windows_autostart(False)
    elif sys.platform == "darwin":
        _macos_autostart(False)
    elif sys.platform.startswith("linux"):
        _linux_autostart(False)
    else:
        raise OSError(f"Autostart is unsupported on {platform.system()}.")


def autostart_is_enabled() -> bool:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return bool(
            appdata
            and (
                Path(appdata)
                / "Microsoft"
                / "Windows"
                / "Start Menu"
                / "Programs"
                / "Startup"
                / "AURA Background.lnk"
            ).is_file()
        )
    if sys.platform == "darwin":
        return _macos_plist().is_file()
    if sys.platform.startswith("linux"):
        return _linux_desktop_file().is_file()
    return False


def _windows_autostart(enabled: bool) -> None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise OSError("The Windows APPDATA directory is unavailable.")
    startup = (
        Path(appdata)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )
    shortcut_path = startup / "AURA Background.lnk"
    if not enabled:
        shortcut_path.unlink(missing_ok=True)
        return
    startup.mkdir(parents=True, exist_ok=True)
    target = sys.executable
    arguments = ["--background"]
    if not _is_frozen():
        arguments.insert(0, str(APP_ROOT / "main.py"))
        if Path(target).name.casefold() == "python.exe":
            pythonw = Path(target).with_name("pythonw.exe")
            if pythonw.is_file():
                target = str(pythonw)
    try:
        import win32com.client
    except ImportError as exc:
        raise RuntimeError(
            "Windows shortcut support requires pywin32. Reinstall the app "
            "dependencies and try again."
        ) from exc
    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortcut(str(shortcut_path))
    shortcut.TargetPath = target
    shortcut.Arguments = subprocess.list2cmdline(arguments)
    shortcut.WorkingDirectory = str(APP_ROOT)
    shortcut.IconLocation = str(APP_ROOT / "config" / "aura.ico")
    shortcut.Description = "Start A.U.R.A. in the background"
    shortcut.Save()


def _macos_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{SERVER_LABEL}.plist"


def _macos_autostart(enabled: bool) -> None:
    plist = _macos_plist()
    domain = f"gui/{os.getuid()}"
    if not enabled:
        subprocess.run(
            ["launchctl", "bootout", domain, str(plist)],
            check=False,
            capture_output=True,
            text=True,
        )
        plist.unlink(missing_ok=True)
        return

    import plistlib

    plist.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": SERVER_LABEL,
        "ProgramArguments": _launch_arguments(),
        "WorkingDirectory": str(APP_ROOT),
        "RunAtLoad": True,
        "KeepAlive": False,
    }
    temporary = plist.with_suffix(".plist.tmp")
    temporary.write_bytes(plistlib.dumps(payload))
    temporary.replace(plist)
    subprocess.run(
        ["launchctl", "bootout", domain, str(plist)],
        check=False,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        plist.unlink(missing_ok=True)
        raise OSError(
            "Could not load A.U.R.A.'s LaunchAgent: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def _linux_desktop_file() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "autostart" / "aura.desktop"


def _desktop_exec_token(value: str) -> str:
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _linux_autostart(enabled: bool) -> None:
    desktop_file = _linux_desktop_file()
    if not enabled:
        desktop_file.unlink(missing_ok=True)
        return
    desktop_file.parent.mkdir(parents=True, exist_ok=True)
    command = " ".join(_desktop_exec_token(arg) for arg in _launch_arguments())
    contents = "\n".join(
        (
            "[Desktop Entry]",
            "Type=Application",
            "Name=A.U.R.A.",
            "Comment=Start A.U.R.A. in the background",
            f"Exec={command}",
            f"Path={_working_directory()}",
            f"Icon={APP_ROOT / 'config' / 'aura.ico'}",
            "Terminal=false",
            "X-GNOME-Autostart-enabled=true",
            "",
        )
    )
    desktop_file.write_text(contents, encoding="utf-8")


def tray_available() -> bool:
    try:
        from PyQt6.QtWidgets import QSystemTrayIcon

        return bool(QSystemTrayIcon.isSystemTrayAvailable())
    except Exception as exc:  # noqa: BLE001 - Qt platform plugins vary by desktop
        _LOG.warning("Could not query system tray availability: %s", exc)
        return False


def mic_permission_check() -> bool | None:
    """Return False only when macOS explicitly denies microphone access."""
    if sys.platform != "darwin":
        return True
    try:
        import AVFoundation

        status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
            AVFoundation.AVMediaTypeAudio
        )
        denied = getattr(AVFoundation, "AVAuthorizationStatusDenied", 2)
        restricted = getattr(AVFoundation, "AVAuthorizationStatusRestricted", 1)
        authorized = getattr(AVFoundation, "AVAuthorizationStatusAuthorized", 3)
        if status in (denied, restricted):
            return False
        if status == authorized:
            return True
        return None
    except Exception as exc:  # noqa: BLE001 - AVFoundation authorization varies by macOS release
        _LOG.info("Could not query macOS microphone authorization: %s", exc)
        return None


def _mac_accessibility_allowed() -> bool:
    try:
        import Quartz

        accessibility = bool(Quartz.AXIsProcessTrusted())
        input_monitoring = bool(Quartz.CGPreflightListenEventAccess())
        return accessibility and input_monitoring
    except Exception as exc:  # noqa: BLE001 - Quartz APIs vary by macOS release
        _LOG.info("Could not check macOS input permissions: %s", exc)
        return False


def start_global_click_hook(on_dismiss: Callable[[str], None]) -> ClickHook:
    """Start a temporary global listener or report that a UI fallback is needed."""
    hook = ClickHook()
    if sys.platform.startswith("linux") and _is_wayland_session():
        hook.fallback = True
        hook.message = (
            "Wayland blocks global mouse hooks; using a transparent click-catcher. "
            "Its dismissing click may not pass through to the application underneath."
        )
        try:
            from pynput import keyboard

            keyboard_listener = keyboard.Listener(
                on_press=lambda key: (
                    on_dismiss("escape") if key == keyboard.Key.esc else None
                ),
                suppress=False,
            )
            keyboard_listener.start()
            hook.listeners.append(keyboard_listener)
        except Exception as exc:  # noqa: BLE001 - global keyboard access varies by compositor
            _LOG.info("Wayland global Escape listener unavailable: %s", exc)
        return hook
    if sys.platform == "darwin" and not _mac_accessibility_allowed():
        hook.fallback = True
        hook.message = (
            "Global click dismissal needs Accessibility and Input Monitoring "
            "permission. Enable A.U.R.A. in System Settings → Privacy & Security; "
            "using the local click-catcher until then."
        )
        return hook

    try:
        from pynput import keyboard, mouse

        def on_click(x, y, _button, pressed):
            if pressed:
                on_dismiss(f"click:{int(x)}:{int(y)}")

        def on_press(key):
            if key == keyboard.Key.esc:
                on_dismiss("escape")

        mouse_listener = mouse.Listener(on_click=on_click, suppress=False)
        keyboard_listener = keyboard.Listener(on_press=on_press, suppress=False)
        hook.listeners = [mouse_listener, keyboard_listener]
        for listener in hook.listeners:
            listener.start()
    except Exception as exc:  # noqa: BLE001 - input backends have OS-specific failures
        for listener in hook.listeners:
            try:
                listener.stop()
                listener.join(timeout=0.5)
            except Exception as cleanup_error:  # noqa: BLE001 - best-effort native hook cleanup
                _LOG.warning(
                    "Could not stop a partially-started input hook: %s",
                    cleanup_error,
                )
        hook.listeners.clear()
        if sys.platform == "win32":
            _start_windows_polling(hook, on_dismiss)
        else:
            hook.fallback = True
            hook.message = f"Global click dismissal is unavailable: {exc}"
    return hook


def _start_windows_polling(hook: ClickHook, on_dismiss: Callable[[str], None]) -> None:
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
    except (AttributeError, ImportError) as exc:
        hook.fallback = True
        hook.message = f"Global click dismissal is unavailable: {exc}"
        return

    def poll() -> None:
        virtual_keys = (0x01, 0x02, 0x04, 0x05, 0x06, 0x1B)
        previous = {
            key: bool(user32.GetAsyncKeyState(key) & 0x8000)
            for key in virtual_keys
        }
        while not hook.stop_event.wait(0.008):
            for key in virtual_keys:
                down = bool(user32.GetAsyncKeyState(key) & 0x8000)
                if down and not previous[key]:
                    if key == 0x1B:
                        on_dismiss("escape")
                    else:
                        point = wintypes.POINT()
                        if user32.GetCursorPos(ctypes.byref(point)):
                            on_dismiss(f"click:{point.x}:{point.y}")
                previous[key] = down

    thread = threading.Thread(target=poll, name="aura-dismiss-hook", daemon=True)
    hook.listeners = [thread]
    thread.start()


def stop_global_click_hook(hook: ClickHook | None) -> None:
    if hook is None:
        return
    hook.stop_event.set()
    listeners, hook.listeners = hook.listeners, []
    for listener in listeners:
        stop = getattr(listener, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception as exc:  # noqa: BLE001 - native listener may already have stopped
                _LOG.warning("Could not stop an input listener cleanly: %s", exc)
        join = getattr(listener, "join", None)
        if callable(join) and listener is not threading.current_thread():
            try:
                join(timeout=0.5)
            except Exception as exc:  # noqa: BLE001 - native listener may already have stopped
                _LOG.warning("Could not join an input listener cleanly: %s", exc)


def launch_app(name: str = "AURA") -> None:
    """Launch A.U.R.A. when no instance is reachable; the new process relays SHOW."""
    if sys.platform == "darwin":
        app_bundle = next(
            (
                parent
                for parent in Path(sys.executable).resolve().parents
                if parent.suffix == ".app"
            ),
            None,
        )
        if app_bundle is not None:
            subprocess.Popen(["open", "-a", str(app_bundle)])
            return
    elif sys.platform.startswith("linux"):
        launcher_id = name.casefold().replace(" ", "-")
        desktop_file = (
            Path.home()
            / ".local"
            / "share"
            / "applications"
            / f"{launcher_id}.desktop"
        )
        if desktop_file.is_file() and shutil.which("gtk-launch"):
            subprocess.Popen(["gtk-launch", launcher_id])
            return

    arguments = _launch_arguments(background=False, open_window=True)
    if sys.platform == "win32" and not _is_frozen():
        python = Path(arguments[0])
        pythonw = python.with_name("pythonw.exe")
        if pythonw.is_file():
            arguments[0] = str(pythonw)
    subprocess.Popen(
        arguments,
        cwd=str(_working_directory()),
    )


def configure_background_app(background: bool) -> None:
    """Hide a macOS background instance from Dock/Cmd-Tab when PyObjC is present."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

        application = NSApplication.sharedApplication()
        if background:
            application.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        else:
            application.setActivationPolicy_(0)
    except Exception as exc:  # noqa: BLE001 - native application policy varies by Qt/macOS build
        _LOG.warning("Could not update the macOS activation policy: %s", exc)
        return


def configure_orb(widget) -> None:
    """Apply best-effort native desktop hints without changing the orb widget."""
    if sys.platform == "darwin":
        try:
            import AppKit
            import objc

            native_view = objc.objc_object(c_void_p=int(widget.winId()))
            native_window = native_view.window()
            behavior = (
                AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                | AppKit.NSWindowCollectionBehaviorStationary
                | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
            )
            native_window.setCollectionBehavior_(behavior)
        except Exception as exc:  # noqa: BLE001 - native window handles vary by Qt/macOS build
            _LOG.warning("Could not configure macOS orb Spaces behavior: %s", exc)
            return
    elif sys.platform.startswith("linux"):
        if _is_wayland_session():
            _LOG.info(
                "Wayland may restrict orb positioning and always-on-top behavior; "
                "using Qt's frameless-window fallback."
            )
            return
        # Qt.Tool in FloatingOrbOverlay maps to the utility window type on X11.
