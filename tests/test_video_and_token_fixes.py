import asyncio

import orjson

from app.core.exceptions import UpstreamException
import app.services.reverse.utils.retry as retry_mod
import app.services.token.manager as manager_mod
from app.services.grok.services.video import _generate_continuation_prompt
from app.services.token.manager import TokenManager
from app.services.token.models import TokenInfo, TokenStatus
from app.services.token.pool import TokenPool


def _json_line(payload: dict) -> bytes:
    return orjson.dumps(payload)


def test_generate_continuation_prompt_uses_app_chat_response(monkeypatch):
    class _Session:
        async def close(self):
            return None

    async def fake_request(session, token, message, model, mode=None, **kwargs):
        assert token == "tok_test"
        assert model == "grok-3"
        assert mode == "MODEL_MODE_FAST"
        assert "Extension round: 2" in message

        async def _gen():
            yield _json_line({"result": {"response": {"token": "Pan"}}})
            yield _json_line(
                {
                    "result": {
                        "response": {
                            "modelResponse": {
                                "message": "Pan right as the subject walks into a brighter street"
                            }
                        }
                    }
                }
            )

        return _gen()

    monkeypatch.setattr(
        "app.services.grok.services.video._new_session",
        lambda: _Session(),
    )
    monkeypatch.setattr(
        "app.services.grok.services.video.AppChatReverse.request",
        fake_request,
    )
    monkeypatch.setattr(
        "app.services.grok.services.video.get_config",
        lambda key, default=None: 1 if key == "video.concurrent" else default,
    )

    async def _run():
        prompt = await _generate_continuation_prompt(
            "A cat runs down an alley", 2, "tok_test"
        )
        assert prompt == "Pan right as the subject walks into a brighter street"

    asyncio.run(_run())


def test_generate_continuation_prompt_falls_back_on_error(monkeypatch):
    class _Session:
        async def close(self):
            return None

    async def fake_request(*args, **kwargs):
        raise UpstreamException("boom", status_code=502)

    monkeypatch.setattr(
        "app.services.grok.services.video._new_session",
        lambda: _Session(),
    )
    monkeypatch.setattr(
        "app.services.grok.services.video.AppChatReverse.request",
        fake_request,
    )
    monkeypatch.setattr(
        "app.services.grok.services.video.get_config",
        lambda key, default=None: 1 if key == "video.concurrent" else default,
    )

    async def _run():
        prompt = await _generate_continuation_prompt(
            "A cat runs down an alley", 3, "tok_test"
        )
        assert prompt == "A cat runs down an alley"

    asyncio.run(_run())


def test_record_fail_removes_email_domain_rejected_token(monkeypatch):
    mgr = TokenManager()
    pool = TokenPool("ssoBasic")
    pool.add(TokenInfo(token="tok_test"))
    mgr.pools["ssoBasic"] = pool

    saved = {"called": False}

    async def fake_save(force=False):
        saved["called"] = force

    monkeypatch.setattr(mgr, "_save", fake_save)

    async def _run():
        removed = await mgr.record_fail(
            "tok_test",
            400,
            "email-domain-rejected",
        )
        assert removed is True
        assert mgr.pools["ssoBasic"].get("tok_test") is None
        assert "tok_test" in mgr._dirty_deletes
        assert saved["called"] is True

    asyncio.run(_run())


def test_refresh_cooling_tokens_removes_email_domain_rejected_token(monkeypatch):
    mgr = TokenManager()
    pool = TokenPool("ssoBasic")
    pool.add(TokenInfo(token="tok_refresh", status=TokenStatus.COOLING, quota=0))
    mgr.pools["ssoBasic"] = pool
    mgr.initialized = True

    async def fake_save(force=False):
        return None

    class _UsageService:
        async def get_multi(self, token_str):
            raise UpstreamException(
                "refresh failed",
                details={"status": 400, "body": "email-domain-rejected"},
            )

    retry_defaults = {
        "retry.max_retry": 3,
        "retry.retry_status_codes": [400, 401, 403, 429, 500, 502, 503, 504],
        "retry.retry_budget": 30,
        "retry.retry_backoff_base": 0.1,
        "retry.retry_backoff_factor": 2,
        "retry.retry_backoff_max": 1,
    }

    monkeypatch.setattr(mgr, "_save", fake_save)
    monkeypatch.setattr(manager_mod, "UsageService", _UsageService)
    monkeypatch.setattr(
        retry_mod,
        "get_config",
        lambda key, default=None: retry_defaults.get(key, default),
    )

    async def _run():
        result = await mgr.refresh_cooling_tokens(trigger="manual")
        assert result["expired"] == 1
        assert mgr.get_pool_name_for_token("tok_refresh") is None
        assert "tok_refresh" in mgr._dirty_deletes
        assert "tok_refresh" not in mgr._dirty_tokens

    asyncio.run(_run())
