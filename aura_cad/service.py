"""Coordinated CAD workflow facade used by the desktop integration."""

from __future__ import annotations

import logging
import re
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from aura_cad.backends import CadBackend
from aura_cad.cache import PartCache
from aura_cad.cache.models import CacheAction, PartResolution
from aura_cad.engine_template import engine_script
from aura_cad.errors import BackendUnavailable, CadError, CodegenError, ValidationError
from aura_cad.export.checks import simplify_mesh_if_needed, validate_mesh
from aura_cad.export.exporter import export_meshes
from aura_cad.llm.adapter import CadLLMAdapter
from aura_cad.llm.codegen import CadCodeGenerator
from aura_cad.llm.planner import CadPlanner
from aura_cad.llm.repair import CadRepair
from aura_cad.models import CadResult, CadSpec, JobState
from aura_cad.sandbox.runner import SandboxRunner, ensure_backend_available
from aura_cad.spec.clarifier import CadClarifier, ClarificationStep, StepKind
from aura_cad.spec.intent import detect_intent
from aura_cad.strings import cache_summary
from aura_cad.store.project_store import ProjectStore

LOGGER = logging.getLogger("aura.cad")
_REFINEMENT = re.compile(
    r"\b(?:make|add|remove|change|refine|modify|increase|reduce|thicken|"
    r"thin|enlarge|shrink)\b",
    re.I,
)
_NEW_DESIGN = re.compile(
    r"\b(?:sketch|design|model|make|build|create|draw|generate)\s+"
    r"(?:me\s+)?(?:a|an|another|new|different|second|one more)\b",
    re.I,
)
_UNDO = re.compile(r"\bundo(?:\s+that)?(?:\s+change)?\b", re.I)


class _Build123dBackend:
    """Primary free, CPU-only backend; keep engine-specific work behind this seam."""

    def __init__(self, runner: SandboxRunner) -> None:
        self._runner = runner

    def run_script(self, script: str, mesh_path: Path, job_id: str = "") -> str:
        return self._runner.run(script, mesh_path, job_id)

    def cancel(self) -> None:
        self._runner.cancel()


class CadService:
    """Public API for clarification, generation, validation, and versioning."""

    def __init__(
        self,
        llm: CadLLMAdapter | None = None,
        store: ProjectStore | None = None,
        backend: CadBackend | None = None,
        runner: SandboxRunner | None = None,
    ) -> None:
        self._llm = llm or CadLLMAdapter()
        self._planner = CadPlanner(self._llm)
        self._codegen = CadCodeGenerator(self._llm)
        self._repair = CadRepair(self._llm)
        self._store = store or ProjectStore()
        self._part_cache = PartCache()
        self._runner = runner or SandboxRunner()
        self._backend = backend or _Build123dBackend(self._runner)
        self._clarifier = CadClarifier()
        self._lock = threading.RLock()
        self._cancelled = threading.Event()
        self._busy = threading.Event()
        self._project_id = ""
        self._spec: CadSpec | None = None
        self._script = ""
        self._active_resolutions: list[PartResolution] = []
        self._active_plans = []
        self._last_job_id = ""
        self.on_progress = None

    @property
    def has_pending_dialogue(self) -> bool:
        return self._clarifier.active

    @property
    def is_busy(self) -> bool:
        return self._busy.is_set()

    @property
    def has_project(self) -> bool:
        return bool(self._project_id and self._script)

    @property
    def last_job_id(self) -> str:
        return self._last_job_id

    @property
    def part_cache(self) -> PartCache:
        return self._part_cache

    def should_handle(self, text: str) -> bool:
        if self._busy.is_set() and re.search(r"\b(?:cancel|stop cad)\b", text or "", re.I):
            return True
        if self.has_pending_dialogue or detect_intent(text):
            return True
        if _UNDO.search(text or "") and self.has_project:
            return True
        return bool(self.has_project and _REFINEMENT.search(text or ""))

    def cancel(self) -> None:
        self._cancelled.set()
        cancel = getattr(self._backend, "cancel", None)
        if callable(cancel):
            cancel()

    def process(self, text: str) -> CadResult | None:
        """Advance one text/voice turn; heavy stages remain in the caller's worker."""
        request = " ".join(str(text or "").split())
        if self._busy.is_set():
            if re.search(r"\b(?:cancel|stop cad)\b", request, re.I):
                self.cancel()
                return CadResult(JobState.CANCELLED, "CAD job cancellation requested.")
            if detect_intent(request) or _REFINEMENT.search(request):
                return CadResult(
                    JobState.BUILDING,
                    "A CAD job is already running. Say cancel to stop it.",
                )
            return None
        refinement = None
        with self._lock:
            if self._clarifier.active:
                step = self._clarifier.answer(self._quiz_answer(request))
            elif _UNDO.search(request) and self.has_project:
                return self._undo_result()
            elif detect_intent(request) and _NEW_DESIGN.search(request):
                step = self._clarifier.start(request)
            elif self.has_project and _REFINEMENT.search(request):
                refinement = (self._spec, self._script, request)
                step = ClarificationStep(StepKind.NONE)
            else:
                step = self._clarifier.start(request)
        if refinement is not None:
            return self._refine(*refinement)
        if step.kind == StepKind.NONE:
            return None
        if step.kind == StepKind.QUESTION:
            choices = ", ".join(step.options) if step.options else "You can answer in your own words."
            return CadResult(
                JobState.CLARIFYING,
                f"{step.question} Options: {choices}",
                details={"question": step.question, "options": step.options},
            )
        if step.kind == StepKind.CONFIRM:
            assumptions = step.spec.assumptions if step.spec else []
            summary = step.message
            if assumptions:
                summary += " Assumptions: " + "; ".join(assumptions) + "."
            return CadResult(
                JobState.CONFIRMING,
                summary,
                details={"question": summary, "options": ("Build it", "Change details", "Cancel")},
            )
        if step.kind == StepKind.CANCEL:
            self._cancelled.set()
            return CadResult(JobState.CANCELLED, step.message)
        if step.kind == StepKind.BUILD and step.spec is not None:
            return self._build(step.spec)
        return None

    @staticmethod
    def _quiz_answer(text: str) -> str:
        if "[QUIZ_DONE]" not in text:
            return text
        answers = re.findall(r"they answered:\s*(.*?)\s*\|\s*expected:", text, re.I)
        return " ".join(answer.strip() for answer in answers if answer.strip())

    def _build(self, spec: CadSpec, script: str | None = None) -> CadResult:
        job_id = uuid4().hex[:12]
        self._last_job_id = job_id
        self._busy.set()
        self._cancelled.clear()
        try:
            resolutions = None
            self._progress(job_id, "Checking CAD support…")
            if isinstance(self._backend, _Build123dBackend):
                ensure_backend_available()
            deterministic_engine = script is None and spec.object_type == "engine"
            if script is None:
                if deterministic_engine:
                    self._progress(job_id, "Preparing engine geometry…")
                    script = engine_script(spec)
                else:
                    self._progress(job_id, "Planning model parts…")
                    cache_enabled = self._part_cache.is_enabled(job_id)
                    if cache_enabled:
                        resolutions = self._planner.plan_with_cache(
                            spec, job_id, self._part_cache
                        )
                    else:
                        plan = self._planner.plan(spec, job_id)
                    if self._cancelled.is_set():
                        raise CadError("CAD job cancelled.")
                    self._progress(job_id, "Writing the CAD script…")
                    if cache_enabled:
                        script = self._codegen.generate_for_parts(
                            spec, resolutions, job_id
                        )
                    else:
                        script = self._codegen.generate(spec, plan, job_id)
            if self._cancelled.is_set():
                raise CadError("CAD job cancelled.")
            if script is None or not self._project_id:
                self._project_id = self._store.create_project(spec)
            self._progress(job_id, "Generating model geometry…")
            return self._execute_versions(
                job_id,
                spec,
                script,
                resolutions=resolutions,
                allow_repair=not deterministic_engine,
            )
        except CadError:
            LOGGER.exception("[job=%s] CAD job failed", job_id)
            raise
        except Exception as exc:
            LOGGER.exception("[job=%s] Unexpected CAD job failure", job_id)
            raise CodegenError("The CAD model could not be completed.") from exc
        finally:
            self._busy.clear()

    def _execute_versions(
        self,
        job_id: str,
        spec: CadSpec,
        script: str,
        resolutions: list[PartResolution] | None = None,
        allow_repair: bool = True,
    ) -> CadResult:
        from aura_cad.config import MAX_REPAIR_ATTEMPTS

        failure: Exception | None = None
        repairs_used = 0
        with tempfile.TemporaryDirectory(prefix="aura-cad-export-") as directory:
            temporary = Path(directory)
            stl_path = temporary / "model.stl"
            for attempt in range(MAX_REPAIR_ATTEMPTS + 1):
                if self._cancelled.is_set():
                    raise CadError("CAD job cancelled.")
                try:
                    self._backend.run_script(script, stl_path, job_id)
                    self._progress(job_id, "Geometry generated; loading the mesh…")
                    import trimesh

                    mesh = trimesh.load(str(stl_path), force="mesh", process=True)
                    mesh = simplify_mesh_if_needed(mesh)
                    self._progress(job_id, "Checking dimensions and mesh integrity…")
                    checks = validate_mesh(mesh)
                    self._progress(job_id, "Preparing STL, OBJ, and GLB exports…")
                    exports = export_meshes(stl_path, temporary / "exports", mesh)
                    self._progress(job_id, "Saving the validated model version…")
                    version = self._store.commit_version(
                        self._project_id, script, spec, exports
                    )
                    self._spec, self._script = spec, script
                    LOGGER.info(
                        "[job=%s] Model passed checks: %s triangles",
                        job_id,
                        checks.triangle_count,
                    )
                    result = CadResult(
                        JobState.COMPLETE,
                        f"Model built and checked ({checks.triangle_count} triangles); "
                        f"saved as version {version.version}.",
                        project_id=version.project_id,
                        version=version.version,
                        details={
                            "stl": version.stl_path,
                            "obj": version.obj_path,
                            "glb": version.glb_path,
                            "watertight": checks.watertight,
                            "job_id": job_id,
                        },
                    )
                    if resolutions is not None:
                        result = self._record_cache_success(
                            result, resolutions, script, job_id, repairs_used
                        )
                    self._progress(job_id, "Model complete.")
                    return result
                except (CadError, OSError, ValueError) as exc:
                    failure = exc
                    if self._cancelled.is_set():
                        raise CadError("CAD job cancelled.") from exc
                    if not allow_repair:
                        raise ValidationError(
                            "The deterministic engine model failed validation."
                        ) from exc
                    if attempt >= MAX_REPAIR_ATTEMPTS:
                        raise ValidationError(
                            f"Model validation failed after {MAX_REPAIR_ATTEMPTS} repair attempts."
                        ) from exc
                    self._progress(job_id, f"Repairing model (attempt {attempt + 1})…")
                    script = self._repair.repair(spec, script, str(exc), job_id)
                    repairs_used += 1
        raise ValidationError("The CAD model could not be validated.") from failure

    def _record_cache_success(
        self,
        result: CadResult,
        resolutions: list[PartResolution],
        script: str,
        job_id: str,
        repairs_used: int,
    ) -> CadResult:
        used = [
            item for item in resolutions
            if self._part_cache.function_is_used(script, item.function_name)
        ]
        reused = sum(
            item.part is not None and item.action != CacheAction.GENERATE
            for item in used
        )
        generated = sum(item.action == CacheAction.GENERATE for item in used)
        self._part_cache.store_success(
            used, script, job_id, repairs_used=repairs_used
        )
        self._active_plans = [
            replace(item.plan, dimensions=dict(item.parameters))
            for item in used
        ]
        try:
            self._active_resolutions = self._part_cache.resolve_parts(
                self._active_plans, job_id
            )
        except Exception:
            LOGGER.exception("[job=%s] Could not refresh active CAD cache parts", job_id)
            self._active_resolutions = used
        prefix = cache_summary(reused, generated)
        return replace(result, message=f"{prefix} {result.message}".strip()) if prefix else result

    def _refine(self, spec: CadSpec | None, script: str, request: str) -> CadResult:
        if spec is None:
            raise CadError("There is no active CAD specification to refine.")
        job_id = uuid4().hex[:12]
        self._last_job_id = job_id
        self._busy.set()
        self._cancelled.clear()
        try:
            self._progress(job_id, "Updating the model…")
            adapted = self._part_cache.adapt_refinement(
                self._active_resolutions, request, job_id
            )
            if adapted is not None:
                adapted_script = self._codegen.apply_parameter_values(script, adapted)
                if adapted_script is not None:
                    return self._execute_versions(
                        job_id,
                        spec,
                        adapted_script,
                        resolutions=adapted,
                    )
            refined = self._codegen.refine(spec, script, request, job_id)
            if self._cancelled.is_set():
                raise CadError("CAD job cancelled.")
            result = self._execute_versions(job_id, spec, refined)
            if result.state == JobState.COMPLETE:
                self._active_resolutions = []
                self._active_plans = []
            return result
        except CadError:
            LOGGER.exception("[job=%s] CAD refinement failed", job_id)
            raise
        except Exception as exc:
            LOGGER.exception("[job=%s] Unexpected CAD refinement failure", job_id)
            raise CodegenError("The CAD model could not be refined.") from exc
        finally:
            self._busy.clear()

    def _undo_result(self) -> CadResult:
        previous = self._store.previous_version(self._project_id)
        if previous is None:
            return CadResult(JobState.FAILED, "There is no earlier CAD version to restore.")
        restored = self._store.set_active_version(self._project_id, previous.version)
        self._script = Path(restored.script_path).read_text(encoding="utf-8")
        return CadResult(
            JobState.COMPLETE,
            f"Restored CAD model version {restored.version}.",
            project_id=restored.project_id,
            version=restored.version,
            details={"stl": restored.stl_path, "obj": restored.obj_path, "glb": restored.glb_path},
        )

    def _progress(self, job_id: str, message: str) -> None:
        LOGGER.info("[job=%s] %s", job_id, message)
        callback = self.on_progress
        if callable(callback):
            try:
                callback(job_id, message)
            except Exception:
                LOGGER.exception("[job=%s] Progress callback failed", job_id)
