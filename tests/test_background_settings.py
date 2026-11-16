import asyncio
import json
from unittest.mock import patch

import pytest

from core.local_brain import LocalBrain
from memory import config_manager


def _use_temp_config(monkeypatch, tmp_path):
    config_file = tmp_path / "api_keys.json"
    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", config_file)
    return config_file


def test_background_settings_persist_without_dropping_existing_config(
    monkeypatch, tmp_path
):
    config_file = _use_temp_config(monkeypatch, tmp_path)
    config_file.write_text(
        json.dumps({"gemini_api_key": "existing", "assistant_name": "AURA"}),
        encoding="utf-8",
    )

    config_manager.save_wake_word_enabled(True)
    config_manager.save_wake_word_sensitivity(0.8)
    config_manager.save_start_at_login_enabled(True)

    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved["gemini_api_key"] == "existing"
    assert saved["assistant_name"] == "AURA"
    assert config_manager.get_wake_word_enabled() is True
    assert config_manager.get_wake_word_sensitivity() == 0.8
    assert config_manager.get_start_with_windows_enabled() is True
    assert config_manager.get_start_at_login_enabled() is True


@pytest.mark.parametrize("value", [0.09, 1.01, "invalid"])
def test_wake_word_sensitivity_rejects_out_of_range_values(value):
    with pytest.raises(ValueError):
        config_manager.save_wake_word_sensitivity(value)


def test_wake_word_sensitivity_clamps_corrupt_config(monkeypatch, tmp_path):
    config_file = _use_temp_config(monkeypatch, tmp_path)
    config_file.write_text('{"wake_word_sensitivity": 5}', encoding="utf-8")

    assert config_manager.get_wake_word_sensitivity() == 1.0


def test_wake_word_is_enabled_by_default_but_respects_explicit_opt_out(
    monkeypatch, tmp_path
):
    config_file = _use_temp_config(monkeypatch, tmp_path)
    assert config_manager.get_wake_word_enabled() is True

    config_file.write_text('{"wake_word_enabled": false}', encoding="utf-8")
    assert config_manager.get_wake_word_enabled() is False


def test_background_assistant_defaults_are_long_form_and_stay_active(
    monkeypatch, tmp_path
):
    _use_temp_config(monkeypatch, tmp_path)

    settings = config_manager.get_background_assistant_config()

    assert settings["end_of_speech_ms"] == 1800
    assert settings["max_utterance_seconds"] == 120
    assert settings["response_timeout_seconds"] == 120
    assert settings["auto_hide_enabled"] is False
    assert settings["speak_responses"] is True
    assert settings["conversation_mode"] is True


def test_background_assistant_settings_merge_with_existing_config(
    monkeypatch, tmp_path
):
    config_file = _use_temp_config(monkeypatch, tmp_path)
    config_file.write_text(
        json.dumps({"gemini_api_key": "existing", "assistant_name": "AURA"}),
        encoding="utf-8",
    )

    config_manager.save_background_assistant_config({
        "end_of_speech_ms": 2400,
        "vad_sensitivity": 0.8,
        "auto_hide_enabled": True,
        "auto_hide_minutes": 5,
    })

    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved["gemini_api_key"] == "existing"
    assert saved["assistant_name"] == "AURA"
    assert saved["background_assistant"]["end_of_speech_ms"] == 2400
    assert saved["background_assistant"]["auto_hide_minutes"] == 5
    assert config_manager.get_background_assistant_config()["vad_sensitivity"] == 0.8


def test_local_brain_uses_the_configured_response_timeout():
    async def execute(_name, _args):
        return ""

    async def speak(_sentence):
        return None

    brain = LocalBrain(
        "system",
        [],
        execute,
        speak,
        lambda _message: None,
        request_timeout=37,
    )
    with patch(
        "core.local_brain.call_llm_stream",
        return_value=iter([{"type": "done", "content": "", "tool_calls": []}]),
    ) as stream:
        asyncio.run(brain.ask("question"))

    assert stream.call_args.kwargs["timeout"] == 37


@pytest.mark.parametrize(
    "values",
    [
        {"end_of_speech_ms": 100},
        {"max_utterance_seconds": 601},
        {"response_timeout_seconds": "invalid"},
        {"vad_sensitivity": 0},
    ],
)
def test_background_assistant_settings_reject_invalid_values(values):
    with pytest.raises(ValueError):
        config_manager.save_background_assistant_config(values)
