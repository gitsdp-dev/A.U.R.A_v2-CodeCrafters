from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from aura_cad.errors import SandboxError, SandboxTimeout, ValidationError
from aura_cad.export import checks as mesh_checks
from aura_cad.export.checks import validate_mesh
from aura_cad.llm.adapter import CadLLMAdapter
from aura_cad.llm.planner import CadPlanner
from aura_cad.models import CadSpec
from aura_cad.sandbox.runner import SandboxRunner, ensure_backend_available
from aura_cad.sandbox.validator import (
    MATH_API,
    MATH_FUNCTIONS,
    validate_script,
)
from aura_cad.spec.clarifier import CadClarifier, StepKind
from aura_cad.spec.intent import detect_intent
from aura_cad.store.project_store import ProjectStore


def test_intent_detection_is_specific():
    assert detect_intent("Sketch me a car engine") == "engine"
    assert detect_intent("Make a gear with 20 teeth") == "gear"
    assert detect_intent("What is a gear?") is None


def test_clarifier_batched_answers_and_skip():
    clarifier = CadClarifier()
    step = clarifier.start("Sketch me a car engine")
    assert step.kind == StepKind.QUESTION
    assert step.question == "Inline, V-type or flat?"
    step = clarifier.answer("V8, 8 cylinders, full internals")
    assert step.kind == StepKind.CONFIRM
    assert step.spec.slots["layout"] == "V8"
    assert step.spec.slots["cylinders"] == 8
    assert step.spec.slots["detail"] == "Internals"

    skipped = CadClarifier()
    skipped.start("Sketch a car engine")
    step = skipped.answer("just build it")
    assert step.kind == StepKind.CONFIRM
    assert step.spec.assumptions


@pytest.mark.parametrize(
    ("input_text", "expected_layout", "expected_cylinders"),
    [
        ("Build an inline 4-cylinder engine", "Inline", 4),
        ("Build an inline four cylinder engine", "Inline", 4),
        ("Build a four-cylinder engine", None, 4),
    ],
)
def test_clarifier_extracts_engine_layout_and_worded_cylinder_count(
    input_text, expected_layout, expected_cylinders
):
    clarifier = CadClarifier()
    step = clarifier.start(input_text)
    assert clarifier.spec.slots.get("layout") == expected_layout
    assert clarifier.spec.slots["cylinders"] == expected_cylinders
    assert step.question != "How many cylinders?"


def test_clarifier_question_cap_and_cancel():
    clarifier = CadClarifier(max_questions=1)
    assert clarifier.start("Sketch an engine").kind == StepKind.QUESTION
    assert clarifier.answer("not sure").kind == StepKind.CONFIRM
    assert clarifier.answer("cancel").kind == StepKind.CANCEL


def test_clarifier_accepts_batched_gear_values():
    clarifier = CadClarifier()
    step = clarifier.start("Make a gear with 20 teeth and module 2")
    assert step.kind == StepKind.CONFIRM
    assert step.spec.slots["teeth"] == 20
    assert step.spec.slots["module_mm"] == 2.0


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("the first option", 12),
        ("second option please", 20),
        ("choose option 3", 32),
    ],
)
def test_clarifier_accepts_spoken_option_ordinals(answer, expected):
    clarifier = CadClarifier()
    assert clarifier.start("Make a gear").kind == StepKind.QUESTION
    step = clarifier.answer(answer)
    assert step.kind == StepKind.QUESTION
    assert step.spec.slots["teeth"] == expected


@pytest.mark.parametrize(
    ("answer", "expected_kind"),
    [
        ("first option", StepKind.BUILD),
        ("second option", StepKind.QUESTION),
        ("third option", StepKind.CANCEL),
    ],
)
def test_confirmation_accepts_spoken_option_ordinals(answer, expected_kind):
    clarifier = CadClarifier()
    clarifier.start("Make a gear")
    step = clarifier.answer("just build it")
    assert step.kind == StepKind.CONFIRM
    step = clarifier.answer(answer)
    assert step.kind == expected_kind


@pytest.mark.parametrize(
    "script",
    [
        "import os\ndef build():\n    return bd.Box(1, 1, 1)\n",
        "import build123d as bd\ndef build():\n    open('x', 'w')\n    return bd.Box(1, 1, 1)\n",
        "import build123d as bd\ndef build():\n    return bd.__dict__\n",
    ],
)
def test_validator_rejects_unsafe_code(script):
    with pytest.raises(SandboxError):
        validate_script(script)


def test_validator_accepts_build123d_primitive():
    validate_script("import build123d as bd\ndef build():\n    return bd.Box(1, 2, 3)\n")


@pytest.mark.parametrize("name", sorted(MATH_API))
def test_codegen_normalizes_all_math_names_off_the_build123d_namespace(name):
    from aura_cad.llm.codegen import normalize_build123d_math

    value = f"bd.{name}(1)" if name in MATH_FUNCTIONS else f"bd.{name}"
    script = (
        "import build123d as bd\n"
        "def build():\n"
        f"    return bd.Box({value}, 2, 3)\n"
    )
    with pytest.raises(SandboxError, match=f"Attribute is not allowed: {name}"):
        validate_script(script)
    normalized = normalize_build123d_math(script)
    assert f"math.{name}" in normalized
    validate_script(normalized)


@pytest.mark.parametrize(
    "script",
    [
        (
            "import build123d as b3d\n"
            "import math as m\n"
            "def build():\n"
            "    return b3d.Box(b3d.sin(m.pi / 2), 2, 3)\n"
        ),
        (
            "from math import sin as sine\n"
            "import build123d as bd\n"
            "def build():\n"
            "    return bd.Box(sine(1), 2, 3)\n"
        ),
        (
            "import build123d as bd\n"
            "def build():\n"
            "    return bd.Box(sin(1), 2, 3)\n"
        ),
    ],
)
def test_codegen_normalizes_math_aliases_and_missing_math_import(script):
    from aura_cad.llm.codegen import normalize_build123d_math

    normalized = normalize_build123d_math(script)
    assert "import math" in normalized
    assert "math.sin" in normalized
    validate_script(normalized)


@pytest.mark.parametrize(
    "script",
    [
        "import os\ndef build():\n    return 1\n",
        "import build123d as bd\ndef build():\n    return bd.exec(1)\n",
    ],
)
def test_math_normalization_does_not_allow_unsafe_code(script):
    from aura_cad.llm.codegen import normalize_build123d_math

    with pytest.raises(SandboxError):
        validate_script(normalize_build123d_math(script))


@pytest.mark.parametrize(
    "expression",
    ["part.add(bd.Box(1, 2, 3))", "bd.add(bd.Box(1, 2, 3))"],
)
def test_validator_rejects_add_without_builder_context(expression):
    with pytest.raises(SandboxError, match="Attribute is not allowed: add"):
        validate_script(
            "import build123d as bd\n"
            "def build():\n"
            "    part = bd.Part()\n"
            f"    {expression}\n"
            "    return part\n"
        )


def test_validator_accepts_local_variables_but_keeps_calls_allowlisted():
    validate_script(
        "import build123d as bd\n"
        "def build():\n"
        "    shape = bd.Cylinder(2, 1)\n"
        "    for angle in range(0, 360, 90):\n"
        "        shape += bd.Rot(0, 0, angle) * bd.Box(1, 1, 1)\n"
        "    return shape\n"
    )
    with pytest.raises(SandboxError, match="Call is not allowed"):
        validate_script(
            "def build():\n"
            "    return arbitrary_function()\n"
        )


def test_local_mode_uses_existing_local_llm_path(monkeypatch):
    from aura_cad.llm.adapter import CadLLMAdapter

    calls = []
    monkeypatch.setattr(
        "memory.config_manager.get_local_llm_config",
        lambda: {"mode": "local", "provider": "ollama"},
    )
    monkeypatch.setattr(
        "core.llm_client.call_llm_text",
        lambda prompt, timeout: calls.append((prompt, timeout)) or "local result",
    )
    adapter = CadLLMAdapter()
    assert adapter._generate_with_configured_provider("offline prompt") == "local result"
    assert calls == [("offline prompt", 180)]


def test_planner_recovers_from_empty_or_malformed_model_output():
    spec = CadSpec("gear", "A compact gear.", {"teeth": 24, "module_mm": 1.5})
    for reply in ("", "I could not produce JSON.", "```json\n{bad json}\n```"):
        planner = CadPlanner(CadLLMAdapter(lambda _task, _prompt, value=reply: value))
        parts = planner.plan(spec, "job-plan")
        assert len(parts) == 1
        assert parts[0].name == "gear"
        assert parts[0].dimensions == {"teeth": 24.0, "module_mm": 1.5}


def test_planner_extracts_json_from_fenced_prose_and_filters_invalid_numbers():
    response = (
        "Plan:\n```json\n"
        '{"parts":[{"name":"gear","description":"Gear body",'
        '"dimensions":{"teeth":24,"bad":NaN,"flag":true}}]}\n```'
    )
    planner = CadPlanner(CadLLMAdapter(lambda _task, _prompt: response))
    parts = planner.plan(CadSpec("gear", "gear"), "job-plan")
    assert len(parts) == 1
    assert parts[0].dimensions == {"teeth": 24.0}


def test_cad_question_panel_is_floating_and_click_submits_answer():
    from types import SimpleNamespace

    from PyQt6.QtWidgets import QApplication, QWidget
    import ui

    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    submitted = threading.Event()
    answers = []

    class Signal:
        @staticmethod
        def emit(_message):
            return None

    window = SimpleNamespace(
        centralWidget=lambda: parent,
        _hide_cad_question=lambda: window._cad_question_panel.hide(),
        _position_cad_question_panel=lambda: None,
        _log_sig=Signal(),
        _select_cad_question_option=lambda answer: (
            ui.MainWindow._select_cad_question_option(window, answer)
        ),
        on_text_command=lambda answer: (answers.append(answer), submitted.set()),
    )
    window._cad_question_panel = ui.MainWindow._build_cad_question_panel(window)
    ui.MainWindow._show_cad_question(
        window, "CAD DESIGN", "How many teeth?", ["12", "20", "32"]
    )

    assert not window._cad_question_panel.isHidden()
    assert window._cad_question_options_lay.count() == 3
    window._cad_question_options_lay.itemAt(1).widget().click()
    assert submitted.wait(1)
    assert answers == ["20"]
    parent.close()
    app.processEvents()


def test_cad_question_is_spoken_for_typed_clarification():
    from aura_cad.integration.hooks import CadHooks
    from aura_cad.models import CadResult, JobState

    shown = []
    spoken = []

    class Service:
        is_busy = False
        last_job_id = ""

        @staticmethod
        def should_handle(_text):
            return True

        @staticmethod
        def process(_text):
            return CadResult(
                JobState.CLARIFYING,
                "How many teeth?",
                details={"question": "How many teeth?", "options": ("12", "20", "32")},
            )

    class Player:
        @staticmethod
        def hide_quiz():
            return None

        @staticmethod
        def hide_cad_question():
            return None

        @staticmethod
        def show_cad_question(title, question, options):
            shown.append((title, question, options))

    hooks = CadHooks.__new__(CadHooks)
    hooks.service = Service()
    hooks.player = Player()

    assert hooks.handle_text("Make a gear", speak=spoken.append)
    assert shown == [("CAD DESIGN", "How many teeth?", ["12", "20", "32"])]
    assert "say first option" in spoken[0]


def test_runner_kills_timed_out_child(tmp_path):
    worker = tmp_path / "hang.py"
    worker.write_text("while True: pass\n", encoding="utf-8")
    runner = SandboxRunner(timeout_seconds=0.2, worker_path=worker)
    with pytest.raises(SandboxTimeout):
        runner.run("def build():\n    return 1\n", tmp_path / "out.stl")


def test_runner_caps_child_output(tmp_path):
    worker = tmp_path / "noisy.py"
    worker.write_text("print('x' * 10000)\n", encoding="utf-8")
    runner = SandboxRunner(timeout_seconds=3, output_limit=2048, worker_path=worker)
    with pytest.raises(SandboxError, match="output"):
        runner.run("def build():\n    return 1\n", tmp_path / "out.stl")


def test_mesh_checks_require_watertight_sane_geometry():
    class Mesh:
        faces = [(0, 1, 2)]
        bounds = ((0, 0, 0), (1, 1, 1))
        is_watertight = True

    check = validate_mesh(Mesh())
    assert check.triangle_count == 1
    Mesh.is_watertight = False
    with pytest.raises(Exception, match="watertight"):
        validate_mesh(Mesh())


def test_oversized_mesh_is_simplified_before_validation(monkeypatch):
    class Mesh:
        faces = [1, 2, 3]

        def simplify_quadric_decimation(self, face_count):
            assert face_count == 2
            return type("SimplifiedMesh", (), {"faces": [1, 2]})()

    monkeypatch.setattr(mesh_checks, "MAX_TRIANGLES", 2)
    simplified = mesh_checks.simplify_mesh_if_needed(Mesh())
    assert len(simplified.faces) == 2


def test_oversized_mesh_simplification_failure_is_reported(monkeypatch):
    class Mesh:
        faces = [1, 2, 3]

        def simplify_quadric_decimation(self, face_count):
            return type("SimplifiedMesh", (), {"faces": [1, 2, 3]})()

    monkeypatch.setattr(mesh_checks, "MAX_TRIANGLES", 2)
    with pytest.raises(ValidationError, match="still exceeds"):
        mesh_checks.simplify_mesh_if_needed(Mesh())


def test_mesh_checks_reject_non_finite_dimensions():
    class Mesh:
        faces = [(0, 1, 2)]
        bounds = ((0, 0, 0), (float("nan"), 1, 1))
        is_watertight = True

    with pytest.raises(ValidationError, match="dimensions"):
        validate_mesh(Mesh())


def test_spoken_cancel_interrupts_an_active_job():
    from aura_cad.integration.hooks import CadHooks

    cancelled = threading.Event()

    class Service:
        is_busy = True

        def cancel(self):
            cancelled.set()

    hooks = CadHooks.__new__(CadHooks)
    hooks.service = Service()
    assert hooks.handle_voice_text("cancel the CAD job")
    assert cancelled.wait(1)


def test_cad_progress_reports_monotonic_estimated_stages():
    from aura_cad.integration.hooks import CadHooks

    hooks = CadHooks.__new__(CadHooks)
    hooks._progress_lock = threading.Lock()
    hooks._progress_values = {}

    planning = hooks._estimate_progress("job", "Planning model parts…")
    geometry = hooks._estimate_progress("job", "Generating model geometry…")
    earlier = hooks._estimate_progress("job", "Writing the CAD script…")
    complete = hooks._estimate_progress("job", "Model complete.")

    assert 0 < planning < geometry < complete
    assert earlier == geometry
    assert complete == 100


def test_cad_progress_panel_shows_estimate_and_hides_on_finish():
    from types import SimpleNamespace

    from PyQt6.QtWidgets import QApplication, QWidget
    import ui

    app = QApplication.instance() or QApplication([])
    parent = QWidget()

    class Signal:
        @staticmethod
        def emit():
            return None

    class Timer:
        def __init__(self):
            self.running = False

        def start(self):
            self.running = True

        def stop(self):
            self.running = False

    window = SimpleNamespace(
        centralWidget=lambda: parent,
        _cad_cancel_sig=Signal(),
        _cad_progress_timer=Timer(),
        _position_cad_progress_panel=lambda: None,
    )
    window._cad_progress_panel = ui.MainWindow._build_cad_progress_panel(window)
    window._cad_progress_job = ""
    window._cad_progress_stage = ""

    ui.MainWindow._show_cad_progress(
        window, "job-1", 48, "Generating model geometry…", True
    )
    assert not window._cad_progress_panel.isHidden()
    assert window._cad_progress_bar.value() == 48
    assert window._cad_progress_percent.text() == "EST. 48%"

    ui.MainWindow._show_cad_progress(window, "job-1", 0, "", False)
    assert window._cad_progress_panel.isHidden()
    assert not window._cad_progress_timer.running
    parent.close()
    app.processEvents()

def test_project_store_versions_and_restore(tmp_path):
    store = ProjectStore(tmp_path)
    spec = CadSpec("gear", "small gear", {"teeth": 20})
    project = store.create_project(spec)
    outputs = {}
    for extension in ("stl", "obj", "glb"):
        source = tmp_path / f"source.{extension}"
        source.write_bytes(extension.encode("ascii"))
        outputs[extension] = source
    first = store.commit_version(project, "def build(): return None", spec, outputs)
    second = store.commit_version(project, "def build(): return None", spec, outputs)
    assert first.version == 1
    assert second.version == 2
    assert store.previous_version(project).version == 1
    assert store.set_active_version(project, 1).version == 1


def test_end_to_end_mocked_gear_script_when_backend_is_installed(tmp_path):
    try:
        ensure_backend_available()
    except Exception as exc:
        pytest.skip(str(exc))

    from aura_cad.service import CadService

    replies = iter([
        json.dumps({"parts": [{"name": "gear", "description": "Toothed gear", "dimensions": {}}]}),
        (
            "import build123d as bd\n"
            "def build():\n"
            "    part = bd.Part()\n"
            "    part.add(bd.Box(2, 2, 2))\n"
            "    return part\n"
        ),
        (
            "import build123d as bd\n"
            "def build():\n"
            "    shape = bd.Cylinder(20, 4)\n"
            "    for angle in range(0, 360, 18):\n"
            "        shape += bd.Rot(0, 0, angle) * bd.Pos(19, 0, 0) * bd.Box(5, 2, 4)\n"
            "    return shape\n"
        ),
    ])
    llm = CadLLMAdapter(lambda _task, _prompt: next(replies))
    service = CadService(llm=llm, store=ProjectStore(tmp_path))
    assert service.process("Make a gear with 20 teeth").state.value == "clarifying"
    assert service.process("module 2").state.value == "confirming"
    result = service.process("yes, build it")
    assert result.state.value == "complete"
    assert all(Path(result.details[key]).is_file() for key in ("stl", "obj", "glb"))


@pytest.mark.parametrize("layout", ["Inline", "Flat", "V-type"])
def test_engine_build_uses_no_llm_and_passes_mesh_checks(tmp_path, layout):
    try:
        ensure_backend_available()
    except Exception as exc:
        pytest.skip(str(exc))

    from aura_cad.service import CadService

    llm_calls = []
    llm = CadLLMAdapter(
        lambda task, prompt: llm_calls.append((task, prompt))
    )
    service = CadService(llm=llm, store=ProjectStore(tmp_path))
    spec = CadSpec(
        "engine",
        "A four-cylinder engine.",
        {"layout": layout, "cylinders": 4, "detail": "Internals"},
    )

    result = service._build(spec)

    assert result.state.value == "complete"
    assert result.details["watertight"] is True
    assert all(Path(result.details[key]).is_file() for key in ("stl", "obj", "glb"))
    assert llm_calls == []


def test_failed_deterministic_engine_does_not_spend_quota_on_llm_repairs(tmp_path):
    from aura_cad.service import CadService

    class FailingBackend:
        def run_script(self, script, mesh_path, job_id=""):
            raise SandboxError("forced engine failure")

    llm_calls = []
    service = CadService(
        llm=CadLLMAdapter(
            lambda task, prompt: llm_calls.append((task, prompt))
        ),
        store=ProjectStore(tmp_path),
        backend=FailingBackend(),
    )
    spec = CadSpec(
        "engine",
        "A four-cylinder engine.",
        {"layout": "Inline", "cylinders": 4, "detail": "Internals"},
    )

    with pytest.raises(ValidationError, match="deterministic engine"):
        service._build(spec)

    assert llm_calls == []
