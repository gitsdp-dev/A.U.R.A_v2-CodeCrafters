# Background Assistant

A.U.R.A. can stay in the notification area/menu bar after the main window is
closed. Closing the main window hides it and returns it to offline wake-word
listening when enabled. Start it directly with `python main.py --background`.
A second launch opens the existing instance.

**Enable offline Hey AURA wake word** is on by default and can be changed in
**Settings → Customize**. After detecting the wake phrase, the background
listener opens the voice session and shows the floating orb. Vosk runs locally
on the CPU. Its small English model is downloaded on first use and cached for
the current user. The listener releases its audio stream before the assistant
session starts. The greeting is spoken by the configured A.U.R.A. brain and
voice.

The orb remains active through silence, long model requests, and follow-up
questions. Its response panel supports Markdown, scrolling, copy, expand, text
follow-ups, and Stop/Close controls. Local speech endpointing defaults to 1.8
seconds of silence, a 120-second utterance limit, a 300 ms minimum voice
duration, and a 120-second response timeout with one retry. The orb's Customize
settings save these preferences in `config/api_keys.json` under
`background_assistant`; auto-hide is off by default. Existing `voice`,
`llm`, and other A.U.R.A. configuration remains unchanged.

The tray/menu-bar menu contains **Open AURA**, **Pause Listening** or
**Resume Listening**, **Start at login**, and **Quit**. Startup registration is
per-user: a Windows Startup shortcut, a macOS LaunchAgent, or a Linux
freedesktop autostart entry. The login preference is stored in
`config/api_keys.json`. Windows keeps the existing `start_with_windows` config
key for compatibility.

## Platform notes

- **Windows:** existing notification-area behavior is retained.
- **macOS:** `pynput` global dismissal needs A.U.R.A. enabled under **System
  Settings → Privacy & Security → Accessibility** and **Input Monitoring**.
  Microphone access is requested by macOS; if denied, enable it under
  **Privacy & Security → Microphone** and restart A.U.R.A. The menu-bar icon
  stays available without a Dock icon for background launches.
- **Linux:** X11 uses the global input listener. On Wayland, global mouse hooks
  are prohibited by the compositor, so A.U.R.A. temporarily places a
  transparent full-screen click-catcher behind the orb. The click that
  dismisses the orb may not pass through to the application underneath. Orb
  positioning and always-on-top behavior on Wayland remain compositor-dependent.
  If `QSystemTrayIcon.isSystemTrayAvailable()` is false (for example GNOME
  without a tray/AppIndicator extension), A.U.R.A. continues running in the
  background and logs the missing tray. Launching A.U.R.A. again opens the
  running instance with the same menu attached to its main window. Installing or
  enabling the desktop's tray/AppIndicator extension restores tray controls.

The optional wake-word backend uses `sounddevice`/PortAudio and Vosk. A.U.R.A.
uses the selected input device's native sample rate and resamples to Vosk's
16 kHz input when necessary. Pause Listening stops the wake listener and closes
its input stream.

## Packaging macOS builds

Merge the keys in `packaging/macos/Info.plist` into the PyInstaller-generated
`Contents/Info.plist`; retain both `LSUIElement` and
`NSMicrophoneUsageDescription`. Build the `.app` with a stable bundle
identifier (for example `com.codecrafters.aura`) so macOS permissions remain
associated with the installed application. An app launched with `--background`
also requests the accessory activation policy at runtime; opening its main
window restores normal Dock/Cmd-Tab behavior.

Explicit `--background` launch suppresses the normal startup briefing and
automatic panels.
