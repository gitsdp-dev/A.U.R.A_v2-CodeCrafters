"""Resource limits and storage paths for local CAD jobs."""

from __future__ import annotations

import os
from pathlib import Path

MAX_QUESTIONS = 5
SCRIPT_TIMEOUT_SECONDS = 90
SCRIPT_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
MAX_SCRIPT_BYTES = 128 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_MODEL_RESPONSE_BYTES = 256 * 1024
MAX_TRIANGLES = 400_000
MAX_MESH_BYTES = 64 * 1024 * 1024
MAX_BOUNDING_DIMENSION_MM = 100_000.0
MAX_REPAIR_ATTEMPTS = 3
MAX_COMPONENTS = 12
PART_CACHE_ENABLED_DEFAULT = True
PART_CACHE_FUZZY_THRESHOLD = 0.88
PART_CACHE_MAX_BYTES = 256 * 1024 * 1024
PART_CACHE_MAX_ENTRIES = 1000
PART_CACHE_SCHEMA_VERSION = 1
# Add explicit compatibility mappings here; unlisted version changes invalidate.
PART_CACHE_VERSION_COMPATIBILITY = {
    "backend": {},
    "prompt": {},
}


def cad_data_dir() -> Path:
    """Keep generated files in the user's data area, not the source tree."""
    app_data = os.environ.get("LOCALAPPDATA")
    root = Path(app_data) if app_data else Path.home() / ".aura"
    return root / "AURA" / "cad" if app_data else root / "cad"


PROJECTS_DIR = cad_data_dir()
PART_CACHE_DIR = PROJECTS_DIR / "part_cache"
