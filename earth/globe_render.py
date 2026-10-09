from __future__ import annotations

import numpy as np


def render_globe_surface(width: int, height: int, phase: float = 0.0) -> np.ndarray:
    """Return an RGB uint8 array for a simple spherical Earth preview.

    This is intentionally lightweight and deterministic: it renders a CPU-only
    stylised globe that keeps the app feeling alive without any external assets
    or network dependency. The output is fully in-app and unit-testable.
    """
    if width <= 0 or height <= 0:
        return np.zeros((max(1, height), max(1, width), 3), dtype=np.uint8)

    x = np.linspace(-1.0, 1.0, width, dtype=np.float32)[None, :]
    y = np.linspace(-1.0, 1.0, height, dtype=np.float32)[:, None]
    r2 = x * x + y * y
    mask = r2 <= 1.0

    ocean = np.empty((height, width, 3), dtype=np.float32)
    ocean[..., 0] = 0.12
    ocean[..., 1] = 0.35
    ocean[..., 2] = 0.70

    lon = np.arctan2(x, np.sqrt(np.clip(1.0 - r2, 0.0, 1.0)))
    lat = np.arcsin(np.clip(y, -1.0, 1.0))

    land = (
        (np.sin((lon * 2.75) + phase) + np.cos((lat * 2.2) - phase * 0.8) + np.sin((lon * 4.1) + (lat * 3.4) + phase * 1.2)) > 0.85
    ) | (
        ((np.abs(lon + 1.2) < 0.9) & (np.abs(lat - 0.18) < 0.8) & (np.sin((lon * 3.0) + phase * 1.6) > -0.35))
    ) | (
        ((np.abs(lon - 1.1) < 0.8) & (np.abs(lat + 0.35) < 0.7) & (np.cos((lon * 4.0) + phase) > 0.2))
    )

    land_color = np.array([0.18, 0.58, 0.28], dtype=np.float32)
    ocean[~mask] = 0.0
    ocean[mask & land] = land_color

    edge = np.clip(1.0 - np.sqrt(np.clip(r2, 0.0, 1.0)), 0.0, 1.0)
    ocean *= 0.82 + 0.34 * edge[..., None]
    ocean[~mask] = 0.0

    cloud = (np.sin((lon * 7.0) + phase * 2.0) * np.cos((lat * 5.0) - phase * 1.1) > 0.7)
    ocean[mask & cloud] = np.clip(ocean[mask & cloud] * 1.18 + np.array([0.08, 0.08, 0.08], dtype=np.float32), 0.0, 1.0)

    result = np.clip(ocean * 255.0, 0, 255).astype(np.uint8)
    result[~mask] = 0
    return result
