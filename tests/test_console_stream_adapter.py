import unittest
from types import SimpleNamespace
from unittest.mock import patch

import orjson

from app.control.model.registry import resolve as resolve_model
from app.dataplane.reverse.protocol.xai_console import (
    ConsoleStreamAdapter,
    build_console_input,
    client_function_tool_names,
    convert_openai_tool_choice,
    convert_openai_tools_to_console,
    extract_console_tool_calls,
    inject_web_search_tool,
)
from app.platform.tokens import estimate_tool_call_tokens


def _data(obj: dict) -> str:
    return orjson.dumps(obj).decode()


def _sse_lines() -> list[str]:
    return [
        "event: response.output_text.delta",
        f"data: {_data({'delta': 'preface'})}",
        "event: response.output_item.done",
        f"data: {_data({'output_index': 0, 'item': {'id': 'fc_1', 'type': 'function_call', 'call_id': 'call_1', 'name': 'bash', 'arguments': '{}', 'status': 'completed'}})}",
        "event: response.completed",
        f"data: {_data({'response': {}})}",
    ]


def _json_payloads(frames: list[str]) -> list[dict]:
    payloads: list[dict] = []
    for frame in frames:
        for line in frame.splitlines():
            if not line.startswith("data: "):
                continue
            data = line.removeprefix("data: ")
            if data == "[DONE]":
                continue
            payloads.append(orjson.loads(data))
    return payloads


class _FakeConfig:
    def get(self, key: str, default=None):
        return default

    def get_float(self, key: str, default: float) -> float:
        return default


class _FakeDirectory:
    async def release(self, acct) -> None:
        pass

    async def feedback(self, *args, **kwargs) -> None:
        pass


class _FakeResponse:
    def __init__(self, lines: list[str] | None = None, content: bytes | None = None) -> None:
        self._lines = lines or []
        self.content = content or b"{}"

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeSession:
    async def __aexit__(self, exc_type, exc, tb) -> None:
        pass


async def _fake_reserve_account(*args, **kwargs):
    return SimpleNamespace(token="token-test"), 5


async def _fake_console_post(*args, **kwargs):
    return _FakeSession(), _FakeResponse(_sse_lines())


async def _noop_async(*args, **kwargs) -> None:
    pass


class ConsoleStreamAdapterToolFilteringTests(unittest.TestCase):
    def test_default_adapter_ignores_internal_function_calls_and_keeps_text(self) -> None:
        adapter = ConsoleStreamAdapter()

        adapter.feed_event("response.output_item.added")
        adapter.feed_data(_data({
            "output_index": 0,
            "item": {
                "id": "builtin_1",
                "type": "function_call",
                "call_id": "call_builtin",
                "name": "x_search",
                "arguments": "",
            },
        }))
        adapter.feed_event("response.output_text.delta")
        event = adapter.feed_data(_data({"delta": "final text"}))

        self.assertEqual(event, {"kind": "text", "content": "final text"})
        self.assertEqual(adapter.full_text, "final text")
        self.assertEqual(adapter.function_call_items, [])

    def test_collects_only_client_declared_function_tool_calls(self) -> None:
        adapter = ConsoleStreamAdapter(function_tool_names={"lookup_order"})

        adapter.feed_event("response.output_item.added")
        adapter.feed_data(_data({
            "output_index": 0,
            "item": {
                "id": "builtin_1",
                "type": "function_call",
                "call_id": "call_builtin",
                "name": "web_search",
                "arguments": "",
            },
        }))
        adapter.feed_event("response.function_call_arguments.delta")
        adapter.feed_data(_data({"output_index": 1, "delta": '{"order_id":"A'}))
        adapter.feed_event("response.output_item.done")
        adapter.feed_data(_data({
            "output_index": 1,
            "item": {
                "id": "fc_1",
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup_order",
                "arguments": '{"order_id":"A123"}',
                "status": "completed",
            },
        }))

        self.assertEqual(
            adapter.function_call_items,
            [{
                "id": "fc_1",
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup_order",
                "arguments": '{"order_id":"A123"}',
                "status": "completed",
            }],
        )
        self.assertEqual(adapter.parsed_tool_calls[0].name, "lookup_order")

    def test_console_tool_conversion_filters_internal_function_names(self) -> None:
        internal_tools = [
            {"type": "function", "function": {"name": name}}
            for name in ("web_search", "open_page", "x_search", "code_execution")
        ]
        user_tools = internal_tools + [
            {"type": "function", "function": {"name": "search"}},
            {"type": "web_search", "filters": {"allowed_domains": ["x.ai"]}},
        ]

        self.assertEqual(client_function_tool_names(internal_tools), set())
        self.assertEqual(client_function_tool_names(user_tools), {"search"})
        self.assertEqual(
            convert_openai_tool_choice(
                {"type": "function", "function": {"name": "web_search"}}
            ),
            "auto",
        )

        console_tools = inject_web_search_tool(convert_openai_tools_to_console(user_tools))

        self.assertEqual(
            console_tools[0],
            {"type": "web_search", "filters": {"allowed_domains": ["x.ai"]}},
        )
        self.assertEqual(console_tools[1]["type"], "x_search")
        self.assertIn({"type": "function", "name": "search"}, console_tools)

    def test_console_input_maps_tool_roundtrip_without_literal_none(self) -> None:
        empty_input, instructions = build_console_input([{"role": "user", "content": None}])
        self.assertEqual(empty_input, [])
        self.assertEqual(instructions, "")

        input_array, _ = build_console_input([
            {"role": "user", "content": "lookup order"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "lookup_order",
                        "arguments": '{"order_id":"A123"}',
                    },
                }],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "shipped"},
        ])

        self.assertEqual(
            input_array,
            [
                {"role": "user", "content": [{"type": "input_text", "text": "lookup order"}]},
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup_order",
                    "arguments": '{"order_id":"A123"}',
                    "status": "completed",
                },
                {"type": "function_call_output", "call_id": "call_1", "output": "shipped"},
            ],
        )

    def test_extract_console_tool_calls_filters_completed_internal_output(self) -> None:
        response = {
            "output": [
                {
                    "id": "builtin_1",
                    "type": "function_call",
                    "call_id": "call_builtin",
                    "name": "x_search",
                    "arguments": '{"query":"grok"}',
                },
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup_order",
                    "arguments": '{"order_id":"A123"}',
                },
            ]
        }

        self.assertEqual(extract_console_tool_calls(response), [])
        self.assertEqual(
            extract_console_tool_calls(response, {"lookup_order"}),
            [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "lookup_order",
                    "arguments": '{"order_id":"A123"}',
                },
            }],
        )


class ConsoleRouteToolFilteringTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_console_stream_buffers_text_when_late_function_call_arrives(self) -> None:
        from app.products.openai import chat

        with (
            patch("app.dataplane.account._directory", _FakeDirectory()),
            patch.object(chat, "get_config", return_value=_FakeConfig()),
            patch.object(chat, "selection_max_retries", return_value=0),
            patch.object(chat, "reserve_account", _fake_reserve_account),
            patch.object(chat, "_console_post", _fake_console_post),
            patch.object(chat, "_quota_sync", _noop_async),
            patch.object(chat, "_fail_sync", _noop_async),
        ):
            gen = await chat._console_completions(
                spec=resolve_model("grok-build-0.1"),
                model="grok-build-0.1",
                messages=[{"role": "user", "content": "use bash"}],
                is_stream=True,
                emit_think=False,
                tools=[{"type": "function", "function": {"name": "bash"}}],
            )
            frames = [frame async for frame in gen]

        joined = "".join(frames)
        payloads = _json_payloads(frames)

        self.assertNotIn("preface", joined)
        self.assertTrue(any(
            (payload["choices"][0].get("delta") or {}).get("tool_calls")
            for payload in payloads
            if payload.get("choices")
        ))
        final_usage = next(
            payload["usage"] for payload in payloads
            if payload.get("choices") and payload["choices"][0].get("finish_reason") == "tool_calls"
        )
        self.assertEqual(
            final_usage["completion_tokens"],
            estimate_tool_call_tokens([
                SimpleNamespace(name="bash", arguments="{}", call_id="call_1")
            ]),
        )

    async def test_responses_console_stream_buffers_text_when_late_function_call_arrives(self) -> None:
        from app.products.openai import responses

        with (
            patch("app.dataplane.account._directory", _FakeDirectory()),
            patch.object(responses, "get_config", return_value=_FakeConfig()),
            patch.object(responses, "selection_max_retries", return_value=0),
            patch.object(responses, "reserve_account", _fake_reserve_account),
            patch.object(responses, "_console_post", _fake_console_post),
            patch.object(responses, "_quota_sync", _noop_async),
            patch.object(responses, "_fail_sync", _noop_async),
        ):
            gen = await responses._console_responses_dispatch(
                spec=resolve_model("grok-build-0.1"),
                model="grok-build-0.1",
                messages=[{"role": "user", "content": "use bash"}],
                stream=True,
                temperature=0.7,
                top_p=0.95,
                tools=[{"type": "function", "function": {"name": "bash"}}],
                tool_choice=None,
            )
            frames = [frame async for frame in gen]

        joined = "".join(frames)
        payloads = _json_payloads(frames)
        completed = next(payload for payload in payloads if payload.get("type") == "response.completed")

        self.assertNotIn("preface", joined)
        self.assertIn("response.function_call_arguments.done", joined)
        self.assertEqual(completed["response"]["output"][0]["type"], "function_call")
        self.assertEqual(
            completed["response"]["usage"]["output_tokens"],
            estimate_tool_call_tokens([
                SimpleNamespace(name="bash", arguments="{}", call_id="call_1")
            ]),
        )


if __name__ == "__main__":
    unittest.main()
