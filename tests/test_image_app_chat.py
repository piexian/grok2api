import asyncio
from types import SimpleNamespace

from app.api.v1.chat import MessageItem, _extract_prompt_images
from app.services.grok.services.image import (
    ImageGenerationResult,
    ImageGenerationService,
)
from app.services.reverse.app_chat import AppChatReverse


def test_extract_prompt_images_collects_markdown_and_dedupes():
    prompt, image_urls = _extract_prompt_images(
        [
            MessageItem(
                role="assistant",
                content="Here is one ![img](https://example.com/a.png) and duplicate ![img](https://example.com/a.png)",
            ),
            MessageItem(
                role="user",
                content=[
                    {
                        "type": "text",
                        "text": "Edit this ![img](https://example.com/b.png)",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/c.png"},
                    },
                ],
            ),
        ]
    )

    assert prompt == "Edit this ![img](https://example.com/b.png)"
    assert image_urls == [
        "https://example.com/a.png",
        "https://example.com/b.png",
        "https://example.com/c.png",
    ]


def test_app_chat_build_payload_applies_request_overrides(monkeypatch):
    monkeypatch.setattr(
        "app.services.reverse.app_chat.get_config",
        lambda key, default=None: (
            False if key in {"app.disable_memory", "app.temporary"} else ""
        ),
    )

    payload = AppChatReverse.build_payload(
        message="draw a cat",
        model="grok-3",
        mode="MODEL_MODE_FAST",
        request_overrides={"imageGenerationCount": 1, "enableNsfw": True},
    )

    assert payload["imageGenerationCount"] == 1
    assert payload["enableNsfw"] is True
    assert payload["modelName"] == "grok-3"


def test_generate_non_stream_uses_app_chat_path(monkeypatch):
    async def _run():
        token_mgr = SimpleNamespace()

        async def _reload_if_stale():
            return None

        async def _consume(token, effort):
            return None

        token_mgr.reload_if_stale = _reload_if_stale
        token_mgr.consume = _consume

        model_info = SimpleNamespace(
            model_id="grok-imagine-1.0",
            grok_model="grok-3",
            model_mode="MODEL_MODE_FAST",
            cost=SimpleNamespace(value="high"),
        )

        service = ImageGenerationService()
        called = {"app": 0, "ws": 0}

        async def fake_pick_token(*args, **kwargs):
            return "tok_test"

        async def fake_collect_app_chat(**kwargs):
            called["app"] += 1
            return ImageGenerationResult(
                stream=False,
                data=["https://example.com/image.png"],
                usage_override={
                    "total_tokens": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
            )

        async def fake_collect_ws(**kwargs):
            called["ws"] += 1
            raise AssertionError("ws path should not be used")

        monkeypatch.setattr(
            "app.services.grok.services.image.get_config",
            lambda key, default=None: 3 if key == "retry.max_retry" else False,
        )
        monkeypatch.setattr(
            "app.services.grok.services.image.pick_token",
            fake_pick_token,
        )
        monkeypatch.setattr(service, "_collect_app_chat", fake_collect_app_chat)
        monkeypatch.setattr(service, "_collect_ws", fake_collect_ws)

        result = await service.generate(
            token_mgr=token_mgr,
            token="tok_test",
            model_info=model_info,
            prompt="draw a cat",
            n=1,
            response_format="url",
            size="1024x1024",
            aspect_ratio="1:1",
            stream=False,
            enable_nsfw=False,
        )

        assert result.data == ["https://example.com/image.png"]
        assert called == {"app": 1, "ws": 0}

    asyncio.run(_run())
