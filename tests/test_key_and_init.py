"""
tests/test_key_and_init.py — Tests for Application Initialisation and Gemini API Key Updates
"""

import json
from pathlib import Path
import pytest
from core.startup import ensure_windows_administrator
from memory.config_manager import load_api_keys, save_api_keys, get_gemini_key
from main import _get_api_key


def test_ensure_windows_administrator_graceful():
    # Calling ensure_windows_administrator should never crash or raise SystemExit in non-admin shells
    ensure_windows_administrator()


def test_gemini_key_persistence_and_retrieval(tmp_path, monkeypatch):
    test_key = "AIzaSyTestDynamicKeyUpdate999"
    fake_config = tmp_path / "api_keys.json"
    fake_config.write_text(json.dumps({"os_system": "windows"}), encoding="utf-8")
    monkeypatch.setattr("memory.config_manager.CONFIG_FILE", fake_config)
    monkeypatch.setattr("memory.config_manager.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("core.gemini._KEY_FILE", fake_config)
    monkeypatch.setattr("main.API_CONFIG_PATH", str(fake_config))

    save_api_keys(test_key)
    assert get_gemini_key() == test_key
    assert _get_api_key() == test_key

    # Test cache invalidation in core.gemini
    from core import gemini as _gemini
    cached = _gemini.api_key(refresh=True)
    assert cached == test_key


def test_setup_overlay_probe_components_robustness():
    import sys
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    from ui import SetupOverlay
    overlay = SetupOverlay(None, {})
    settings = {
        "gemini_api_key": "AIzaSyTestKey",
        "mode": "gemini",
        "tts_engine": "kokoro",
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "model": "qwen2.5:1.5b",
    }
    probe_results = []
    overlay._probe_done.connect(lambda res: probe_results.append(res))
    overlay._probe_components(settings)
    assert len(probe_results) == 1
    res = probe_results[0]
    assert "stt" in res
    assert "llm" in res
    assert "tts" in res
    assert res["llm"][0] is True  # API key configured
