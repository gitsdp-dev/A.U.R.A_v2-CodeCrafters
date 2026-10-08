"""
A.U.R.A v2 — one-time setup.

Installs the Python dependencies for THIS operating system only: the OS-specific
packages in requirements.txt carry `sys_platform` markers, so a macOS or Linux
user never pulls Windows-only libraries (and vice-versa). It also downloads
the offline voice models and configures the selected local LLM provider.

One thing it deliberately does NOT install:
  * the optional local wake word ("Hey Aura") — one-click, opt-in, from
    ⚙ → WAKE WORD inside the app;
  * anything for the avatar — its holographic head renders in software.
"""
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

OS = platform.system()  # "Windows" | "Darwin" | "Linux"
HERE = Path(__file__).resolve().parent

MIN_PY = (3, 11)        # hard floor: below this the syntax used here won't parse
MAX_PY = (3, 13)        # highest version this is actually tested on


def _run(label: str, args: list[str]) -> None:
    print(f"\n▶ {label}")
    subprocess.run(args, check=True, cwd=HERE)


def _check_python() -> None:
    """Fail immediately and clearly rather than deep inside a pip resolver.

    A wrong interpreter is the single most common way this install goes sideways,
    and the error it produces on its own names a wheel, not the real problem.
    """
    v = sys.version_info[:2]
    if v > MAX_PY:
        # Newer is a warning, not a wall. Turning away someone who installed
        # today's Python is a worse first impression than a version that
        # turns out to work fine, and if a wheel really is missing pip says
        # so plainly.
        print(f"\n⚠️  Python {v[0]}.{v[1]} is newer than the "
              f"{MAX_PY[0]}.{MAX_PY[1]} this is tested on. Continuing — if a "
              f"package has no wheel yet, install Python "
              f"{MAX_PY[0]}.{MAX_PY[1]} and run setup with that.")
        return
    if v < MIN_PY:
        print(f"\n❌ Python {v[0]}.{v[1]} detected — A.U.R.A v2 needs at "
              f"least Python {MIN_PY[0]}.{MIN_PY[1]}.")
        print("   Install a supported version and run setup with it, e.g.:")
        print(f"     py -{MIN_PY[0]}.{MIN_PY[1]} setup.py        (Windows)")
        print(f"     python{MIN_PY[0]}.{MIN_PY[1]} setup.py      (macOS / Linux)")
        sys.exit(1)


def _check_assets() -> None:
    """The avatar's face is a shipped file; a truncated clone should say so."""
    face = HERE / "core" / "face_model.obj"
    if not face.exists() or face.stat().st_size < 4096:
        print(
            "\n⚠️  core/face_model.obj is missing or truncated — the avatar will "
            "fall back to the plain glowing core.\n"
            "    Re-clone the repository, or fetch that one file again."
        )


def _configured_ollama_model() -> str:
    """Read the existing local model without changing A.U.R.A.'s configuration."""
    config_file = HERE / "config" / "api_keys.json"
    try:
        config = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "qwen2.5:1.5b"
    local = config.get("local_llm", {})
    if isinstance(local, dict):
        model = str(local.get("model", "")).strip()
        if model:
            return model
    return str(config.get("llm_model", "")).strip() or "qwen2.5:1.5b"


def _choose_provider() -> str:
    """Ask which provider the voice-mode command should use."""
    print("\nWhat Provider Would You Prefer:")
    print("a. Ollama (Press '1')")
    print("b. LM Studio (Press '2')")
    while True:
        try:
            choice = input("Enter 1 or 2 [1]: ").strip().lower() or "1"
        except EOFError:
            choice = "1"
            print("No interactive input detected; selecting the existing Ollama model.")
        if choice in {"1", "a", "ollama"}:
            return "ollama"
        if choice in {"2", "b", "lmstudio", "lm studio"}:
            return "lmstudio"
        print("Please enter 1 for Ollama or 2 for LM Studio.")


def _save_voice_provider(provider: str) -> None:
    """Persist only the voice provider selection; leave the existing model untouched."""
    import yaml

    config_path = HERE / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    llm = config.setdefault("llm", {})
    llm["provider"] = provider
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _prepare_voice_models() -> None:
    """Download Vosk and Kokoro assets using the project's resumable installer."""
    _run(
        "Downloading offline Vosk and Kokoro voice models…",
        [sys.executable, str(HERE / "scripts" / "download_models.py")],
    )


def _prepare_ollama(model: str) -> None:
    """Pull A.U.R.A.'s already-configured Ollama model when the CLI is present."""
    ollama = shutil.which("ollama")
    if not ollama:
        print(
            "\n⚠️ Ollama CLI was not found. Install Ollama from https://ollama.com, "
            "then run this command:\n"
            f"    ollama pull {model}"
        )
        return
    try:
        _run(f"Downloading the configured Ollama model ({model})…", [ollama, "pull", model])
    except subprocess.CalledProcessError as exc:
        print(
            f"\n⚠️ Ollama could not pull '{model}' (exit code {exc.returncode}). "
            "Start Ollama and retry:\n"
            f"    ollama pull {model}"
        )


def _show_voice_command(provider: str) -> None:
    command = f"python main.py --voice --provider {provider}"
    print(f"\nVoice mode is configured for {provider}.")
    if provider == "lmstudio":
        print(
            "In LM Studio, load a model, open the Developer tab, and click "
            "Start Server on port 1234."
        )
    else:
        print("Ensure Ollama is running before starting voice mode.")
    print(f"Run this command from the project folder:\n    {command}")


def main() -> None:
    print(f"⚙  A.U.R.A v2 setup — detected OS: {OS or 'unknown'}, "
          f"Python {sys.version_info[0]}.{sys.version_info[1]}")
    _check_python()

    # requirements.txt filters OS-specific extras by itself via pip markers.
    _run("Installing Python dependencies (OS-specific extras auto-filtered)…",
         [sys.executable, "-m", "pip", "install", "-r", str(HERE / "requirements.txt")])

    _prepare_voice_models()
    provider = _choose_provider()
    _save_voice_provider(provider)
    if provider == "ollama":
        _prepare_ollama(_configured_ollama_model())

    # Chromium covers Chrome/Edge/Opera/Brave/Vivaldi; Firefox for Firefox.
    # (Safari automation additionally needs: python -m playwright install webkit)
    # Not fatal: these are a few hundred megabytes from a CDN that a corporate
    # network or a flaky connection can refuse, and everything except browser
    # automation works without them. Failing the whole install there would send
    # a user away from a working app.
    try:
        _run("Installing Playwright browsers (chromium + firefox)…",
             [sys.executable, "-m", "playwright", "install", "chromium", "firefox"])
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"\n⚠️  Playwright browsers were not installed ({e}).")
        print("    Everything except browser automation works. Retry later with:")
        print(f'    {sys.executable} -m playwright install chromium firefox')

    _check_assets()

    # ── OS-specific post-install notes ────────────────────────────────────────
    if OS == "Windows":
        try:
            import win32com.client  # noqa: F401
        except ImportError:
            postinstall = Path(sys.executable).parent / "Scripts" / "pywin32_postinstall.py"
            print(
                "\n⚠️  pywin32 did not register correctly — desktop-shortcut "
                "creation will use a slower fallback. To fix it, run:\n"
                f'    "{sys.executable}" -m pip install --force-reinstall pywin32\n'
                f'    "{sys.executable}" "{postinstall}" -install'
            )
    elif OS == "Linux":
        print(
            "\nℹ️  Linux note — a few voice-controlled OS actions shell out to "
            "native tools. Install the ones you'll use via your package manager:\n"
            "    • volume      → pulseaudio-utils   (pactl)\n"
            "    • brightness  → brightnessctl\n"
            "    • reminders   → systemd (systemd-run) or 'at'\n"
            "    • open URLs   → xdg-utils          (xdg-open)"
        )
    elif OS == "Darwin":
        print(
            "\nℹ️  macOS note — volume, brightness and reminders use the built-in "
            "'osascript' / LaunchAgents, so no extra tools are required.\n"
            "    For Safari automation only: python -m playwright install webkit"
        )

    print("\n✅ Setup complete!")
    print("   Text/UI mode: python main.py")
    _show_voice_command(provider)
    print("   Voice model paths and wake-word settings: config.yaml")


if __name__ == "__main__":
    main()
