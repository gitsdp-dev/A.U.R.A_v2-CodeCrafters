from __future__ import annotations

import shutil
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"
VOSK_NAME = "vosk-model-small-en-us-0.15"
DOWNLOADS = {
    "kokoro-v1.0.onnx": (
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
        "model-files-v1.1/kokoro-v1.0.onnx"
    ),
    "voices-v1.0.bin": (
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
        "model-files-v1.1/voices-v1.0.bin"
    ),
}


def download(url: str, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".download")
    print(f"Downloading {destination.name} ...")
    try:
        with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        temporary.replace(destination)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            f"Failed to download {destination.name} from {url}: {exc}"
        ) from exc


def main() -> None:
    MODELS.mkdir(parents=True, exist_ok=True)
    vosk_dir = MODELS / VOSK_NAME
    if not vosk_dir.is_dir():
        archive_path = MODELS / f"{VOSK_NAME}.zip"
        download(
            f"https://alphacephei.com/vosk/models/{VOSK_NAME}.zip",
            archive_path,
        )
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                destination = (MODELS / member.filename).resolve()
                if not destination.is_relative_to(MODELS.resolve()):
                    raise ValueError(f"Unsafe path in Vosk archive: {member.filename}")
            archive.extractall(MODELS)
        archive_path.unlink()
    for name, url in DOWNLOADS.items():
        destination = MODELS / name
        if not destination.is_file():
            download(url, destination)
    print("Voice models are ready in the models directory.")


if __name__ == "__main__":
    main()
