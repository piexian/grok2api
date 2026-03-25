"""
OpenAI-compatible usage estimation and format conversion helpers.

Notes:
- The Grok reverse-engineered web path does not reliably expose per-request
  prompt/completion token statistics.
- These helpers provide a lightweight local estimate so OpenAI-compatible
  clients do not always receive zeros.
- The estimate is for compatibility and basic reporting, not billing accuracy.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Optional, Sequence, Tuple

import orjson


_TOKEN_SEGMENT_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_PROMPT_OVERHEAD_TOKENS = 4
_SIZE_RE = re.compile(r"^\s*(\d+)\s*x\s*(\d+)\s*$", re.IGNORECASE)
_BASE64_BODY_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")
_IMAGE_PIXELS_PER_TOKEN = 1024
_IMAGE_BYTES_PER_TOKEN = 512
_VIDEO_PIXEL_SECONDS_PER_TOKEN = 4096
_VIDEO_DIMENSIONS = {
    "480p": {
        "16:9": (854, 480),
        "9:16": (480, 854),
        "3:2": (720, 480),
        "2:3": (480, 720),
        "1:1": (480, 480),
    },
    "720p": {
        "16:9": (1280, 720),
        "9:16": (720, 1280),
        "3:2": (1080, 720),
        "2:3": (720, 1080),
        "1:1": (720, 720),
    },
}


def _compact_json(value: Any) -> str:
    return orjson.dumps(
        value,
        option=orjson.OPT_SORT_KEYS | orjson.OPT_NON_STR_KEYS,
    ).decode("utf-8")


def estimate_tokens(value: Any) -> int:
    """
    Estimate tokens for arbitrary payloads with a conservative heuristic.

    The heuristic avoids returning zero for non-empty content:
    - utf-8 byte length / 4
    - text segment count * 0.75
    The larger estimate wins.
    """
    if value is None:
        return 0

    if isinstance(value, (bytes, bytearray)):
        if not value:
            return 0
        return max(1, math.ceil(len(value) / 4))

    if not isinstance(value, str):
        try:
            value = _compact_json(value)
        except Exception:
            value = str(value)

    text = value.strip()
    if not text:
        return 0

    byte_estimate = math.ceil(len(text.encode("utf-8")) / 4)
    segment_estimate = math.ceil(len(_TOKEN_SEGMENT_RE.findall(text)) * 0.75)
    return max(1, byte_estimate, segment_estimate)


def estimate_prompt_tokens(prompt_text: str) -> int:
    if not prompt_text or not prompt_text.strip():
        return 0
    return estimate_tokens(prompt_text) + _PROMPT_OVERHEAD_TOKENS


def estimate_completion_tokens(
    *,
    content: Optional[str] = None,
    tool_calls: Optional[list[dict[str, Any]]] = None,
) -> int:
    completion_tokens = estimate_tokens(content)
    if tool_calls:
        completion_tokens += estimate_tokens(tool_calls)
    return completion_tokens


def _parse_size(size: Optional[str]) -> Optional[Tuple[int, int]]:
    if not isinstance(size, str):
        return None
    match = _SIZE_RE.match(size)
    if not match:
        return None
    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    return width, height


def _estimate_base64_bytes(value: Any) -> int:
    if not isinstance(value, str):
        return 0

    candidate = value.strip()
    if not candidate:
        return 0

    if candidate.startswith(("http://", "https://", "/")):
        return 0

    if candidate.startswith("data:"):
        parts = candidate.split(",", 1)
        if len(parts) != 2 or "base64" not in parts[0].lower():
            return 0
        candidate = parts[1]

    candidate = "".join(candidate.split())
    if len(candidate) < 64 or len(candidate) % 4 != 0:
        return 0
    if not _BASE64_BODY_RE.fullmatch(candidate):
        return 0

    padding = len(candidate) - len(candidate.rstrip("="))
    return max(0, (len(candidate) * 3) // 4 - padding)


def estimate_image_tokens_from_size(size: Optional[str]) -> int:
    dims = _parse_size(size)
    if not dims:
        return 0
    width, height = dims
    return max(1, math.ceil((width * height) / _IMAGE_PIXELS_PER_TOKEN))


def estimate_image_tokens_from_base64(value: Any) -> int:
    byte_len = _estimate_base64_bytes(value)
    if byte_len <= 0:
        return 0
    return max(1, math.ceil(byte_len / _IMAGE_BYTES_PER_TOKEN))


def estimate_image_reference_tokens(
    references: Optional[Sequence[str]],
    *,
    fallback_size: Optional[str] = None,
) -> int:
    total = 0
    for reference in references or ():
        token_estimate = estimate_image_tokens_from_base64(reference)
        if token_estimate <= 0:
            token_estimate = estimate_image_tokens_from_size(fallback_size)
        total += max(0, token_estimate)
    return total


def estimate_image_output_tokens(
    outputs: Optional[Sequence[str]],
    *,
    response_format: str = "url",
    size: Optional[str] = None,
) -> int:
    total = 0
    size_tokens = estimate_image_tokens_from_size(size)
    for output in outputs or ():
        token_estimate = 0
        if response_format != "url":
            token_estimate = estimate_image_tokens_from_base64(output)
        total += max(size_tokens, token_estimate)
    return total


def estimate_video_tokens(
    *,
    seconds: Optional[int],
    resolution: Optional[str],
    aspect_ratio: Optional[str] = None,
    size: Optional[str] = None,
) -> int:
    pixels = 0
    dims = _parse_size(size)
    if dims:
        width, height = dims
        pixels = width * height
    else:
        width_height = (
            (_VIDEO_DIMENSIONS.get(str(resolution or "").strip()) or {}).get(
                str(aspect_ratio or "").strip()
            )
        )
        if width_height:
            width, height = width_height
            pixels = width * height

    duration = max(0, int(seconds or 0))
    if pixels <= 0 or duration <= 0:
        return 0
    return max(1, math.ceil((pixels * duration) / _VIDEO_PIXEL_SECONDS_PER_TOKEN))


def _collect_message_text_and_images(
    messages: Optional[Sequence[Dict[str, Any]]],
) -> Tuple[str, list[str]]:
    text_parts: list[str] = []
    image_refs: list[str] = []

    for message in messages or ():
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            if content.strip():
                text_parts.append(content.strip())
            continue
        if isinstance(content, dict):
            content = [content]
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").strip().lower()
            if block_type in {"text", "input_text"}:
                text = block.get("text") or block.get("input_text") or ""
                if isinstance(text, str) and text.strip():
                    text_parts.append(text.strip())
                continue
            if block_type not in {"image_url", "input_image", "image", "output_image"}:
                continue

            image_value = block.get("image_url")
            if isinstance(image_value, dict):
                image_value = image_value.get("url") or image_value.get("image_url")
            if not image_value:
                image_value = block.get("url")
            if isinstance(image_value, str) and image_value.strip():
                image_refs.append(image_value.strip())

    return "\n".join(text_parts), image_refs


def build_media_usage(
    *,
    input_text_tokens: int = 0,
    input_image_tokens: int = 0,
    output_text_tokens: int = 0,
    output_image_tokens: int = 0,
    output_video_tokens: int = 0,
) -> Dict[str, Any]:
    input_text_tokens = max(0, int(input_text_tokens or 0))
    input_image_tokens = max(0, int(input_image_tokens or 0))
    output_text_tokens = max(0, int(output_text_tokens or 0))
    output_image_tokens = max(0, int(output_image_tokens or 0))
    output_video_tokens = max(0, int(output_video_tokens or 0))

    prompt_tokens = input_text_tokens + input_image_tokens
    completion_tokens = output_text_tokens + output_image_tokens + output_video_tokens
    total_tokens = prompt_tokens + completion_tokens

    prompt_details = {
        "cached_tokens": 0,
        "text_tokens": input_text_tokens,
        "audio_tokens": 0,
        "image_tokens": input_image_tokens,
    }
    completion_details = {
        "text_tokens": output_text_tokens,
        "audio_tokens": 0,
        "reasoning_tokens": 0,
        "image_tokens": output_image_tokens,
        "video_tokens": output_video_tokens,
    }

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "prompt_tokens_details": prompt_details,
        "completion_tokens_details": completion_details,
        "input_tokens_details": prompt_details,
        "output_tokens_details": completion_details,
    }


def estimate_image_usage(
    *,
    prompt_text: str,
    outputs: Optional[Sequence[str]],
    response_format: str,
    size: Optional[str],
    input_images: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    return build_media_usage(
        input_text_tokens=estimate_prompt_tokens(prompt_text),
        input_image_tokens=estimate_image_reference_tokens(input_images),
        output_image_tokens=estimate_image_output_tokens(
            outputs,
            response_format=response_format,
            size=size,
        ),
    )


def estimate_video_usage(
    *,
    messages: Optional[Sequence[Dict[str, Any]]] = None,
    content: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    seconds: Optional[int] = None,
    resolution: Optional[str] = None,
    size: Optional[str] = None,
) -> Dict[str, Any]:
    prompt_text, image_refs = _collect_message_text_and_images(messages)
    return build_media_usage(
        input_text_tokens=estimate_prompt_tokens(prompt_text),
        input_image_tokens=estimate_image_reference_tokens(image_refs),
        output_text_tokens=estimate_tokens(content),
        output_video_tokens=estimate_video_tokens(
            seconds=seconds,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            size=size,
        ),
    )


def build_chat_usage(prompt_tokens: int, completion_tokens: int) -> Dict[str, Any]:
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    total_tokens = prompt_tokens + completion_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "prompt_tokens_details": {
            "cached_tokens": 0,
            "text_tokens": prompt_tokens,
            "audio_tokens": 0,
            "image_tokens": 0,
        },
        "completion_tokens_details": {
            "text_tokens": completion_tokens,
            "audio_tokens": 0,
            "reasoning_tokens": 0,
        },
    }


def estimate_chat_usage(
    *,
    prompt_tokens: int,
    content: Optional[str] = None,
    tool_calls: Optional[list[dict[str, Any]]] = None,
) -> Dict[str, Any]:
    completion_tokens = estimate_completion_tokens(
        content=content,
        tool_calls=tool_calls,
    )
    return build_chat_usage(prompt_tokens, completion_tokens)


def normalize_chat_usage(usage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not usage:
        return build_chat_usage(0, 0)

    prompt_tokens = usage.get("prompt_tokens")
    if prompt_tokens is None:
        prompt_tokens = usage.get("input_tokens", 0)

    completion_tokens = usage.get("completion_tokens")
    if completion_tokens is None:
        completion_tokens = usage.get("output_tokens", 0)

    return build_chat_usage(prompt_tokens, completion_tokens)


def to_responses_usage(usage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    chat_usage = normalize_chat_usage(usage)
    prompt_tokens = chat_usage["prompt_tokens"]
    completion_tokens = chat_usage["completion_tokens"]
    total_tokens = chat_usage["total_tokens"]
    prompt_details = chat_usage.get("prompt_tokens_details") or {}

    return {
        "input_tokens": prompt_tokens,
        "input_tokens_details": {
            "cached_tokens": int(prompt_details.get("cached_tokens") or 0),
            "text_tokens": int(prompt_details.get("text_tokens") or prompt_tokens),
            "image_tokens": int(prompt_details.get("image_tokens") or 0),
        },
        "output_tokens": completion_tokens,
        "output_tokens_details": {
            "text_tokens": int(completion_tokens),
            "reasoning_tokens": int(
                (chat_usage.get("completion_tokens_details") or {}).get(
                    "reasoning_tokens"
                )
                or 0
            ),
        },
        "total_tokens": total_tokens,
    }


__all__ = [
    "build_media_usage",
    "build_chat_usage",
    "estimate_image_output_tokens",
    "estimate_image_reference_tokens",
    "estimate_image_tokens_from_base64",
    "estimate_image_tokens_from_size",
    "estimate_image_usage",
    "estimate_chat_usage",
    "estimate_completion_tokens",
    "estimate_prompt_tokens",
    "estimate_tokens",
    "estimate_video_tokens",
    "estimate_video_usage",
    "normalize_chat_usage",
    "to_responses_usage",
]
