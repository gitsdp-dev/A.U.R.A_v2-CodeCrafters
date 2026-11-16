"""
A.U.R.A v2 — one-time setup.

Installs the Python dependencies for THIS operating system only: the OS-specific
packages in requirements.txt carry `sys_platform` markers, so a macOS or Linux
user never pulls Windows-only libraries (and vice-versa). It also downloads
the offline voice models and configures the selected local LLM provider.

One thing it deliberately does NOT enable:
  * the optional local wake word ("Hey Aura") — its Vosk and pynput runtime
    dependencies are installed with the core requirements, and listening can
    be changed from ⚙ → WAKE WORD inside the app;
  * anything for the avatar — its holographic head renders in software.
"""
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

OS = platform.system()  # "Windows" | "Darwin" | "Linux"
HERE = Path(__file__).resolve().parent

MIN_PY = (3, 10)
MAX_PY = (3, 14)        # highest version covered by the compatibility matrix


def _python_version(executable: str) -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            [
                executable, "-c",
                "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return None
        major, minor = result.stdout.strip().split(".")
        return int(major), int(minor)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _find_python312() -> str | None:
    """Find an installed Python 3.12 without relying on the current PATH alias."""
    if sys.version_info[:2] == (3, 12):
        return sys.executable

    if os.name == "nt" and shutil.which("py"):
        try:
            result = subprocess.run(
                ["py", "-3.12", "-c", "import sys; print(sys.executable)"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            candidate = result.stdout.strip()
            if result.returncode == 0 and candidate and _python_version(candidate) == (3, 12):
                return candidate
        except (OSError, subprocess.SubprocessError):
            pass

    for name in ("python3.12", "python3.12.exe"):
        candidate = shutil.which(name)
        if candidate and _python_version(candidate) == (3, 12):
            return candidate
    return None


def _venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def _setup_python(
    root: Path = HERE,
    current_version: tuple[int, int] | None = None,
    current_executable: str | None = None,
) -> str:
    """Use the current 3.12 environment, or prepare/reuse a Python 3.12 venv."""
    version = current_version or sys.version_info[:2]
    if version == (3, 12):
        return current_executable or sys.executable

    base_python = _find_python312()
    if not base_python:
        raise RuntimeError(
            "Python 3.12 is required when setup.py is run with another Python. "
            "Install Python 3.12 and rerun setup.py."
        )

    interpreter = _venv_python(root)
    venv_dir = root / ".venv"
    if interpreter.is_file():
        existing_version = _python_version(str(interpreter))
        if existing_version != (3, 12):
            found = (
                f"{existing_version[0]}.{existing_version[1]}"
                if existing_version
                else "an unknown version"
            )
            raise RuntimeError(
                f"{venv_dir} already contains {found}; refusing to replace it. "
                "Use a Python 3.12 .venv or move the existing environment first."
            )
        return str(interpreter)
    if venv_dir.exists():
        raise RuntimeError(
            f"{venv_dir} exists but has no usable Python interpreter; "
            "move or repair it before running setup.py."
        )

    _run(
        "Creating a Python 3.12 virtual environment…",
        [base_python, "-m", "venv", str(venv_dir)],
    )
    if not interpreter.is_file() or _python_version(str(interpreter)) != (3, 12):
        raise RuntimeError("Could not create a working Python 3.12 virtual environment.")
    return str(interpreter)


def _run(label: str, args: list[str]) -> None:
    print(f"\n▶ {label}")
    subprocess.run(args, check=True, cwd=HERE)


def _install_requirements(python_exe: str) -> None:
    """Install core (including background assistant), optional, and CAD requirements."""
    _run(
        "Installing core and background assistant dependencies…",
        [python_exe, "-m", "pip", "install", "-r", str(HERE / "requirements.txt")],
    )
    for filename, label, feature in (
        ("requirements-optional.txt", "optional integrations", "optional integrations"),
        ("requirements-cad.txt", "CAD design support", "CAD design"),
    ):
        try:
            _run(
                f"Installing {label}…",
                [python_exe, "-m", "pip", "install", "-r", str(HERE / filename)],
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            print(
                f"\n⚠️  Could not install {feature} requirements ({exc}). "
                f"Retry with:\n    \"{python_exe}\" -m pip install -r "
                f"\"{HERE / filename}\""
            )


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


def _prepare_voice_models(python_exe: str) -> None:
    """Download Vosk and Kokoro assets using the project's resumable installer."""
    _run(
        "Downloading offline Vosk and Kokoro voice models…",
        [python_exe, str(HERE / "scripts" / "download_models.py")],
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


def _show_voice_command(provider: str, python_exe: str) -> None:
    interpreter = (
        subprocess.list2cmdline([python_exe])
        if os.name == "nt"
        else shlex.quote(python_exe)
    )
    command = f"{interpreter} main.py --voice --provider {provider}"
    print(f"\nVoice mode is configured for {provider}.")
    if provider == "lmstudio":
        print(
            "In LM Studio, load a model, open the Developer tab, and click "
            "Start Server on port 1234."
        )
    else:
        print("Ensure Ollama is running before starting voice mode.")
    print(f"Run this command from the project folder:\n    {command}")


def _gui_executable(python_exe: str) -> str:
    """Prefer pythonw on Windows so launching the GUI does not create a console."""
    if OS == "Windows":
        pythonw = Path(python_exe).with_name("pythonw.exe")
        if pythonw.is_file():
            return str(pythonw)
    return python_exe


def main() -> None:
    print(f"⚙  A.U.R.A v2 setup — detected OS: {OS or 'unknown'}, "
          f"Python {sys.version_info[0]}.{sys.version_info[1]}")
    _check_python()
    try:
        python_exe = _setup_python()
    except RuntimeError as exc:
        print(f"\n❌ {exc}")
        raise SystemExit(1) from exc

    # Each requirements file filters OS/Python-specific packages with markers.
    _install_requirements(python_exe)

    _prepare_voice_models(python_exe)
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
             [python_exe, "-m", "playwright", "install", "chromium", "firefox"])
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"\n⚠️  Playwright browsers were not installed ({e}).")
        print("    Everything except browser automation works. Retry later with:")
        print(f'    {python_exe} -m playwright install chromium firefox')

    _check_assets()

    # ── OS-specific post-install notes ────────────────────────────────────────
    if OS == "Windows":
        try:
            import win32com.client  # noqa: F401
        except ImportError:
            scripts_dir = Path(python_exe).parent
            if scripts_dir.name.casefold() != "scripts":
                scripts_dir /= "Scripts"
            postinstall = scripts_dir / "pywin32_postinstall.py"
            print(
                "\n⚠️  pywin32 did not register correctly — desktop-shortcut "
                "creation will use a slower fallback. To fix it, run:\n"
                f'    "{python_exe}" -m pip install --force-reinstall pywin32\n'
                f'    "{python_exe}" "{postinstall}" -install'
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
    print("   Background wake-word support is installed; its setting defaults to on.")
    print("   Change wake-word listening in Customize settings.")
    interpreter = (
        subprocess.list2cmdline([_gui_executable(python_exe)])
        if os.name == "nt"
        else shlex.quote(_gui_executable(python_exe))
    )
    print(f"   Text/UI mode: {interpreter} main.py")
    _show_voice_command(provider, python_exe)
    print("   Voice model paths and wake-word settings: config.yaml")


if __name__ == "__main__":
    main()
