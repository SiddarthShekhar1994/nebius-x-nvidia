"""Offline router integration and complete preview command tests."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from keeper_core.adapters import AuthenticationError, InvalidProviderResponse, ProviderUnavailable, RateLimited
from router import calllog
from router.config import load_settings
from tests.test_triage import FakeModel, UTC_TIME, payload
from triage.model import RouterModel
from triage.preview import load_emails, preview
from triage.__main__ import main

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "demo/fixtures/inbox_75.json"


class RouterModelTest(unittest.TestCase):
    def test_router_selects_nano_logs_usage_and_exposes_no_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings({"KEEPER_DATA_DIR": directory, "TOKENFACTORY_API_KEY": "synthetic"})
            requests = []
            def create(**request):
                requests.append(request)
                return SimpleNamespace(
                    model="served-nano", usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
                    choices=[SimpleNamespace(
                        finish_reason="stop", message=SimpleNamespace(content=json.dumps(payload()), tool_calls=[]))],
                )
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            with self.assertLogs("router.core", level="WARNING"):
                answer = RouterModel(settings=settings, client=client).complete("triage", "Email content")
            self.assertEqual(requests[0]["model"], settings.models["nano"])
            self.assertNotIn("tools", requests[0])
            # Nano reasons before answering; a small cap cut answers off live.
            self.assertGreaterEqual(requests[0]["max_tokens"], 4096)
            self.assertEqual(requests[0]["messages"][0]["role"], "system")
            self.assertEqual(answer.metadata.requested_model, settings.models["nano"])
            self.assertEqual(answer.metadata.served_model, "served-nano")
            self.assertIsNone(answer.metadata.router_call_id)
            self.assertEqual(len(calllog.recent(settings.db_path)), 1)

    def test_invalid_completions_are_rejected(self):
        messages = [
            SimpleNamespace(content=None, tool_calls=[]),
            SimpleNamespace(content="{}", tool_calls=[object()]),
        ]
        for message in messages:
            response = SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])
            with patch("router.route", return_value=response):
                with self.assertRaises(InvalidProviderResponse):
                    RouterModel(settings=load_settings({"TOKENFACTORY_API_KEY": "synthetic"})).complete("triage", "data")
        with patch("router.route", return_value=SimpleNamespace(choices=[])):
            with self.assertRaises(InvalidProviderResponse):
                RouterModel(settings=load_settings({"TOKENFACTORY_API_KEY": "synthetic"})).complete("triage", "data")

    def test_provider_errors_are_normalized_and_redacted(self):
        import openai
        errors = [(openai.APIConnectionError(request=None), ProviderUnavailable)]
        for status, expected in [(400, ValueError), (401, AuthenticationError),
                                 (403, AuthenticationError), (404, ValueError),
                                 (429, RateLimited),
                                 (500, ProviderUnavailable)]:
            response = SimpleNamespace(status_code=status, request=SimpleNamespace(), headers={})
            errors.append((openai.APIStatusError("private error", response=response, body=None), expected))
        for error, expected in errors:
            with patch("router.route", side_effect=error):
                with self.assertRaises(expected):
                    RouterModel(settings=load_settings({"TOKENFACTORY_API_KEY": "synthetic"})).complete("triage", "data")

    def test_truncated_completion_is_rejected(self):
        response = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="length", message=SimpleNamespace(content="{}", tool_calls=[]))])
        with patch("router.route", return_value=response):
            with self.assertRaises(InvalidProviderResponse):
                RouterModel(settings=load_settings({"TOKENFACTORY_API_KEY": "synthetic"})).complete("triage", "data")

    def test_missing_key_and_wrong_task_stop_before_router(self):
        with patch("router.route") as route:
            with self.assertRaises(AuthenticationError):
                RouterModel(settings=load_settings({})).complete("triage", "data")
            with self.assertRaises(ValueError):
                RouterModel(settings=load_settings({})).complete("draft", "data")
            route.assert_not_called()


class PreviewTest(unittest.TestCase):
    def test_fixture_projection_never_passes_gold_or_labels_to_model(self):
        emails = load_emails(FIXTURE)
        self.assertEqual(len(emails), 75)
        model = FakeModel(payload())
        result = preview(emails[:3], model, classified_at=UTC_TIME)
        self.assertEqual(result["summary"]["total"], 3)
        self.assertEqual(result["summary"]["newsletter"], 3)
        for _, prompt in model.calls:
            self.assertNotIn('"gold"', prompt)
            self.assertNotIn('"labels"', prompt)
            self.assertNotIn("expected_application_change", prompt)
        self.assertEqual(result["results"][0]["email_id"], emails[0].id)
        self.assertEqual(result["results"][0]["proposed_labels"], ["KEEPER_NEWSLETTER", "KEEPER_SCAM_REVIEW"])

    def test_partial_model_failure_keeps_a_review_row_and_other_successes(self):
        class SometimesBroken(FakeModel):
            def complete(self, task, prompt):
                if not self.calls:
                    self.calls.append((task, prompt))
                    raise ProviderUnavailable("private response")
                return super().complete(task, prompt)
        result = preview(load_emails(FIXTURE)[:2], SometimesBroken(payload()), classified_at=UTC_TIME)
        self.assertEqual(result["summary"]["review"], 1)
        self.assertEqual(result["summary"]["newsletter"], 1)
        self.assertNotIn("private response", json.dumps(result))

    def test_invalid_fixture_is_rejected_before_live_setup(self):
        for data in [{}, {"synthetic": False, "emails": []},
                     {"synthetic": True, "emails": [None]},
                     {"synthetic": True, "emails": [{"id": "bad"}]}]:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "fixture.json"
                path.write_text(json.dumps(data))
                with self.subTest(data=data), patch("triage.__main__.RouterModel") as model:
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertNotEqual(main(["preview", "--fixture", str(path), "--live"]), 0)
                    model.assert_not_called()

    def test_full_utc_timestamp_and_unique_ids_are_required(self):
        source = json.loads(FIXTURE.read_text())["emails"][0]
        for changes in [{"received_at": "2026-10-12Z"}, {"received_at": "2026-10-12T09:01:00-07:00"},
                        {"received_at": "badZ"}, {"id": ""}, {"body_text": 3}]:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "fixture.json"
                path.write_text(json.dumps({"synthetic": True, "emails": [{**source, **changes}]}))
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    load_emails(path)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.json"
            path.write_text(json.dumps({"synthetic": True, "emails": [source, source]}))
            with self.assertRaises(ValueError):
                load_emails(path)

    def test_permanent_config_failure_stops_batch_and_redacts_error(self):
        class Broken:
            calls = 0
            def complete(self, *args):
                self.calls += 1
                raise ValueError("private configuration detail")
        model = Broken()
        with patch("triage.__main__.RouterModel", return_value=model), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(["preview", "--live", "--limit", "3"]), 2)
        self.assertEqual(model.calls, 1)
        self.assertNotIn("private configuration detail", errors.getvalue())

    def test_cli_offline_subprocess_needs_no_keys_and_creates_no_product_state(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "PYTHONPATH": str(ROOT), "KEEPER_DATA_DIR": directory,
                           "TOKENFACTORY_API_KEY": "", "KEEPER_TOKENFACTORY_KEY_FILE": "",
                           "PYTHONDONTWRITEBYTECODE": "1"}
            process = subprocess.run(
                [sys.executable, "-m", "triage", "preview", "--fixture", str(FIXTURE), "--limit", "2"],
                cwd=directory, env=environment, capture_output=True, text=True,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual(result["mode"], "offline-demo")
            self.assertEqual(result["summary"]["total"], 2)
            self.assertEqual(result["summary"]["review"], 2)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_cli_live_requires_key_and_bad_limits_make_no_calls(self):
        for arguments in [["preview", "--limit", "0"], ["preview", "--limit", "1001"]]:
            with patch("triage.__main__.RouterModel") as model, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(arguments)
                model.assert_not_called()
        with patch.dict(os.environ, {"TOKENFACTORY_API_KEY": "", "KEEPER_TOKENFACTORY_KEY_FILE": ""}):
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertNotEqual(main(["preview", "--live", "--limit", "1"]), 0)

    def test_cli_live_can_be_exercised_with_an_injected_model(self):
        model = FakeModel(payload())
        with patch("triage.__main__.RouterModel", return_value=model), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["preview", "--live", "--limit", "1"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["mode"], "live-nebius")
        self.assertEqual(result["summary"]["total"], 1)
        self.assertEqual(len(model.calls), 1)
