"""OpenAI-compatible request schemas (Pydantic models)."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class MessageItem(BaseModel):
    role:         str
    content:      str | list[dict[str, Any]] | None = None
    tool_calls:   list[dict[str, Any]] | None       = None
    tool_call_id: str | None                        = None
    name:         str | None                        = None


class ImageConfig(BaseModel):
    n:               int | None = Field(1, ge=1, le=10)
    size:            str | None = "1024x1024"
    aspect_ratio:    str | None = None
    resolution:      Literal["1k", "2k"] | None = None
    response_format: str | None = None
    quality:         str | None = None
    output_format:   Literal["png", "jpeg", "webp"] | None = None
    output_compression: int | None = Field(None, ge=0, le=100)
    background:      str | None = None
    moderation:      str | None = None


class VideoConfig(BaseModel):
    seconds: int | None = 6
    size: Literal["720x1280", "1280x720", "1024x1024", "1024x1792", "1792x1024"] | None = "720x1280"
    resolution_name: Literal["480p", "720p"] | None = None
    preset: Literal["fun", "normal", "spicy", "custom"] | None = None


class ChatCompletionRequest(BaseModel):
    model:               str
    messages:            list[MessageItem]
    stream:              bool | None                = None
    reasoning_effort:    str | None                 = None
    temperature:         float | None               = 0.8
    top_p:               float | None               = 0.95
    response_format:     str | dict[str, Any] | None = None
    image_config:        ImageConfig | None         = None
    video_config:        VideoConfig | None         = None
    tools:               list[dict[str, Any]] | None = None
    tool_choice:         str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None                = True
    max_tokens:          int | None                 = None
    max_completion_tokens: int | None               = None
    metadata:            dict[str, Any] | None      = None
    service_tier:        str | None                 = None
    store:               bool | None                = None
    stream_options:      dict[str, Any] | None      = None
    user:                str | None                 = None
    stop:                str | list[str] | None     = None
    n:                   int | None                 = None
    logprobs:            bool | None                = None
    top_logprobs:        int | None                 = None
    seed:                int | None                 = None
    presence_penalty:    float | None               = None
    frequency_penalty:   float | None               = None


class ImageGenerationRequest(BaseModel):
    model:           str
    prompt:          str
    n:               int | None = Field(1, ge=1, le=10)
    size:            str | None = "1024x1024"
    aspect_ratio:    str | None = None
    resolution:      Literal["1k", "2k"] | None = None
    response_format: str | None = None
    quality:         str | None = None
    output_format:   Literal["png", "jpeg", "webp"] | None = None
    output_compression: int | None = Field(None, ge=0, le=100)
    background:      str | None = None
    moderation:      str | None = None
    stream:          bool | None = None
    partial_images:  int | None = Field(None, ge=0, le=3)
    user:            str | None = None


class ImageEditRequest(BaseModel):
    model:           str
    prompt:          str
    image:           str | dict[str, Any] | list[str] | list[dict[str, Any]]
    mask:            str | None = None
    n:               int | None = Field(1, ge=1, le=2)
    size:            str | None = "1024x1024"
    aspect_ratio:    str | None = None
    resolution:      Literal["1k", "2k"] | None = None
    response_format: str | None = None
    quality:         str | None = None
    output_format:   Literal["png", "jpeg", "webp"] | None = None
    output_compression: int | None = Field(None, ge=0, le=100)
    background:      str | None = None
    moderation:      str | None = None
    user:            str | None = None


class ResponsesCreateRequest(BaseModel):
    """OpenAI Responses API — /v1/responses.

    model/input/instructions/stream/reasoning/temperature/top_p are acted on.
    tools/tool_choice are supported for tool-capable routes; unsupported fields
    are accepted and silently discarded.
    """
    model:                str
    input:                str | list[Any]
    instructions:         str | None           = None
    stream:               bool | None          = None
    reasoning:            dict[str, Any] | None = None
    temperature:          float | None         = None
    top_p:                float | None         = None
    tools:                list[Any] | None      = None
    tool_choice:          Any | None            = None
    # compatibility fields
    max_output_tokens:    int | None            = None
    previous_response_id: str | None            = None
    store:                bool | None           = None
    metadata:             dict[str, Any] | None = None
    truncation:           str | None            = None
    parallel_tool_calls:  bool | None           = None
    include:              list[str] | None      = None
    background:           bool | None           = None
    frequency_penalty:    float | None          = None
    max_tool_calls:       int | None            = None
    presence_penalty:     float | None          = None
    prompt_cache_key:     str | None            = None
    safety_identifier:    str | None            = None
    service_tier:         str | None            = None
    text:                 dict[str, Any] | None = None
    top_logprobs:         int | None            = None
    user:                 str | None            = None

    class Config:
        extra = "ignore"


__all__ = [
    "MessageItem", "ImageConfig", "VideoConfig",
    "ChatCompletionRequest", "ImageGenerationRequest", "ImageEditRequest",
    "ResponsesCreateRequest",
]
