import asyncio
from types import SimpleNamespace

from app.api.v1.function.video import _collect_image_urls
from app.api.v1.video import _parse_image_references
from app.core.exceptions import ValidationException
from app.services.grok.services.video import (
    VideoRoundPlan,
    VideoRoundResult,
    VideoService,
    _replace_reference_placeholders,
    _strip_reference_placeholders,
)


def test_parse_image_references_supports_array_and_legacy_formats():
    assert _parse_image_references({"image_url": "https://example.com/legacy.png"}) == [
        "https://example.com/legacy.png"
    ]
    assert _parse_image_references(
        [
            {
                "type": "image_url",
                "image_url": {"url": "https://example.com/first.png"},
            },
            "data:image/png;base64,abc",
        ]
    ) == [
        "https://example.com/first.png",
        "data:image/png;base64,abc",
    ]


def test_collect_image_urls_supports_legacy_and_new_fields():
    assert _collect_image_urls(
        "https://example.com/legacy.png",
        [
            "https://example.com/first.png",
            "https://example.com/legacy.png",
            "",
        ],
    ) == [
        "https://example.com/first.png",
        "https://example.com/legacy.png",
    ]


def test_replace_reference_placeholders_uses_uploaded_asset_ids():
    assert (
        _replace_reference_placeholders(
            "@图1 cat and @image2 dog running",
            ["asset_one", "asset_two"],
        )
        == "@asset_one cat and @asset_two dog running"
    )
    assert (
        _strip_reference_placeholders("@图1 cat and @image2 dog running")
        == "cat and dog running"
    )

    try:
        _replace_reference_placeholders("@图3 missing", ["asset_one"])
    except ValidationException as exc:
        assert "no matching uploaded image" in str(exc)
    else:
        assert False, "expected placeholder validation error"


def test_video_completions_multi_reference_uses_first_round_reference_config(
    monkeypatch,
):
    captured = {}

    class _TokenManager:
        def __init__(self):
            self.consumed = []

        async def reload_if_stale(self):
            return None

        def get_token_for_video(self, resolution, video_length, pool_candidates):
            return SimpleNamespace(token="sso=tok_test")

        def get_pool_name_for_token(self, token):
            return "ssoBasic"

        async def consume(self, token, effort):
            self.consumed.append((token, effort.value))

        async def mark_rate_limited(self, *args, **kwargs):
            raise AssertionError("rate limiting should not happen in this test")

    token_mgr = _TokenManager()

    async def fake_get_token_manager():
        return token_mgr

    class _UploadService:
        counter = 0

        async def upload_file(self, attach_data, token):
            _UploadService.counter += 1
            idx = _UploadService.counter
            return f"asset_{idx}", f"files/ref_{idx}.png"

        async def close(self):
            return None

    async def fake_create_post(
        self,
        token,
        prompt,
        media_type="MEDIA_POST_TYPE_VIDEO",
        media_url=None,
    ):
        captured["seed_prompt"] = prompt
        captured["media_type"] = media_type
        captured["media_url"] = media_url
        return "seed_post_id"

    async def fake_request_round_stream(
        *,
        token,
        message,
        model_config_override,
        file_attachments=None,
    ):
        captured["token"] = token
        captured["message"] = message
        captured["config"] = model_config_override
        captured["file_attachments"] = file_attachments
        return object()

    async def fake_collect_round_result(response, *, model, source):
        return VideoRoundResult(
            response_id="resp_multi_ref",
            post_id="12345678-1234-1234-1234-1234567890ab",
            video_url="https://cdn.example.com/generated/12345678-1234-1234-1234-1234567890ab/video.mp4",
            thumbnail_url="https://cdn.example.com/thumb.jpg",
        )

    class _DownloadService:
        async def render_video(self, video_url, token, thumbnail_url=None):
            return f"[video]({video_url})"

        async def close(self):
            return None

    config_values = {
        "app.stream": False,
        "app.thinking": False,
        "proxy.browser": None,
        "video.concurrent": 1,
        "video.enable_public_asset": False,
        "video.upscale_timing": "complete",
    }

    monkeypatch.setattr(
        "app.services.grok.services.video.get_token_manager",
        fake_get_token_manager,
    )
    monkeypatch.setattr(
        "app.services.grok.services.model.ModelService.pool_candidates_for_model",
        lambda model: ["ssoBasic"],
    )
    monkeypatch.setattr(
        "app.services.grok.services.model.ModelService.get",
        lambda model: SimpleNamespace(cost=SimpleNamespace(value="low")),
    )
    monkeypatch.setattr(
        "app.services.grok.utils.upload.UploadService",
        _UploadService,
    )
    monkeypatch.setattr(
        "app.services.grok.services.video.VideoService.create_post",
        fake_create_post,
    )
    monkeypatch.setattr(
        "app.services.grok.services.video._request_round_stream",
        fake_request_round_stream,
    )
    monkeypatch.setattr(
        "app.services.grok.services.video._collect_round_result",
        fake_collect_round_result,
    )
    monkeypatch.setattr(
        "app.services.grok.services.video.DownloadService",
        _DownloadService,
    )
    monkeypatch.setattr(
        "app.services.grok.services.video._build_round_plan",
        lambda target_length, *, is_super=False: [
            VideoRoundPlan(
                round_index=1,
                total_rounds=1,
                is_extension=False,
                video_length=6,
            )
        ],
    )
    monkeypatch.setattr(
        "app.services.grok.services.video.get_config",
        lambda key, default=None: config_values.get(key, default),
    )

    async def _run():
        result = await VideoService.completions(
            model="grok-imagine-1.0-video",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "@图1 cat and @图2 dog running"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/one.png"},
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/two.png"},
                        },
                    ],
                }
            ],
            stream=False,
            aspect_ratio="3:2",
            video_length=6,
            resolution="480p",
            preset="custom",
        )

        assert captured["seed_prompt"] == "@asset_1 cat and @asset_2 dog running"
        assert captured["message"].startswith(
            "@asset_1 cat and @asset_2 dog running --mode=custom"
        )
        assert captured["file_attachments"] == ["asset_1", "asset_2"]
        assert captured["config"]["modelMap"]["videoGenModelConfig"] == {
            "aspectRatio": "3:2",
            "parentPostId": "seed_post_id",
            "resolutionName": "480p",
            "videoLength": 6,
            "imageReferences": [
                "https://assets.grok.com/files/ref_1.png",
                "https://assets.grok.com/files/ref_2.png",
            ],
            "isReferenceToVideo": True,
        }
        assert result["choices"][0]["message"]["content"].startswith("[video](")
        assert token_mgr.consumed == [("tok_test", "low")]

    asyncio.run(_run())
