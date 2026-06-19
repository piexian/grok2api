import unittest
from pathlib import Path


class StreamHeartbeatTests(unittest.TestCase):
    def test_openai_chat_streams_emit_initial_heartbeat(self):
        src = Path("app/products/openai/chat.py").read_text(encoding="utf-8")

        self.assertIn("yield \": heartbeat\\n\\n\"\n                            async for raw_line in response.aiter_lines():", src)
        self.assertIn("yield \": heartbeat\\n\\n\"\n                        async for line in _stream_chat(", src)

    def test_openai_responses_streams_emit_initial_heartbeat(self):
        src = Path("app/products/openai/responses.py").read_text(encoding="utf-8")

        self.assertIn("yield \": heartbeat\\n\\n\"\n                                current_event = \"\"", src)
        self.assertIn("yield \": heartbeat\\n\\n\"\n                    async for line in _stream_chat(", src)

    def test_anthropic_streams_emit_initial_heartbeat(self):
        src = Path("app/products/anthropic/messages.py").read_text(encoding="utf-8")

        self.assertIn("yield \": heartbeat\\n\\n\"\n\n    text_block_open = False", src)
        self.assertIn("yield \": heartbeat\\n\\n\"\n                    async for line in _stream_chat(", src)


if __name__ == "__main__":
    unittest.main()
