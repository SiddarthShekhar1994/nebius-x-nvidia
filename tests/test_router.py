"""Router tests with a fake client. No network or API key needed.

Run from the repo root:  py -m unittest discover tests
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from router import calllog, route, UnknownTaskType
from router.config import load_settings
from router.pricing import estimate_cost


def fake_response(model, prompt_tokens=100, completion_tokens=50, content="ok", tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


class FakeClient:
    def __init__(self, served_model=None, error=None):
        self.requests = []
        self.served_model = served_model
        self.error = error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return fake_response(self.served_model or request["model"])


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "router.db"
        self.settings = load_settings({"KEEPER_ROUTER_DB": str(self.db), "TOKENFACTORY_API_KEY": "x"})

    def tearDown(self):
        self.tmp.cleanup()

    def _route(self, task_type, client=None, **kw):
        client = client or FakeClient()
        route(task_type, [{"role": "user", "content": "hi"}], settings=self.settings, client=client, **kw)
        return client

    def test_each_task_type_goes_to_its_model(self):
        expected = {
            "classify": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
            "triage": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
            "radar": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
            "tool_call": "nvidia/nemotron-3-super-120b-a12b",
            "draft": "nvidia/nemotron-3-super-120b-a12b",
            "plan": "nvidia/Nemotron-3-Ultra-550b-a55b",
            "skill_write": "nvidia/Nemotron-3-Ultra-550b-a55b",
        }
        for task_type, model in expected.items():
            with self.subTest(task_type=task_type):
                client = self._route(task_type)
                self.assertEqual(client.requests[0]["model"], model)

    def test_unknown_task_type_is_rejected_before_any_call(self):
        client = FakeClient()
        with self.assertRaises(UnknownTaskType):
            route("chitchat", [], settings=self.settings, client=client)
        self.assertEqual(client.requests, [])

    def test_tools_and_extra_params_are_passed_through(self):
        tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
        client = FakeClient()
        route("tool_call", [], tools, settings=self.settings, client=client, max_tokens=7, tool_choice="auto")
        request = client.requests[0]
        self.assertEqual(request["tools"], tools)
        self.assertEqual(request["max_tokens"], 7)
        self.assertEqual(request["tool_choice"], "auto")

    def test_no_tools_key_when_none_given(self):
        client = self._route("draft")
        self.assertNotIn("tools", client.requests[0])

    def test_successful_call_is_logged_with_cost(self):
        self._route("triage")
        [row] = calllog.recent(self.db)
        self.assertEqual(row.task_type, "triage")
        self.assertEqual(row.model, "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B")
        self.assertEqual((row.prompt_tokens, row.completion_tokens), (100, 50))
        self.assertEqual(row.status, "ok")
        self.assertGreaterEqual(row.latency_ms, 0)
        self.assertAlmostEqual(row.cost_usd, (100 * 0.06 + 50 * 0.24) / 1_000_000)
        self.assertTrue(row.ts.endswith("+00:00"))

    def test_failed_call_is_logged_and_reraised(self):
        client = FakeClient(error=TimeoutError("upstream took too long"))
        with self.assertRaises(TimeoutError):
            self._route("plan", client=client)
        [row] = calllog.recent(self.db)
        self.assertEqual(row.status, "error")
        self.assertIn("upstream took too long", row.error)
        self.assertIsNone(row.cost_usd)

    def test_served_model_mismatch_is_recorded(self):
        # What inference.local does: it rewrites every request to its one model.
        client = FakeClient(served_model="nvidia/nemotron-3-super-120b-a12b")
        with self.assertLogs("router.core", level="WARNING"):
            self._route("classify", client=client)
        [row] = calllog.recent(self.db)
        self.assertEqual(row.served_model, "nvidia/nemotron-3-super-120b-a12b")
        # Priced as Super, since Super answered.
        self.assertAlmostEqual(row.cost_usd, (100 * 0.30 + 50 * 0.90) / 1_000_000)

    def test_summary_groups_by_model(self):
        self._route("classify")
        self._route("triage")
        self._route("draft")
        rows = {r[0]: r for r in calllog.summary(self.db)}
        self.assertEqual(rows["nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"][1], 2)
        self.assertEqual(rows["nvidia/nemotron-3-super-120b-a12b"][1], 1)

    def test_log_failure_does_not_break_the_call(self):
        self.db.mkdir()  # a directory where the db file should be: sqlite cannot open it
        with self.assertLogs("router.core", level="ERROR"):
            self._route("draft")

    def test_key_file_wins_over_placeholder(self):
        key_file = Path(self.tmp.name) / "tokenfactory.key"
        key_file.write_text("real-key\n")
        settings = load_settings({"TOKENFACTORY_API_KEY": "openshell:placeholder",
                                  "KEEPER_TOKENFACTORY_KEY_FILE": str(key_file)})
        self.assertEqual(settings.api_key, "real-key")

    def test_missing_key_file_falls_back_to_env(self):
        settings = load_settings({"TOKENFACTORY_API_KEY": "env-key",
                                  "KEEPER_TOKENFACTORY_KEY_FILE": "/nonexistent/key"})
        self.assertEqual(settings.api_key, "env-key")

    def test_model_ids_can_be_overridden_by_env(self):
        settings = load_settings({"KEEPER_MODEL_NANO": "nvidia/other-nano", "KEEPER_ROUTER_DB": str(self.db)})
        self.assertEqual(settings.models["nano"], "nvidia/other-nano")


class PricingTest(unittest.TestCase):
    def test_lookup_ignores_case(self):
        self.assertAlmostEqual(estimate_cost("nvidia/Nemotron-3-Ultra-550b-a55b", 1_000_000, 0), 1.00)

    def test_unknown_model_or_usage_gives_none(self):
        self.assertIsNone(estimate_cost("someone/else", 10, 10))
        self.assertIsNone(estimate_cost("nvidia/nemotron-3-super-120b-a12b", None, 10))


if __name__ == "__main__":
    unittest.main()
