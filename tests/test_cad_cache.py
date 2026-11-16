from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from aura_cad.cache import PartCache
from aura_cad.cache.matcher import PartMatcher
from aura_cad.cache.models import (
    CacheAction,
    CachedPart,
    ParameterSpec,
)
from aura_cad.cache.parametrizer import PartParametrizer
from aura_cad.cache.policy import PartCachePolicy
from aura_cad.cache.signature import normalized_plan, plan_signature
from aura_cad.cache.storage import PartCacheStorage
from aura_cad.models import CadSpec, PartPlan


def _cached_part(name: str = "gear", source: str | None = None) -> CachedPart:
    plan = PartPlan(name, "toothed gear", {"teeth": 24, "module_mm": 2.0})
    signature = plan_signature(plan)
    code = source or (
        "def make_gear(teeth: int, module_mm: float):\n"
        '    """Parametric gear."""\n'
        "    return bd.Cylinder(teeth * module_mm / 2, 4)\n"
    )
    return CachedPart(
        id=hashlib.sha256(name.encode()).hexdigest()[:32],
        name=name,
        object_family=signature.object_family,
        tags=signature.tags,
        signature=signature.value,
        param_schema=(
            ParameterSpec("teeth", "int", 24, 6, 120),
            ParameterSpec("module_mm", "float", 2.0, 0.1, 100000.0, "mm"),
        ),
        source_code=code,
        docstring="Parametric gear part.",
        backend_name="build123d",
        backend_version="test-backend",
        prompt_version="test-prompt",
        code_sha256=hashlib.sha256(code.encode()).hexdigest(),
        created_at="2026-01-01T00:00:00+00:00",
        last_used_at="2026-01-01T00:00:00+00:00",
        validated=True,
        function_name=f"make_{name}",
    )


def test_signature_is_stable_and_normalizes_explicit_length_units():
    plan = PartPlan(
        "Spur Gear",
        "A toothed part",
        {"diameter_cm": 2.5, "teeth": 24},
    )
    normalized = normalized_plan(plan)
    assert normalized.dimensions == {"diameter_mm": 25.0, "teeth": 24.0}
    equivalent = PartPlan(
        "Spur Gear",
        "A toothed part",
        {"diameter_mm": 25, "teeth": 24},
    )
    assert plan_signature(plan) == plan_signature(equivalent)


def test_matcher_prefers_exact_then_uses_thresholded_feature_similarity():
    part = _cached_part()
    exact = PartMatcher(0.99).rank(plan_signature(PartPlan(
        "gear", "toothed gear", {"teeth": 30, "module_mm": 2.0}
    )), [part])
    assert exact[0].exact

    similar = PartPlan("gear", "toothed wheel", {"teeth": 30, "module_mm": 2.0})
    assert PartMatcher(0.5).rank(plan_signature(similar), [part])
    unrelated = PartPlan("building", "multi storey tower", {"storeys": 4})
    assert not PartMatcher(0.99).rank(plan_signature(unrelated), [part])


def test_parametrizer_adapts_values_and_rejects_structural_changes():
    part = _cached_part()
    parametrizer = PartParametrizer()
    adapted = parametrizer.adapt(part, {"teeth": 30, "module_mm": 2.0})
    assert adapted.status == "adapt"
    assert adapted.parameters["teeth"] == 30
    assert parametrizer.adapt(part, {"teeth": 200}).status == "needs_regeneration"
    bigger = parametrizer.adapt_refinement(
        part, {"teeth": 24, "module_mm": 2.0}, "make the gear bigger"
    )
    assert bigger.status == "adapt"
    assert bigger.parameters["module_mm"] == pytest.approx(2.2)


def test_storage_writes_atomically_and_quarantines_integrity_failures(tmp_path):
    part = _cached_part()
    storage = PartCacheStorage(tmp_path / "cache")
    storage.save(part)
    assert storage.get(part.id) == part
    assert not list((tmp_path / "cache").rglob("*.tmp"))

    path = storage.entries_dir / f"{part.id}.json"
    wrapper = json.loads(path.read_text(encoding="utf-8"))
    wrapper["part"]["source_code"] += "\n# tampered"
    path.write_text(json.dumps(wrapper), encoding="utf-8")
    reloaded = PartCacheStorage(tmp_path / "cache")
    assert reloaded.list_parts() == []
    assert list(reloaded.quarantine_dir.glob("*.json"))


def test_storage_evicts_least_recently_used_and_handles_schema_bumps(tmp_path, monkeypatch):
    storage = PartCacheStorage(tmp_path / "cache", max_entries=1)
    first = _cached_part("gear")
    second = replace(
        _cached_part("wheel"),
        id="f" * 32,
        last_used_at="2026-02-01T00:00:00+00:00",
    )
    storage.save(first)
    storage.save(second)
    assert [part.id for part in storage.list_parts()] == [second.id]

    storage.save(first)
    monkeypatch.setattr("aura_cad.cache.storage.PART_CACHE_SCHEMA_VERSION", 2)
    bumped = PartCacheStorage(tmp_path / "cache")
    assert bumped.list_parts() == []
    assert list(bumped.quarantine_dir.glob("*.json"))


def test_storage_serializes_concurrent_writes(tmp_path):
    storage = PartCacheStorage(tmp_path / "cache")
    parts = [
        replace(_cached_part(f"gear{index}"), id=f"{index + 1:032x}")
        for index in range(8)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(storage.save, parts))
    reloaded = PartCacheStorage(tmp_path / "cache")
    assert len(reloaded.list_parts()) == 8


def test_storage_clear_removes_entries_and_quarantine_but_keeps_setting(tmp_path):
    storage = PartCacheStorage(tmp_path / "cache")
    part = _cached_part()
    storage.save(part)
    storage.set_enabled(False)
    storage.quarantine(part.id, "test corruption")

    stats = storage.stats()
    assert stats.entry_count == 0
    storage.clear()
    assert not list(storage.entries_dir.glob("*.json"))
    assert not list(storage.quarantine_dir.glob("*.json"))
    assert storage.enabled() is False


def test_policy_rejects_unvalidated_warning_and_incompatible_parts(monkeypatch):
    assert not PartCachePolicy.should_store(False, (), 0)
    assert not PartCachePolicy.should_store(True, ("warning",), 0)
    assert not PartCachePolicy.should_store(True, (), 3)

    part = _cached_part()
    policy = PartCachePolicy("build123d", "new-backend", "new-prompt")
    assert not policy.compatible(part)
    monkeypatch.setattr(
        "aura_cad.cache.policy.PART_CACHE_VERSION_COMPATIBILITY",
        {"backend": {"new-backend": {"test-backend"}}, "prompt": {"new-prompt": {"test-prompt"}}},
    )
    assert policy.compatible(part)


def test_incompatible_entry_is_quarantined_and_becomes_a_miss(tmp_path):
    part = _cached_part()
    storage = PartCacheStorage(tmp_path / "cache")
    storage.save(part)
    cache = PartCache(
        storage=storage,
        policy=PartCachePolicy("build123d", "new-backend", "test-prompt"),
    )

    decision = cache.resolve_parts(
        [PartPlan("gear", "toothed gear", {"teeth": 30, "module_mm": 2.0})],
        "job-version",
    )[0]

    assert decision.action == CacheAction.GENERATE
    assert not list(storage.entries_dir.glob("*.json"))
    assert list(storage.quarantine_dir.glob("*.json"))


def test_cache_revalidates_saved_code_and_quarantines_unsafe_source(tmp_path):
    part = _cached_part(source=(
        "def make_gear(teeth: int, module_mm: float):\n"
        "    open('private.txt', 'w')\n"
        "    return bd.Cylinder(1, 1)\n"
    ))
    storage = PartCacheStorage(tmp_path / "cache")
    storage.save(part)
    cache = PartCache(storage=storage, policy=PartCachePolicy(
        "build123d", "test-backend", "test-prompt"
    ))
    decisions = cache.resolve_parts(
        [PartPlan("gear", "toothed gear", {"teeth": 30, "module_mm": 2.0})],
        "job-cache",
    )
    assert decisions[0].action == CacheAction.GENERATE
    assert list(storage.quarantine_dir.glob("*.json"))


def test_corrupt_schema_isolated_without_affecting_other_entries(tmp_path):
    storage = PartCacheStorage(tmp_path / "cache")
    good = _cached_part("gear")
    bad = replace(_cached_part("wheel"), id="f" * 32)
    storage.save(good)
    storage.save(bad)
    bad_path = storage.entries_dir / f"{bad.id}.json"
    bad_path.write_text("{", encoding="utf-8")
    reloaded = PartCacheStorage(tmp_path / "cache")
    assert [part.id for part in reloaded.list_parts()] == [good.id]
    assert list(reloaded.quarantine_dir.glob("*.json"))


def test_cache_disabled_returns_generate_without_reusing(tmp_path):
    cache = PartCache(storage=PartCacheStorage(tmp_path / "cache"))
    cache.set_enabled(False)
    decision = cache.resolve_parts([PartPlan("gear", "gear", {})], "job-cache")[0]
    assert decision.action == CacheAction.GENERATE


def test_cache_settings_are_worker_backed_and_clear_uses_confirmation(
    tmp_path, monkeypatch
):
    from aura_cad.integration.hooks import CadHooks

    cache = PartCache(storage=PartCacheStorage(tmp_path / "cache"))
    hooks = CadHooks.__new__(CadHooks)
    hooks.player = None
    hooks.service = type("Service", (), {"part_cache": cache})()
    section = hooks.part_cache_settings_section()
    assert section["namespace"] == "cad_part_cache"
    assert {field["type"] for field in section["fields"]} == {
        "toggle", "readonly"
    }

    persisted = threading.Event()
    set_enabled = cache.set_enabled

    def record_enabled(enabled):
        set_enabled(enabled)
        persisted.set()

    monkeypatch.setattr(cache, "set_enabled", record_enabled)
    section["save"]({"enabled": False})
    assert persisted.wait(2)
    assert cache.storage.enabled() is False

    cache.storage.save(_cached_part())
    queued = {}

    def request(_key, _title, _detail, run):
        queued["run"] = run
        return "confirmation pending"

    monkeypatch.setattr("core.confirm.pending_title", lambda: "")
    monkeypatch.setattr("core.confirm.request", request)
    accepted, _message = hooks._clear_cache_action({})
    assert accepted
    assert queued["run"]() == "CAD part cache cleared."
    assert cache.storage.list_parts() == []


def test_end_to_end_second_gear_uses_cache_without_an_llm_call(tmp_path):
    from aura_cad.llm.adapter import CadLLMAdapter
    from aura_cad.sandbox.runner import ensure_backend_available
    from aura_cad.service import CadService
    from aura_cad.store.project_store import ProjectStore

    try:
        ensure_backend_available()
    except Exception as exc:
        pytest.skip(str(exc))

    replies = iter(
        [
            json.dumps(
                {
                    "parts": [
                        {
                            "name": "gear",
                            "description": "gear",
                            "dimensions": {"teeth": 24, "module_mm": 2},
                        }
                    ]
                }
            ),
            (
                "import build123d as bd\n"
                "def make_gear(teeth: int, module_mm: float):\n"
                '    \"\"\"Parametric gear.\"\"\"\n'
                "    radius = teeth * module_mm / 2\n"
                "    return bd.Cylinder(radius, 4)\n"
                "def build():\n"
                "    return make_gear(teeth=24, module_mm=2.0)\n"
            ),
        ]
    )
    prompts = []

    def generate(_task, prompt):
        prompts.append(prompt)
        return next(replies)

    service = CadService(
        llm=CadLLMAdapter(generate),
        store=ProjectStore(tmp_path / "projects"),
    )
    service._part_cache = PartCache(
        storage=PartCacheStorage(tmp_path / "cache")
    )
    assert service.process(
        "Make a gear with 24 teeth and module 2"
    ).state.value == "confirming"
    first = service.process("yes, build it")
    assert first.state.value == "complete"
    assert len(prompts) == 2

    assert service.process(
        "Make another gear with 30 teeth and module 2"
    ).state.value == "confirming"
    second = service.process("yes, build it")
    assert second.state.value == "complete"
    assert len(prompts) == 2
    assert second.message.startswith("Reused 1 saved part")
    assert all(
        (tmp_path / "projects" / second.project_id / "versions" / "v2" / f"model.{ext}").is_file()
        for ext in ("stl", "obj", "glb")
    )

    third = service.process("Make the gear bigger")
    assert third.state.value == "complete"
    assert len(prompts) == 2
    assert third.message.startswith("Reused 1 saved part")
    assert service._active_resolutions[0].parameters["module_mm"] == pytest.approx(2.2)

    fourth = service.process("Make the gear bigger")
    assert fourth.state.value == "complete"
    assert len(prompts) == 2
    assert service._active_resolutions[0].parameters["module_mm"] == pytest.approx(2.42)
