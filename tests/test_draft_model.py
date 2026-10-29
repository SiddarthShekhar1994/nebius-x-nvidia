"""DraftRouterModel with a fake router: no network or API key needed."""
import sys
import types
import unittest
from unittest.mock import patch

from integrations.draft_model import DraftRouterModel
from keeper_core.adapters import InvalidProviderResponse


class DraftRouterModelTest(unittest.TestCase):
    def complete(self, finish_reason="stop", content="Thanks, Thursday at 3pm works."):
        requests = []
        model_id = "nvidia/nemotron-3-super-120b-a12b"

        def route(task_type, messages, **kwargs):
            requests.append((task_type, messages, kwargs))
            message = types.SimpleNamespace(content=content, tool_calls=[])
            return types.SimpleNamespace(model=model_id, choices=[
                types.SimpleNamespace(finish_reason=finish_reason, message=message)
            ])

        fake_router = types.ModuleType("router")
        fake_router.route = route
        fake_openai = types.ModuleType("openai")
        fake_openai.APIConnectionError = type("APIConnectionError", (Exception,), {})
        fake_openai.APIStatusError = type("APIStatusError", (Exception,), {})
        settings = types.SimpleNamespace(api_key="synthetic", models={"super": model_id})
        with patch.dict(sys.modules, {"router": fake_router, "openai": fake_openai}):
            answer = DraftRouterModel(settings).complete("draft", "synthetic email")
        return answer, requests

    def test_draft_goes_to_super_with_room_to_reason(self):
        answer, requests = self.complete()
        self.assertEqual(answer.content, "Thanks, Thursday at 3pm works.")
        self.assertEqual(requests[0][0], "draft")
        self.assertEqual(requests[0][1][0]["role"], "system")
        self.assertNotIn("tools", requests[0][2])
        # Super reasons before writing; a 512-token cap cut drafts off.
        self.assertGreaterEqual(requests[0][2]["max_tokens"], 4096)

    def test_cut_off_draft_is_rejected(self):
        with self.assertRaises(InvalidProviderResponse):
            self.complete(finish_reason="length", content="")


if __name__ == "__main__":
    unittest.main()
