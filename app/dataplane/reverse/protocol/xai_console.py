"""Console API protocol — payload builder and response parser.

The ``console.x.ai/v1/responses`` endpoint shares SSO cookies with grok.com
but exposes the OpenAI Responses API directly. Free/basic accounts can call
all models (grok-4.3, grok-4.20-*, etc.) through this endpoint, bypassing
the tier restrictions of the grok.com web chat API.

The upstream API supports:
  - Plain string input or structured input arrays (for multimodal / chat history)
  - Native function calling via ``tools`` field
  - Reasoning summary streaming
  - SSE streaming with OpenAI Responses API event names

Request format (string input):
    {"model": "grok-4.3", "input": "What is 1+1?", "stream": true}

Request format (structured input + tools):
    {s model. Please try again later. Resets in: 30m0s (trace ID: 681cf17538e7dbc5a2362c74348fb8b9)
    Feedback submitted   
        "model": "grok-4.3",
        "input": [
            {"role": "user", "content": [
                {"type": "input_text", "text": "What's the weather?"},
                {"type": "input_image", "image_url": "https://...", "detail": "auto"}
            ]}
        ],
        "tools": [
            {"type": "function", "name": "get_weather",
             "description": "...", "parameters": {...}}
        ],
        "tool_choice": "auto"
    }

Response output items (non-streaming):
  - {"type": "reasoning", "summary": [{"type": "summary_text", "text": "..."}]}
  - {"type": "message", "role": "assistant",
     "content": [{"type": "output_text", "text": "...", "annotations": [...]}]}
  - {"type": "function_call", "call_id": "...", "name": "...", "arguments": "..."}
"""

import re
from typing import Any

import orjson

from app.platform.config.snapshot import get_config
from app.platform.errors import UpstreamError
from app.platform.logging.logger import logger
from app.dataplane.reverse.protocol.tool_parser import ParsedToolCall


# Grok/xAI internal tool names may appear as function_call items in console
# streams. They are server-side work, not client-declared OpenAI tool calls.
_CONSOLE_INTERNAL_TOOL_NAMES: frozenset[str] = frozenset({
    "web_search",
    "x_search",
    "code_interpreter",
    "file_search",
    "web_search_with_snippets",
    "browse_page",
    "open_page",
    "open_page_with_find",
    "search_images",
    "image_search",
    "view_image",
    "x_user_search",
    "x_keyword_search",
    "x_semantic_search",
    "x_thread_fetch",
    "view_x_video",
    "chatroom_send",
    "code_execution",
    "collections_search",
})


def _is_console_internal_tool_name(name: str) -> bool:
    return name.strip() in _CONSOLE_INTERNAL_TOOL_NAMES


def client_function_tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    """Return client-declared function tool names, excluding console internals."""
    names: set[str] = set()
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        fn = tool.get("function")
        src = fn if isinstance(fn, dict) else tool
        name = str(src.get("name") or "").strip()
        if name and not _is_console_internal_tool_name(name):
            names.add(name)
    return names


def _tool_identity(tool: dict[str, Any]) -> tuple[str, str]:
    tool_type = str(tool.get("type") or "").strip()
    if tool_type == "function":
        return tool_type, str(tool.get("name") or "").strip()
    return tool_type, ""


def _merge_console_tools(
    default_tools: list[dict[str, Any]],
    user_tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    positions: dict[tuple[str, str], int] = {}
    for tool in default_tools:
        ident = _tool_identity(tool)
        positions[ident] = len(result)
        result.append(tool)
    for tool in user_tools:
        ident = _tool_identity(tool)
        pos = positions.get(ident)
        if pos is None:
            positions[ident] = len(result)
            result.append(tool)
        else:
            result[pos] = tool
    return result


def _default_console_tools() -> list[dict[str, Any]]:
    return [
        {"type": "web_search", "enable_image_understanding": True},
        {"type": "x_search", "enable_video_understanding": True},
    ]

# ---------------------------------------------------------------------------
# Input conversion (OpenAI Chat Completions → console.x.ai input array)
# ---------------------------------------------------------------------------


def build_console_input(messages: list[dict[str, Any]], ) -> tuple[list[dict[str, Any]], str]:
    """Convert OpenAI Chat Completions messages → console structured input.

    Returns ``(input_array, instructions)``:
      - ``input_array`` is the list passed as Responses API ``input`` field.
      - ``instructions`` aggregates all ``role=system`` messages and is
        passed via the separate Responses API ``instructions`` field for
        better reasoning model behaviour.

    Mapping rules:
      - ``role=system``            → folded into ``instructions``
      - ``role=user/assistant``    → preserved with content blocks converted
      - Content block ``text``     → ``{type: input_text/output_text, text}``
      - Content block ``image_url`` → ``{type: input_image, image_url, detail}``
      - ``role=tool``              → ``{type: function_call_output,
                                        call_id, output}``
      - ``role=assistant`` with ``tool_calls`` → emit one ``function_call``
        item per call before any accompanying text.
    """
    instructions_parts: list[str] = []
    output: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role") or "user"
        content = msg.get("content")
        tool_calls = msg.get("tool_calls")

        # ── system → instructions ────────────────────────────────────────
        if role == "system":
            if isinstance(content, str) and content.strip():
                instructions_parts.append(content.strip())
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text") or ""
                        if text.strip():
                            instructions_parts.append(text.strip())
            continue

        # ── tool result → function_call_output ───────────────────────────
        if role == "tool":
            call_id = msg.get("tool_call_id") or ""
            text = content if isinstance(content, str) else _flatten_text(content)
            output.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": text or "",
            })
            continue

        # ── assistant with tool_calls → function_call items ──────────────
        if role == "assistant" and tool_calls:
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                output.append(
                    {
                        "type": "function_call",
                        "call_id": tc.get("id") or fn.get("name") or "",
                        "name": fn.get("name") or "",
                        "arguments": fn.get("arguments") or "{}",
                        "status": "completed",
                    })
            # Trailing assistant text (rare) is emitted as a normal message
            text = content if isinstance(content, str) else _flatten_text(content)
            if text and text.strip():
                output.append(
                    {
                        "role": "assistant",
                        "content": [{
                            "type": "output_text",
                            "text": text.strip()
                        }],
                    })
            continue

        # ── normal user / assistant message ──────────────────────────────
        blocks = _convert_content_blocks(content, role)
        if not blocks:
            continue
        output.append({"role": role, "content": blocks})

    instructions = "\n\n".join(instructions_parts).strip()
    return output, instructions


def _flatten_text(content: Any) -> str:
    """Flatten an OpenAI content array into a single text string."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text") or ""
            if text:
                parts.append(text)
    return "\n".join(parts)


def _convert_content_blocks(
    content: Any,
    role: str,
) -> list[dict[str, Any]]:
    """Convert one OpenAI message content (str or array) → console blocks."""
    text_type = "output_text" if role == "assistant" else "input_text"

    # Plain string content
    if isinstance(content, str):
        text = content.strip()
        if not text:
            return []
        return [{"type": text_type, "text": text}]

    # Already-structured array
    if not isinstance(content, list):
        return []

    blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")

        if btype == "text":
            text = block.get("text") or ""
            if text.strip():
                blocks.append({"type": text_type, "text": text})
        elif btype == "image_url":
            inner = block.get("image_url") or {}
            if isinstance(inner, str):
                url, detail = inner, "auto"
            else:
                url = inner.get("url") or ""
                detail = inner.get("detail") or "auto"
            if url:
                blocks.append({
                    "type": "input_image",
                    "image_url": url,
                    "detail": detail,
                })
        elif btype in ("input_text", "output_text", "input_image"):
            # Already in console format — pass through
            blocks.append(dict(block))
        else:
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                blocks.append({"type": text_type, "text": text})

    return blocks


# ---------------------------------------------------------------------------
# Tool format conversion
# ---------------------------------------------------------------------------


def convert_openai_tools_to_console(tools: list[dict[str, Any]] | None, ) -> list[dict[str, Any]]:
    """Convert OpenAI Chat Completions tools → console (Responses API) tools.

    OpenAI Chat Completions:
        {"type": "function", "function": {"name", "description", "parameters"}}

    Console (Responses API):
        {"type": "function", "name", "description", "parameters"}

    Already-flat tools are passed through (e.g. ``web_search`` server-side
    tool, ``code_interpreter``, ``x_search`` etc.).
    """
    if not tools:
        return []
    out: list[dict[str, Any]] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") != "function":
            # Pass through server-side tools (web_search, x_search, etc.)
            out.append(dict(t))
            continue
        fn = t.get("function")
        src = fn if isinstance(fn, dict) else t
        name = str(src.get("name") or "").strip()
        if not name or _is_console_internal_tool_name(name):
            continue

        item: dict[str, Any] = {"type": "function", "name": name}
        description = src.get("description")
        if description is not None:
            item["description"] = description
        parameters = src.get("parameters")
        if parameters is not None:
            item["parameters"] = parameters
        for key in ("strict",):
            if key in src:
                item[key] = src[key]
            elif key in t:
                item[key] = t[key]
        out.append(item)
    return out


def convert_openai_tool_choice(tool_choice: Any) -> Any:
    """Convert OpenAI tool_choice → console tool_choice.

    OpenAI:  "none" | "auto" | "required" | {"type":"function","function":{"name":"x"}}
    Console: "none" | "auto" | "required" | {"type":"function","name":"x"}
    """
    if isinstance(tool_choice, str):
        return tool_choice
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        fn = tool_choice.get("function")
        if isinstance(fn, dict):
            name = str(fn.get("name") or "").strip()
        else:
            name = str(tool_choice.get("name") or "").strip()
        if not name:
            return dict(tool_choice)
        if _is_console_internal_tool_name(name):
            return "auto"
        if isinstance(fn, dict):
            return {"type": "function", "name": name}
        return dict(tool_choice)
    return tool_choice


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------

_REASONING_EFFORT_UNSUPPORTED_MODELS = {
    # Upstream rejects `reasoning.effort`/`reasoningEffort` for these Console
    # model ids with HTTP 400. Keep this at the protocol layer so every caller
    # path avoids the unsupported field, including explicit client defaults.
    "grok-4.20-0309-reasoning",
    "grok-4.20-0309-non-reasoning",
    "grok-4.20-multi-agent-0309",
    "grok-build-0.1",
}


def console_model_supports_reasoning_effort(console_model: str) -> bool:
    return console_model not in _REASONING_EFFORT_UNSUPPORTED_MODELS


def build_console_payload(
    *,
    console_model: str,
    input: Any,
    instructions: str = "",
    stream: bool = False,
    temperature: float | None = None,
    top_p: float | None = None,
    reasoning_effort: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    response_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the JSON payload for POST /v1/responses on console.x.ai.

    ``input`` may be a plain string or an array of structured input items
    (use :func:`build_console_input` to convert OpenAI messages).

    ``tools`` should already be in console format (use
    :func:`convert_openai_tools_to_console`).

    ``features.custom_instruction`` (the admin-configured global system
    prompt) is merged into ``instructions`` at the protocol layer so that
    every console request mirrors the grok.com path's ``customPersonality``
    injection. The global instruction is prepended; per-request system
    messages follow and may refine or override it.
    """
    payload: dict[str, Any] = {
        "model": console_model,
        "input": input,
    }
    if stream:
        payload["stream"] = True

    custom = get_config().get_str("features.custom_instruction", "").strip()
    user_sys = (instructions or "").strip()
    merged = "\n\n".join(p for p in (custom, user_sys) if p)
    if merged:
        payload["instructions"] = merged
    if temperature is not None:
        payload["temperature"] = temperature
    if top_p is not None:
        payload["top_p"] = top_p
    # Console upstream accepts effort ∈ {"minimal", "low", "medium", "high"}
    # only for model ids that expose this control. Map project-specific values:
    # "none" → omit (emit_think handles client-side suppression separately);
    # "xhigh" → "high" (upstream cap).
    if (
        reasoning_effort
        and reasoning_effort != "none"
        and console_model_supports_reasoning_effort(console_model)
    ):
        upstream_effort = "high" if reasoning_effort == "xhigh" else reasoning_effort
        payload["reasoning"] = {"effort": upstream_effort}
    if tools:
        payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
    if response_options:
        for key, value in response_options.items():
            if value is not None:
                payload[key] = value

    if isinstance(input, str):
        msg_repr = f"len={len(input)}"
    elif isinstance(input, list):
        msg_repr = f"items={len(input)}"
    else:
        msg_repr = "unknown"
    logger.debug(
        "console payload built: model={} stream={} input_{} tools={}",
        console_model,
        stream,
        msg_repr,
        len(tools) if tools else 0,
    )
    return payload


# ---------------------------------------------------------------------------
# Non-streaming response parsing
# ---------------------------------------------------------------------------


def extract_console_text(response_json: dict[str, Any]) -> str:
    """Extract the assistant's final text from a non-streaming response."""
    output = response_json.get("output") or []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        contents = item.get("content") or []
        for c in contents:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "output_text":
                return c.get("text") or ""
    return ""


def extract_console_reasoning(response_json: dict[str, Any]) -> str:
    """Extract reasoning summary text if present (non-streaming)."""
    output = response_json.get("output") or []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "reasoning":
            summary = item.get("summary") or []
            parts: list[str] = []
            for s in summary:
                if isinstance(s, dict):
                    text = s.get("text") or s.get("content") or ""
                    if text:
                        parts.append(text)
                elif isinstance(s, str):
                    parts.append(s)
            return "\n".join(parts)
    return ""


def extract_console_tool_calls(
    response_json: dict[str, Any],
    function_tool_names: set[str] | list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Extract tool calls from a non-streaming response.

    Returns a list of OpenAI Chat Completions tool_call dicts:
        [{"id": "call_xxx", "type": "function",
          "function": {"name": "...", "arguments": "..."}}]

    Console responses include each tool call as a top-level output item
    of type ``function_call`` with a ``call_id``, ``name`` and
    JSON-serialised ``arguments`` string.
    """
    allowed_names = {
        str(name).strip()
        for name in (function_tool_names or ())
        if str(name).strip() and not _is_console_internal_tool_name(str(name).strip())
    }
    if not allowed_names:
        return []

    output = response_json.get("output") or []
    calls: list[dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "function_call":
            continue
        name = str(item.get("name") or "").strip()
        if name not in allowed_names:
            continue
        call_id = item.get("call_id") or item.get("id") or ""
        calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": item.get("arguments") or "{}",
                },
            })
    return calls


def extract_console_search_sources(response_json: dict[str, Any], ) -> list[dict[str, Any]]:
    """Extract the search sources list from web_search_call output items.

    Returns a deduplicated list of source dicts in the format used by the
    existing grok.com path:
        [{"url": "https://...", "title": ""}, ...]

    Two upstream variants are handled:

    1. Single-agent models (grok-4.3, grok-4.20-0309-reasoning) emit a
       ``web_search_call`` output item per search with full sources:
         ``{"type": "search", "sources": [{"url": "..."}, ...]}``
       or ``{"type": "open_page", "url": "..."}``.

    2. Multi-agent models (grok-4.20-multi-agent-0309) skip ``web_search_call``
       items entirely and embed URLs only as document-level annotations on
       the final assistant message with ``start_index == end_index == 0``.
       We fall back to those annotation URLs so callers always see a
       useful citation list regardless of the upstream emission format.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in response_json.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "web_search_call":
            continue
        action = item.get("action") or {}
        if not isinstance(action, dict):
            continue
        # Search action with sources list
        for src in action.get("sources") or []:
            if not isinstance(src, dict):
                continue
            url = src.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            out.append({
                "url": url,
                "title": src.get("title") or "",
            })
        # Page-open action — single URL
        if action.get("type") == "open_page":
            url = action.get("url") or ""
            if url and url not in seen:
                seen.add(url)
                out.append({"url": url, "title": ""})

    # Fallback: harvest URLs from message annotations. Multi-agent
    # responses publish citations only here. We dedupe against the
    # web_search_call sources collected above so single-agent paths
    # remain unchanged.
    for item in response_json.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            for ann in content.get("annotations") or []:
                if not isinstance(ann, dict):
                    continue
                if ann.get("type") not in (None, "url_citation"):
                    continue
                url = ann.get("url") or ""
                if not url or url in seen:
                    continue
                seen.add(url)
                title = ann.get("title") or ""
                # Multi-agent annotations sometimes set title=url; strip
                # the duplicate so the source list reads cleanly.
                if title == url:
                    title = ""
                out.append({"url": url, "title": title})
    return out


def format_search_sources_suffix(search_sources: list[dict[str, Any]] | None) -> str:
    """Format collected search sources as a ``## Sources`` markdown section.

    Returns ``""`` when ``features.show_search_sources`` is disabled or the
    input list is empty. Mirrors :meth:`xai_chat.StreamAdapter.references_suffix`
    so that text-parsing clients (which can't read the structured
    ``search_sources`` field) see identical formatting across both the
    grok.com app-chat and console.x.ai paths.

    The leading ``[grok2api-sources]: #`` marker is a markdown link reference
    definition that renderers ignore; multi-turn handlers use it to identify
    and strip prior-turn ``## Sources`` blocks.
    """
    if not search_sources:
        return ""
    if not get_config().get_bool("features.show_search_sources", False):
        return ""
    lines = ["\n\n## Sources", "[grok2api-sources]: #"]
    for item in search_sources:
        url = (item or {}).get("url") or ""
        if not url:
            continue
        title = (item or {}).get("title") or url
        title = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
        lines.append(f"- [{title}]({url})")
    if len(lines) == 2:
        return ""
    return "\n".join(lines) + "\n"


def inject_web_search_tool(tools: list[dict[str, Any]] | None, ) -> list[dict[str, Any]]:
    """Ensure default console-native search tools are present.

    If the user supplied a tool with the same identity, their configuration
    overrides the default. xAI charges search calls from prepaid credits.
    """
    existing = [dict(t) for t in tools or [] if isinstance(t, dict)]
    return _merge_console_tools(_default_console_tools(), existing)


def extract_console_annotations(response_json: dict[str, Any], ) -> list[dict[str, Any]]:
    """Extract URL citation annotations from a non-streaming response.

    Returns a flat list of citation dicts in chat-completions format:
        [{"url": "...", "title": "...", "start_index": 0, "end_index": 0}]
    """
    out: list[dict[str, Any]] = []
    output = response_json.get("output") or []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        contents = item.get("content") or []
        for c in contents:
            if not isinstance(c, dict):
                continue
            anns = c.get("annotations") or []
            for a in anns:
                if not isinstance(a, dict):
                    continue
                if a.get("type") not in (None, "url_citation"):
                    continue
                url = a.get("url") or ""
                if not url:
                    continue
                out.append(
                    {
                        "url": url,
                        "title": a.get("title") or "",
                        "start_index": int(a.get("start_index") or 0),
                        "end_index": int(a.get("end_index") or 0),
                    })
    return out


def extract_console_usage(response_json: dict[str, Any]) -> dict[str, int]:
    """Extract usage tokens from a non-streaming response."""
    usage = response_json.get("usage") or {}
    return {
        "prompt_tokens": int(usage.get("input_tokens") or 0),
        "completion_tokens": int(usage.get("output_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "reasoning_tokens": int(
            (usage.get("output_tokens_details") or {}).get("reasoning_tokens") or
            usage.get("reasoning_tokens") or 0),
    }


_CONSOLE_LIMIT_RE = re.compile(
    r"(Requests|Tokens) per (Second|Minute).*?\(actual/limit\):\s*(\d+)\s*/\s*(\d+)"
)
_CONSOLE_TEAM_MODEL_RE = re.compile(
    r"team ([0-9a-f-]{36}) and model ([^ .]+)", re.IGNORECASE
)
_CONSOLE_RESET_RE = re.compile(r"Resets in:\s*([0-9dhms ]+)", re.IGNORECASE)


def _parse_reset_seconds(value: str) -> int | None:
    total = 0
    for amount, unit in re.findall(r"(\d+)\s*([dhms])", value.lower()):
        n = int(amount)
        if unit == "d":
            total += n * 86_400
        elif unit == "h":
            total += n * 3_600
        elif unit == "m":
            total += n * 60
        else:
            total += n
    return total or None


def _console_error_details(message: str, code: str = "") -> dict[str, Any]:
    tm = _CONSOLE_TEAM_MODEL_RE.search(message)
    reset_match = _CONSOLE_RESET_RE.search(message)
    lower = message.lower()
    return {
        "code": code,
        "team": tm.group(1) if tm else "",
        "model": tm.group(2) if tm else "",
        "limits": [
            {
                "kind": m.group(1).lower(),
                "window": m.group(2).lower(),
                "actual": int(m.group(3)),
                "limit": int(m.group(4)),
            }
            for m in _CONSOLE_LIMIT_RE.finditer(message)
        ],
        "reset_seconds": (
            _parse_reset_seconds(reset_match.group(1)) if reset_match else None
        ),
        "free_limit": (
            "free usage limit" in lower
            or "purchase credits" in lower
            or "usage limit reached" in lower
        ),
    }


def parse_console_error(status_code: int, body: str) -> UpstreamError:
    """Convert a non-200 console response into an UpstreamError."""
    message = f"Console upstream returned {status_code}"
    code = ""
    details: dict[str, Any] = {}
    try:
        obj = orjson.loads(body) if body else {}
        if isinstance(obj, dict):
            code = str(obj.get("code") or "")
            err = obj.get("error") or obj.get("code") or ""
            if isinstance(err, dict):
                err = err.get("message") or err.get("code") or ""
            if err:
                err = str(err)
                message = f"{message}: {err}"
                details = _console_error_details(err, code)
    except (orjson.JSONDecodeError, ValueError, TypeError):
        pass
    exc = UpstreamError(message, status=status_code, body=body[:400])
    if details:
        exc.details["console"] = details
    return exc


# ---------------------------------------------------------------------------
# SSE streaming event parsing
# ---------------------------------------------------------------------------


def classify_console_sse_line(line: str | bytes) -> tuple[str, str]:
    """Return (kind, payload) for one raw SSE line.

    kind:
      - 'data'  — SSE data line; payload is the JSON string
      - 'event' — SSE event name line; payload is the event name
      - 'skip'  — comment / blank / unrecognized
    """
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    line = line.strip()
    if not line:
        return "skip", ""
    if line.startswith("event:"):
        return "event", line[6:].strip()
    if line.startswith("data:"):
        data = line[5:].strip()
        return "data", data
    if line.startswith("{"):
        return "data", line
    return "skip", ""


class ConsoleStreamAdapter:
    """Parse upstream Console SSE frames and emit text/reasoning/tool deltas.

    The console.x.ai SSE protocol uses OpenAI Responses API event names:
      - response.created
      - response.output_item.added                  ← announces a new item
      - response.content_part.added
      - response.output_text.delta                  ← text chunks
      - response.output_text.done
      - response.reasoning_summary_text.delta       ← reasoning chunks
      - response.function_call_arguments.delta      ← tool args streaming
      - response.function_call_arguments.done       ← tool args complete
      - response.output_item.done                   ← completed item
      - response.output_text.annotation.added       ← citation annotation
      - response.completed
      - response.failed / response.cancelled / response.error
    """

    __slots__ = (
        "_current_event",
        "_active_tool_index",
        "_tool_args_buf",
        "_allowed_function_names",
        "_ignored_function_keys",
        "_function_keys_by_output_index",
        "_seen_source_urls",
        "tool_calls",
        "annotations",
        "search_sources",
        "text_buf",
        "thinking_buf",
        "_usage",
    )

    def __init__(
        self,
        function_tool_names: set[str] | list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self._current_event: str = ""
        self._active_tool_index: dict[str, int] = {}  # item_id → index
        self._tool_args_buf: dict[str, list[str]] = {}  # item_id → args chunks
        self._allowed_function_names = {
            str(name).strip()
            for name in (function_tool_names or ())
            if str(name).strip() and not _is_console_internal_tool_name(str(name).strip())
        }
        self._ignored_function_keys: set[str] = set()
        self._function_keys_by_output_index: dict[str, str] = {}
        self._seen_source_urls: set[str] = set()
        self.tool_calls: list[dict[str, Any]] = []
        self.annotations: list[dict[str, Any]] = []
        self.search_sources: list[dict[str, Any]] = []
        self.text_buf: list[str] = []
        self.thinking_buf: list[str] = []
        self._usage: dict[str, int] = {}

    def references_suffix(self) -> str:
        """Return the ``## Sources`` markdown block for the collected sources.

        Returns ``""`` when ``features.show_search_sources`` is disabled or
        no sources were collected. Shared formatting with the grok.com path
        via :func:`format_search_sources_suffix`.
        """
        return format_search_sources_suffix(self.search_sources)

    def feed_event(self, event_name: str) -> None:
        """Record the most recent ``event:`` name from the SSE stream."""
        self._current_event = event_name

    def _function_key(self, obj: dict[str, Any]) -> str:
        raw = obj.get("item_id")
        if raw:
            return str(raw)
        raw = obj.get("output_index")
        if raw is None:
            return ""
        idx_key = str(raw)
        return self._function_keys_by_output_index.get(idx_key) or f"output:{idx_key}"

    def _remember_output_index(self, key: str, obj: dict[str, Any]) -> None:
        output_index = obj.get("output_index")
        if output_index is not None and key:
            self._function_keys_by_output_index[str(output_index)] = key

    def _allows_function_name(self, name: str) -> bool:
        name = name.strip()
        return bool(name) and name in self._allowed_function_names

    def _ignore_function_key(self, key: str) -> None:
        if key:
            self._ignored_function_keys.add(key)
        self._forget_function_key(key)

    def _forget_function_key(self, key: str) -> None:
        if not key:
            return
        self._active_tool_index.pop(key, None)
        self._tool_args_buf.pop(key, None)
        for idx, mapped_key in list(self._function_keys_by_output_index.items()):
            if mapped_key == key:
                self._function_keys_by_output_index.pop(idx, None)

    def _register_function_item(
        self,
        item: dict[str, Any],
        event_obj: dict[str, Any],
    ) -> dict[str, Any] | None:
        event_key = self._function_key(event_obj)
        item_key = str(item.get("id") or item.get("call_id") or "").strip()
        key = item_key or event_key
        if not key or key in self._ignored_function_keys:
            return None

        name = str(item.get("name") or "").strip()
        if not self._allows_function_name(name):
            self._ignore_function_key(key)
            if event_key and event_key != key:
                self._ignore_function_key(event_key)
            return None

        if event_key and event_key != key:
            existing = self._tool_args_buf.pop(event_key, None)
            if existing:
                self._tool_args_buf.setdefault(key, []).extend(existing)
            self._forget_function_key(event_key)
        self._remember_output_index(key, event_obj)

        idx = self._active_tool_index.get(key)
        item_id = str(item.get("id") or key)
        call_id = str(item.get("call_id") or item_id)
        arguments = item.get("arguments")
        if not isinstance(arguments, str):
            arguments = "".join(self._tool_args_buf.get(key, []))

        if idx is None:
            idx = len(self.tool_calls)
            self._active_tool_index[key] = idx
            self._tool_args_buf.setdefault(key, [])
            self.tool_calls.append(
                {
                    "id": call_id,
                    "_item_id": item_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": arguments or "",
                    },
                })
        else:
            self.tool_calls[idx]["id"] = call_id
            self.tool_calls[idx]["_item_id"] = item_id
            self.tool_calls[idx]["function"]["name"] = name
            if arguments:
                self.tool_calls[idx]["function"]["arguments"] = arguments
        return {
            "kind": "tool_call_start",
            "index": idx,
            "call_id": call_id,
            "name": name,
        }

    def feed_data(self, data: str) -> dict[str, Any]:
        """Parse one SSE data frame; return the kind/content classification.

        Returns a dict like:
          {"kind": "text", "content": "Two"}
          {"kind": "thinking", "content": "Let me think..."}
          {"kind": "tool_call_start", "index": 0, "call_id": "...", "name": "..."}
          {"kind": "tool_call_args", "index": 0, "delta": "..."}
          {"kind": "tool_call_done", "index": 0}
          {"kind": "annotation", "annotation_data": {...}}
          {"kind": "done"}
          {"kind": "error", "message": "..."}
          {"kind": "skip"}
        """
        if not data or data == "[DONE]":
            return {"kind": "done"}
        try:
            obj = orjson.loads(data)
        except (orjson.JSONDecodeError, ValueError, TypeError):
            return {"kind": "skip"}
        if not isinstance(obj, dict):
            return {"kind": "skip"}

        # Event-specific dispatch (event: line precedes data: line in SSE).
        ev = self._current_event or obj.get("type") or ""

        # ── Text delta ────────────────────────────────────────────────────────
        if ev == "response.output_text.delta" or obj.get("type") == "response.output_text.delta":
            delta = obj.get("delta") or ""
            if isinstance(delta, str) and delta:
                self.text_buf.append(delta)
                return {"kind": "text", "content": delta}
            return {"kind": "skip"}

        # ── Reasoning summary delta (thinking) ────────────────────────────────
        if ev in (
                "response.reasoning_summary_text.delta",
                "response.reasoning_summary.delta",
        ) or obj.get("type") in (
                "response.reasoning_summary_text.delta",
                "response.reasoning_summary.delta",
        ):
            delta = obj.get("delta") or ""
            if isinstance(delta, str) and delta:
                self.thinking_buf.append(delta)
                return {"kind": "thinking", "content": delta}
            return {"kind": "skip"}

        # ── Tool call start (output_item.added with type=function_call) ──────
        if ev == "response.output_item.added" or obj.get("type") == "response.output_item.added":
            item = obj.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "function_call":
                return self._register_function_item(item, obj) or {"kind": "skip"}
            return {"kind": "skip"}

        # ── Web search call done — collect sources ───────────────────────────
        if ev == "response.output_item.done" or obj.get("type") == "response.output_item.done":
            item = obj.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "function_call":
                self._register_function_item(item, obj)
                return {"kind": "skip"}
            if isinstance(item, dict) and item.get("type") == "web_search_call":
                action = item.get("action") or {}
                if isinstance(action, dict):
                    for src in action.get("sources") or []:
                        if not isinstance(src, dict):
                            continue
                        url = src.get("url") or ""
                        if url and url not in self._seen_source_urls:
                            self._seen_source_urls.add(url)
                            self.search_sources.append({
                                "url": url,
                                "title": src.get("title") or "",
                            })
                    if action.get("type") == "open_page":
                        url = action.get("url") or ""
                        if url and url not in self._seen_source_urls:
                            self._seen_source_urls.add(url)
                            self.search_sources.append({
                                "url": url,
                                "title": "",
                            })
            return {"kind": "skip"}

        # ── Tool call argument delta ──────────────────────────────────────────
        if ev == "response.function_call_arguments.delta" or obj.get(
                "type") == "response.function_call_arguments.delta":
            if not self._allowed_function_names:
                return {"kind": "skip"}
            item_id = self._function_key(obj)
            if not item_id or item_id in self._ignored_function_keys:
                return {"kind": "skip"}
            delta = obj.get("delta") or ""
            if not isinstance(delta, str) or not delta:
                return {"kind": "skip"}
            self._remember_output_index(item_id, obj)
            self._tool_args_buf.setdefault(item_id, []).append(delta)
            idx = self._active_tool_index.get(item_id)
            if idx is None:
                return {"kind": "skip"}
            return {"kind": "tool_call_args", "index": idx, "delta": delta}

        # ── Tool call complete ────────────────────────────────────────────────
        if ev == "response.function_call_arguments.done" or obj.get(
                "type") == "response.function_call_arguments.done":
            if not self._allowed_function_names:
                return {"kind": "skip"}
            item_id = self._function_key(obj)
            if not item_id or item_id in self._ignored_function_keys:
                return {"kind": "skip"}
            self._remember_output_index(item_id, obj)
            idx = self._active_tool_index.get(item_id)
            # Prefer upstream-provided final arguments string when present.
            final_args = obj.get("arguments")
            if not isinstance(final_args, str) or not final_args:
                final_args = "".join(self._tool_args_buf.get(item_id, []))
            if idx is None:
                self._tool_args_buf[item_id] = [final_args] if final_args else []
                return {"kind": "skip"}
            self.tool_calls[idx]["function"]["arguments"] = final_args
            return {"kind": "tool_call_done", "index": idx}

        # ── URL citation annotation ───────────────────────────────────────────
        if ev == "response.output_text.annotation.added" or obj.get(
                "type") == "response.output_text.annotation.added":
            ann = obj.get("annotation") or {}
            if isinstance(ann, dict) and ann.get("type") in (None, "url_citation"):
                url = ann.get("url") or ""
                if url:
                    title = ann.get("title") or ""
                    if title == url:
                        # Multi-agent often duplicates URL into title; clean it.
                        title = ""
                    record = {
                        "url": url,
                        "title": title,
                        "start_index": int(ann.get("start_index") or 0),
                        "end_index": int(ann.get("end_index") or 0),
                    }
                    self.annotations.append(record)
                    # Fallback for multi-agent: harvest citation URL into
                    # search_sources too. Dedupe against web_search_call
                    # sources to avoid duplicating single-agent entries.
                    if url not in self._seen_source_urls:
                        self._seen_source_urls.add(url)
                        self.search_sources.append({
                            "url": url,
                            "title": title,
                        })
                    return {"kind": "annotation", "annotation_data": record}
            return {"kind": "skip"}

        # ── Final completion frame — capture usage for accounting ────────────
        if ev == "response.completed" or obj.get("type") == "response.completed":
            resp = obj.get("response") or obj
            usage = resp.get("usage") or {}
            if usage:
                self._usage = {
                    "prompt_tokens": int(usage.get("input_tokens") or 0),
                    "completion_tokens": int(usage.get("output_tokens") or 0),
                    "total_tokens": int(usage.get("total_tokens") or 0),
                    "reasoning_tokens": int(
                        (usage.get("output_tokens_details") or {}).get("reasoning_tokens") or
                        usage.get("reasoning_tokens") or 0),
                }
            output = resp.get("output") if isinstance(resp, dict) else None
            if isinstance(output, list):
                for item in output:
                    if isinstance(item, dict) and item.get("type") == "function_call":
                        self._register_function_item(item, {})
                if not self.text_buf:
                    text = _response_output_text(output)
                    if text:
                        self.text_buf.append(text)
            return {"kind": "done"}

        # ── Error frames ──────────────────────────────────────────────────────
        if ev in ("response.failed", "response.error", "error") or obj.get("type") in (
                "response.failed",
                "response.error",
                "error",
        ):
            err = obj.get("error") or obj.get("response", {}).get("error") or {}
            if isinstance(err, dict):
                msg = err.get("message") or err.get("code") or "Console stream error"
            else:
                msg = str(err) or "Console stream error"
            return {"kind": "error", "message": str(msg)}

        return {"kind": "skip"}

    @property
    def full_text(self) -> str:
        return "".join(self.text_buf)

    @property
    def function_call_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for call in self.tool_calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            if not isinstance(fn, dict):
                continue
            name = str(fn.get("name") or "").strip()
            if not self._allows_function_name(name):
                continue
            call_id = str(call.get("id") or "").strip()
            if not call_id:
                continue
            item_id = str(call.get("_item_id") or call_id)
            items.append({
                "id": item_id,
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": str(fn.get("arguments") or "{}"),
                "status": "completed",
            })
        return items

    @property
    def parsed_tool_calls(self) -> list[ParsedToolCall]:
        return [
            ParsedToolCall(
                call_id=str(item.get("call_id") or item.get("id")),
                name=str(item["name"]),
                arguments=str(item.get("arguments") or "{}"),
            )
            for item in self.function_call_items
        ]

    @property
    def usage(self) -> dict[str, int]:
        """Return collected usage tokens (populated after stream completion)."""
        return dict(self._usage)


def _response_output_text(output: list[Any]) -> str:
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if isinstance(content, str):
            if content:
                parts.append(content)
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in ("output_text", "text", "input_text"):
                text = part.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
    return "".join(parts)


__all__ = [
    "build_console_input",
    "build_console_payload",
    "console_model_supports_reasoning_effort",
    "client_function_tool_names",
    "convert_openai_tools_to_console",
    "convert_openai_tool_choice",
    "inject_web_search_tool",
    "extract_console_text",
    "extract_console_reasoning",
    "extract_console_tool_calls",
    "extract_console_annotations",
    "extract_console_search_sources",
    "extract_console_usage",
    "format_search_sources_suffix",
    "parse_console_error",
    "classify_console_sse_line",
    "ConsoleStreamAdapter",
]
