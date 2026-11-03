"""Shared OpenCV camera opening helpers."""
from __future__ import annotations

import platform
from typing import Any


def _backends(cv2: Any) -> tuple[int, ...]:
    """Return backends in the order most reliable for the current platform."""
    if platform.system() == "Windows":
        # CAP_ANY lets OpenCV select the installed Windows capture provider
        # without probing a provider that may reject this camera index.
        return tuple(
            backend
            for backend in (
                getattr(cv2, "CAP_ANY", None),
                getattr(cv2, "CAP_DSHOW", None),
                getattr(cv2, "CAP_MSMF", None),
            )
            if backend is not None
        )
    if platform.system() == "Darwin":
        return (getattr(cv2, "CAP_AVFOUNDATION", cv2.CAP_ANY), cv2.CAP_ANY)
    return (getattr(cv2, "CAP_V4L2", cv2.CAP_ANY), cv2.CAP_ANY)


def open_camera(cv2: Any, indices: tuple[int, ...], warmup: int = 0) -> Any:
    """Open the first configured camera that returns a usable frame."""
    for index in indices:
        for backend in _backends(cv2):
            cap = cv2.VideoCapture(index, backend)
            if not cap.isOpened():
                cap.release()
                continue
            for _ in range(warmup):
                cap.read()
            ok, frame = cap.read()
            if ok and frame is not None:
                return cap
            cap.release()
    return None
