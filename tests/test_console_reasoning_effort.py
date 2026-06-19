import unittest

from app.control.model.registry import resolve
from app.dataplane.reverse.protocol.xai_console import build_console_payload


class ConsoleReasoningEffortTest(unittest.TestCase):
    def test_grok_420_console_does_not_default_reasoning_effort(self):
        spec = resolve("grok-4.20-0309-reasoning")

        self.assertEqual(spec.console_model, "grok-4.20-0309-reasoning")
        self.assertEqual(spec.default_reasoning_effort, "")

    def test_grok_420_console_omits_explicit_reasoning_effort(self):
        payload = build_console_payload(
            console_model="grok-4.20-0309-reasoning",
            input="hello",
            reasoning_effort="high",
        )

        self.assertNotIn("reasoning", payload)

    def test_supported_console_model_keeps_reasoning_effort(self):
        payload = build_console_payload(
            console_model="grok-4.3",
            input="hello",
            reasoning_effort="xhigh",
        )

        self.assertEqual(payload["reasoning"], {"effort": "high"})
