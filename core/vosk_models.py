"""Download and cache supported Vosk speech-recognition models."""
from __future__ import annotations

import os
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path


_MODEL_CATALOG = (
    ("US English · small · 40 MB", "vosk-model-small-en-us-0.15"),
    ("Indian English · small · 36 MB", "vosk-model-small-en-in-0.4"),
    ("Chinese · small · 42 MB", "vosk-model-small-cn-0.22"),
    ("Russian · small · 45 MB", "vosk-model-small-ru-0.22"),
    ("French · small · 41 MB", "vosk-model-small-fr-0.22"),
    ("French (Guyot) · small · 39 MB", "vosk-model-small-fr-pguyot-0.3"),
    ("German · small · 45 MB", "vosk-model-small-de-0.15"),
    ("Spanish · small · 39 MB", "vosk-model-small-es-0.42"),
    ("Portuguese · small · 31 MB", "vosk-model-small-pt-0.3"),
    ("Turkish · small · 35 MB", "vosk-model-small-tr-0.3"),
    ("Vietnamese · small · 32 MB", "vosk-model-small-vn-0.4"),
    ("Italian · small · 48 MB", "vosk-model-small-it-0.22"),
    ("Dutch · small · 39 MB", "vosk-model-small-nl-0.22"),
    ("Catalan · small · 42 MB", "vosk-model-small-ca-0.4"),
    ("Arabic (Tunisian) · small · 158 MB", "vosk-model-small-ar-tn-0.1-linto"),
    ("Persian · small · 53 MB", "vosk-model-small-fa-0.42"),
    ("Persian · larger small · 60 MB", "vosk-model-small-fa-0.5"),
    ("Ukrainian · nano · 73 MB", "vosk-model-small-uk-v3-nano"),
    ("Ukrainian · small · 133 MB", "vosk-model-small-uk-v3-small"),
    ("Kazakh · small · 58 MB", "vosk-model-small-kz-0.42"),
    ("Swedish · small · 289 MB", "vosk-model-small-sv-rhasspy-0.15"),
    ("Japanese · small · 48 MB", "vosk-model-small-ja-0.22"),
    ("Esperanto · small · 42 MB", "vosk-model-small-eo-0.42"),
    ("Hindi · small · 42 MB", "vosk-model-small-hi-0.22"),
    ("Czech · small · 44 MB", "vosk-model-small-cs-0.4-rhasspy"),
    ("Polish · small · 50 MB", "vosk-model-small-pl-0.22"),
    ("Uzbek · small · 49 MB", "vosk-model-small-uz-0.22"),
    ("Korean · small · 82 MB", "vosk-model-small-ko-0.22"),
    ("Gujarati · small · 100 MB", "vosk-model-small-gu-0.42"),
    ("Tajik · small · 50 MB", "vosk-model-small-tg-0.22"),
    ("Telugu · small · 58 MB", "vosk-model-small-te-0.42"),
    ("Kyrgyz · small · 49 MB", "vosk-model-small-ky-0.42"),
    ("Georgian · small · 45 MB", "vosk-model-small-ka-0.42"),
    ("US English · higher accuracy · 128 MB", "vosk-model-en-us-0.22-lgraph"),
)

VOSK_MODEL_OPTIONS = _MODEL_CATALOG
_MODEL_URLS = {
    name: f"https://alphacephei.com/vosk/models/{name}.zip"
    for _, name in _MODEL_CATALOG
}

VOSK_MODELS_DIR = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    / "AURA" / "models" / "vosk"
)
DEFAULT_VOSK_MODEL = "vosk-model-small-en-us-0.15"


def _is_model_directory(path: Path) -> bool:
    return (path / "am" / "final.mdl").is_file() and (
        path / "conf" / "mfcc.conf"
    ).is_file()


def ensure_vosk_model(
    model_name: str = DEFAULT_VOSK_MODEL,
    models_dir: Path | None = None,
) -> str:
    """Return a local model path, downloading its official archive if needed."""
    if model_name not in _MODEL_URLS:
        raise ValueError(f"Unsupported Vosk model: {model_name}")

    root = Path(models_dir) if models_dir is not None else VOSK_MODELS_DIR
    destination = root / model_name
    if _is_model_directory(destination):
        return str(destination)

    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{model_name}-", dir=root) as temp:
        staging = Path(temp)
        archive = staging / "model.zip"
        extracted = staging / "extracted"
        extracted.mkdir()
        url = _MODEL_URLS[model_name]
        print(f"[STT] Downloading Vosk model '{model_name}' (one-time)…")
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                with archive.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
        except Exception as exc:
            raise RuntimeError(
                f"Could not download Vosk model '{model_name}'. "
                f"Check the internet connection and retry. Details: {exc}"
            ) from exc

        try:
            with zipfile.ZipFile(archive) as model_archive:
                base = extracted.resolve()
                for member in model_archive.infolist():
                    member_path = (extracted / member.filename).resolve()
                    if not member_path.is_relative_to(base):
                        raise ValueError(
                            f"Unsafe path in Vosk model archive: {member.filename}"
                        )
                model_archive.extractall(extracted)
        except Exception as exc:
            raise RuntimeError(
                f"Could not unpack Vosk model '{model_name}': {exc}"
            ) from exc

        candidate = extracted / model_name
        if not _is_model_directory(candidate):
            candidates = [
                path for path in extracted.iterdir()
                if path.is_dir() and _is_model_directory(path)
            ]
            if len(candidates) != 1:
                raise RuntimeError(
                    f"Downloaded Vosk archive '{model_name}' has an "
                    "unexpected layout."
                )
            candidate = candidates[0]

        if destination.exists():
            shutil.rmtree(destination)
        candidate.replace(destination)

    if not _is_model_directory(destination):
        raise RuntimeError(f"Vosk model '{model_name}' is incomplete.")
    print(f"[STT] Vosk model '{model_name}' ready.")
    return str(destination)
