import unittest
import json
from datetime import date
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from model_router import (
    DEEP_TASK_TYPES,
    TIERS,
    InvalidApiKeyError,
    ModelRouter,
    RetryLaterError,
    TaskSession,
    TokenBucket,
    classify_task,
)


class _ApiError(RuntimeError):
    def __init__(self, status_code, message="request failed"):
        super().__init__(message)
        self.status_code = status_code


class _Models:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self.handler(kwargs["model"], kwargs)

    def generate_content_stream(self, **kwargs):
        self.calls.append(kwargs)
        return self.handler(kwargs["model"], kwargs)


class _Client:
    def __init__(self, handler):
        self.models = _Models(handler)


class ModelRouterTests(unittest.TestCase):
    def _router(self, handler, *, available=None, settings=None, **kwargs):
        clients = []

        def client_factory(_key, _timeout):
            client = _Client(handler)
            clients.append(client)
            return client

        router = ModelRouter(
            key_provider=lambda: "test-key",
            client_factory=client_factory,
            model_lister=kwargs.pop(
                "model_lister",
                lambda _key: (
                    set().union(*TIERS.values())
                    if available is None else set(available)
                ),
            ),
            settings_provider=lambda: settings or {"auto_model_switching": True},
            legacy_fallback=kwargs.pop(
                "legacy_fallback", lambda *_args: None
            ),
            budget_file=kwargs.pop("budget_file", "unused-budget.json"),
            requests_per_minute=1000,
            sleeper=kwargs.pop("sleeper", lambda _seconds: None),
            **kwargs,
        )
        return router, clients

    def _generate(self, router, *args, **kwargs):
        result = []
        errors = []

        def run():
            try:
                result.append(router.generate(*args, **kwargs))
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=run)
        worker.start()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive(), "router call did not finish")
        if errors:
            raise errors[0]
        return result[0]

    def test_classifies_explicit_and_conservative_fallback_tasks(self):
        self.assertEqual(classify_task("chat", "write a web app"), "fast")
        self.assertEqual(classify_task("reminder", "create a CAD design"), "fast")
        self.assertEqual(classify_task("vibe_coding", "small question"), "deep")
        self.assertTrue(DEEP_TASK_TYPES.issuperset({
            "cad_design", "vibe_coding", "multi_file_refactor",
            "long_agent_plan", "long_document_generation",
        }))
        self.assertEqual(classify_task(None, "hello"), "fast")
        self.assertEqual(classify_task(None, "I have a long request " * 80), "smart")
        self.assertEqual(classify_task(None, "Could you build a full app?"), "deep")

    def test_lite_models_never_fall_back_to_deep_models(self):
        for tier in ("fast", "smart"):
            self.assertNotIn("gemini-3.8-flash", TIERS[tier])
            self.assertNotIn("gemini-3.6-flash", TIERS[tier])

    def test_chat_qa_and_reminders_never_touch_deep_only_models(self):
        for task_type in ("chat", "qa", "reminder"):
            attempts = []
            router, _clients = self._router(
                lambda model, _kwargs: (
                    attempts.append(model) or SimpleNamespace(text="answer")
                )
            )

            self._generate(router, task_type, "Hi")

            self.assertTrue(attempts)
            self.assertNotIn("gemini-3.8-flash", attempts)
            self.assertNotIn("gemini-3.6-flash", attempts)

    def test_fast_task_uses_fast_chain_and_429_falls_through(self):
        attempts = []

        def handler(model, _kwargs):
            attempts.append(model)
            if model == TIERS["fast"][0]:
                raise _ApiError(429, "quota")
            return SimpleNamespace(text="answer")

        router, _clients = self._router(handler)
        response, metadata = self._generate(router, "chat", "Hi")

        self.assertEqual(response.text, "answer")
        self.assertEqual(attempts, list(TIERS["fast"][:2]))
        self.assertEqual(metadata.model_used, TIERS["fast"][1])
        self.assertEqual(metadata.reason, "rate_limit")

    def test_deep_task_starts_on_38_when_available(self):
        router, clients = self._router(
            lambda _model, _kwargs: SimpleNamespace(text="done")
        )

        self._generate(router, "cad_design", "Design the part")

        self.assertEqual(
            clients[0].models.calls[0]["model"], "gemini-3.8-flash"
        )

    def test_api_model_discovery_reads_generation_methods(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "models": [
                {
                    "name": "models/gemini-3.8-flash",
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/embedding-only",
                    "supportedGenerationMethods": ["embedContent"],
                },
            ]
        }).encode("utf-8")
        with patch("urllib.request.urlopen", return_value=response):
            models = ModelRouter._list_models("test-key")
        self.assertEqual(models, {"gemini-3.8-flash"})

    def test_missing_deep_models_use_remaining_deep_chain(self):
        available = set().union(*TIERS.values()) - {
            "gemini-3.8-flash", "gemini-3.6-flash"
        }
        router, clients = self._router(
            lambda _model, _kwargs: SimpleNamespace(text="done"),
            available=available,
        )

        self._generate(router, "vibe_coding", "Build a project")

        self.assertEqual(
            clients[0].models.calls[0]["model"], "gemini-3.5-flash-lite"
        )

    def test_deep_route_never_uses_models_outside_free_allowlist(self):
        attempts = []

        def handler(model, _kwargs):
            attempts.append(model)
            if model == "gemini-3.8-flash":
                raise _ApiError(503)
            return SimpleNamespace(text="done")

        router, _clients = self._router(
            handler,
            available={
                "gemini-3.8-flash",
                "gemini-3.6-flash",
                "gemini-3-flash-preview",
                "gemini-4-carbon",
            },
        )
        self._generate(router, "cad_design", "Build a complex model")
        self.assertEqual(attempts, ["gemini-3.8-flash", "gemini-3.6-flash"])

    def test_deep_session_tier_is_inferred_and_cheap_step_uses_fast(self):
        router, clients = self._router(
            lambda _model, _kwargs: SimpleNamespace(text="done")
        )
        session = TaskSession("vibe_coding")

        self._generate(router, None, "Build the project", session=session)
        self._generate(
            router, "tool_output_parsing", "Parsed result", session=session
        )

        models = [call["model"] for client in clients for call in client.models.calls]
        self.assertEqual(models, [
            TIERS["deep"][0],
            TIERS["fast"][0],
        ])

    def test_system_messages_move_to_generation_config_and_session(self):
        router, _clients = self._router(
            lambda _model, _kwargs: SimpleNamespace(text="answer")
        )
        observed = []
        session = TaskSession("chat")

        self._generate(
            router,
            "chat",
            messages=[
                {"role": "system", "content": "Answer briefly."},
                {"role": "user", "content": "Hi"},
            ],
            session=session,
            generator=lambda _model, contents, config, *_args: (
                observed.append((contents, config))
                or SimpleNamespace(text="answer")
            ),
        )

        self.assertEqual(session.system_prompt, "Answer briefly.")
        self.assertEqual(observed[0][0], [
            {"role": "user", "parts": [{"text": "Hi"}]},
        ])
        self.assertEqual(
            observed[0][1]["system_instruction"], "Answer briefly."
        )

    def test_handoff_history_strips_model_specific_fields_from_sdk_objects(self):
        class Part:
            @staticmethod
            def model_dump():
                return {"text": "visible", "thought_signature": "private"}

        session = TaskSession(
            "chat",
            messages=[{"role": "model", "parts": [Part()]}],
        )

        self.assertEqual(
            session.safe_messages(),
            [{"role": "model", "parts": [{"text": "visible"}]}],
        )

    def test_deep_budget_near_limit_skips_spent_model(self):
        router, clients = self._router(
            lambda _model, _kwargs: SimpleNamespace(text="done"),
            settings={
                "auto_model_switching": True,
                "deep_daily_budget": 1,
            },
        )
        router._daily_counts[date.today().isoformat()] = {
            "gemini-3.8-flash": 1,
        }

        self._generate(router, "cad_design", "Design the part")

        self.assertEqual(
            clients[0].models.calls[0]["model"], "gemini-3.6-flash"
        )

    def test_auth_error_does_not_cascade(self):
        for status, message in (
            (403, "permission denied"),
            (400, "API_KEY_INVALID"),
        ):
            attempts = []

            def handler(model, _kwargs):
                attempts.append(model)
                raise _ApiError(status, message)

            legacy_attempts = []
            router, _clients = self._router(
                handler,
                legacy_fallback=lambda *_args: legacy_attempts.append("called"),
            )
            with self.assertRaises(InvalidApiKeyError):
                self._generate(router, "chat", "Hi")

            self.assertEqual(attempts, [TIERS["fast"][0]])
            self.assertEqual(legacy_attempts, [])

    def test_stream_handoff_continues_without_duplicate_text_or_tool_replay(self):
        attempts = []

        def handler(model, _kwargs):
            attempts.append(model)
            if len(attempts) == 1:
                def interrupted():
                    yield "Partial "
                    raise _ApiError(429, "quota")
                return interrupted()
            return iter(["Partial final answer."])

        router, _clients = self._router(handler)
        session = TaskSession("vibe_coding")
        self.assertTrue(session.claim_tool_call("tool-1", "write_file", {"path": "a.py"}))
        session.complete_tool_call("tool-1", "written")
        visible = []

        response, metadata = self._generate(
            router,
            "vibe_coding",
            "Build the project",
            session=session,
            on_chunk=visible.append,
        )

        self.assertEqual(response.text, "Partial final answer.")
        self.assertEqual("".join(visible), response.text)
        self.assertEqual(attempts[:2], list(TIERS["deep"][:2]))
        self.assertFalse(session.claim_tool_call("tool-1", "write_file", {"path": "a.py"}))
        self.assertTrue(session.tool_call_log[0].completed)
        self.assertEqual(metadata.reason, "rate_limit")

    def test_local_fallback_answers_after_gemini_chain_exhaustion(self):
        router, _clients = self._router(
            lambda _model, _kwargs: (_ for _ in ()).throw(_ApiError(503)),
            settings={"auto_model_switching": True, "mode": "fallback"},
            local_fallback=lambda _prompt, _system, _timeout: "local answer",
        )

        response, metadata = self._generate(router, "chat", "Hi")

        self.assertEqual(response.text, "local answer")
        self.assertEqual(metadata.model_used, "local")

    def test_existing_gemini_ladder_is_last_resort_for_non_deep_tasks(self):
        attempts = []

        def legacy(contents, _config, _timeout, _key, tier):
            attempts.append((contents, tier))
            return SimpleNamespace(text="fallback answer")

        router, _clients = self._router(
            lambda _model, _kwargs: (_ for _ in ()).throw(_ApiError(503)),
            legacy_fallback=legacy,
        )

        response, metadata = self._generate(router, "chat", "Hi")

        self.assertEqual(response.text, "fallback answer")
        self.assertEqual(metadata.model_used, "legacy")
        self.assertEqual(len(attempts), 1)

    def test_empty_deep_model_list_never_uses_unverified_legacy_models(self):
        legacy_calls = []
        router, _clients = self._router(
            lambda _model, _kwargs: self.fail("No listed model should be called."),
            available=set(),
            legacy_fallback=lambda *_args: (
                legacy_calls.append(True)
                or SimpleNamespace(text="unexpected fallback")
            ),
        )

        with self.assertRaises(RetryLaterError):
            self._generate(router, "cad_design", "Design a part")
        self.assertEqual(legacy_calls, [])

    def test_deep_route_fails_closed_when_api_key_model_discovery_fails(self):
        attempts = []
        legacy_calls = []

        def unavailable(_key):
            raise OSError("model discovery unavailable")

        router, _clients = self._router(
            lambda model, _kwargs: (
                attempts.append(model) or SimpleNamespace(text="unexpected")
            ),
            model_lister=unavailable,
            legacy_fallback=lambda *_args: (
                legacy_calls.append(True)
                or SimpleNamespace(text="unexpected fallback")
            ),
        )
        with self.assertRaises(RetryLaterError):
            self._generate(router, "cad_design", "Design a part")
        self.assertEqual(attempts, [])
        self.assertEqual(legacy_calls, [])

    def test_deep_tasks_stay_on_free_route_when_auto_switching_is_off(self):
        router, clients = self._router(
            lambda _model, _kwargs: SimpleNamespace(text="done"),
            settings={"auto_model_switching": False},
            available={"gemini-3.8-flash"},
        )
        self._generate(router, "cad_design", "Design a complicated engine")
        self.assertEqual(
            clients[0].models.calls[0]["model"], "gemini-3.8-flash"
        )

    def test_throttle_does_not_grant_more_than_capacity(self):
        bucket = TokenBucket(requests_per_minute=1)

        self.assertTrue(bucket.acquire(timeout=0))
        self.assertFalse(bucket.acquire(timeout=0))

    def test_repeated_429_cooldown_doubles_and_caps(self):
        router, _clients = self._router(lambda _model, _kwargs: None)
        key_id = "key-id"
        router._cool_model(key_id, "model", None)
        first_until, first_count = router._cooldowns[(key_id, "model")]
        router._cool_model(key_id, "model", None)
        second_until, second_count = router._cooldowns[(key_id, "model")]
        second_remaining = second_until - __import__("time").monotonic()

        self.assertEqual((first_count, second_count), (1, 2))
        self.assertGreaterEqual(second_remaining, 119)
        self.assertLessEqual(second_remaining, 121)

    def test_chain_exhaustion_preserves_pending_task(self):
        available = {TIERS["fast"][0]}
        router, _clients = self._router(
            lambda _model, _kwargs: (_ for _ in ()).throw(_ApiError(404, "not found")),
            available=available,
        )
        session = TaskSession("chat")

        with self.assertRaises(RetryLaterError):
            self._generate(router, "chat", "Hi", session=session)

        self.assertTrue(session.pending)
        self.assertIn(session.task_id, router._pending_sessions)

    def test_auto_switching_off_uses_legacy_model_selection(self):
        router, _clients = self._router(
            lambda _model, _kwargs: SimpleNamespace(text="legacy answer"),
            settings={"auto_model_switching": False},
        )
        selected = []

        response, metadata = self._generate(
            router,
            "chat",
            "Hi",
            generator=lambda model, *_args: (
                selected.append(model) or SimpleNamespace(text="legacy answer")
            ),
            legacy_tier="configured-model",
        )

        self.assertEqual(selected, ["configured-model"])
        self.assertEqual(response.text, "legacy answer")
        self.assertFalse(metadata.switched)


if __name__ == "__main__":
    unittest.main()
