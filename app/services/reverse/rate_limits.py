"""
Reverse interface: rate limits.
"""

import asyncio
import orjson
from typing import Any, Dict, List, Optional
from curl_cffi.requests import AsyncSession

from app.core.logger import logger
from app.core.config import get_config
from app.core.proxy_pool import (
    build_http_proxies,
    get_current_proxy_from,
    rotate_proxy,
    should_rotate_proxy,
)
from app.core.exceptions import UpstreamException
from app.services.reverse.utils.headers import build_headers
from app.services.reverse.utils.retry import retry_on_status

RATE_LIMITS_API = "https://grok.com/rest/rate-limits"

# 真实桶（可信，可作为账本）
BUCKET_MODELS = ["grok-3", "grok-4"]

# 探针桶（不可信，仅参考）
PROBE_MODELS = ["grok-4-1-thinking-1129", "grok-420"]

# 所有需要查询的模型
ALL_MODELS = BUCKET_MODELS + PROBE_MODELS


class RateLimitsReverse:
    """/rest/rate-limits reverse interface."""

    @staticmethod
    async def request(
        session: AsyncSession,
        token: str,
        model_name: str = "grok-3",
    ) -> Any:
        """Fetch rate limits from Grok for a specific model.

        Args:
            session: AsyncSession, the session to use for the request.
            token: str, the SSO token.
            model_name: str, the model name to query rate limits for.

        Returns:
            Any: The response from the request.
        """
        try:
            # Build headers
            headers = build_headers(
                cookie_token=token,
                content_type="application/json",
                origin="https://grok.com",
                referer="https://grok.com/",
            )

            # Build payload
            payload = {
                "requestKind": "DEFAULT",
                "modelName": model_name,
            }

            # Curl Config
            timeout = get_config("usage.timeout")
            browser = get_config("proxy.browser")
            active_proxy_key = None

            async def _do_request():
                nonlocal active_proxy_key
                active_proxy_key, proxy_url = get_current_proxy_from(
                    "proxy.base_proxy_url"
                )
                proxies = build_http_proxies(proxy_url)
                response = await session.post(
                    RATE_LIMITS_API,
                    headers=headers,
                    data=orjson.dumps(payload),
                    timeout=timeout,
                    proxies=proxies,
                    impersonate=browser,
                )

                if response.status_code != 200:
                    try:
                        resp_text = response.text
                    except Exception:
                        resp_text = "N/A"

                    # --- 识别逻辑开始 ---
                    # 区分是真正的 Token 过期还是 Cloudflare 拦截
                    is_token_expired = False
                    server_header = response.headers.get("Server", "").lower()
                    content_type = response.headers.get("Content-Type", "").lower()

                    # 1. 只有当返回不是 JSON 且包含 cloudflare 关键字，或者包含特定的 challenge 标志时，才认为是网络拦截
                    is_cloudflare = "challenge-platform" in resp_text
                    if (
                        "cloudflare" in server_header
                        and "application/json" not in content_type
                    ):
                        is_cloudflare = True

                    # 2. 如果是 401 且返回 JSON 内容包含认证失败关键字，则确认为 Token 过期
                    if (
                        response.status_code == 401
                        and "application/json" in content_type
                    ):
                        # 增加 unauthenticated 和 bad-credentials 等更精确的关键字
                        body_lower = resp_text.lower()
                        auth_error_keywords = [
                            "unauthorized",
                            "not logged in",
                            "unauthenticated",
                            "bad-credentials",
                        ]
                        if any(k in body_lower for k in auth_error_keywords):
                            is_token_expired = True
                    # --- 识别逻辑结束 ---

                    logger.error(
                        "RateLimitsReverse: Request failed, model={}, status={}, "
                        "is_token_expired={}, is_cloudflare={}, Body: {}",
                        model_name,
                        response.status_code,
                        is_token_expired,
                        is_cloudflare,
                        resp_text[:300],
                        extra={"error_type": "UpstreamException"},
                    )

                    raise UpstreamException(
                        message=(
                            f"RateLimitsReverse: Request failed for {model_name}, "
                            f"{response.status_code}"
                        ),
                        details={
                            "status": response.status_code,
                            "body": resp_text,
                            "is_token_expired": is_token_expired,
                            "is_cloudflare": is_cloudflare,
                        },
                    )

                return response

            async def _on_retry(
                attempt: int, status_code: int, error: Exception, delay: float
            ):
                if active_proxy_key and should_rotate_proxy(status_code):
                    rotate_proxy(active_proxy_key)

            return await retry_on_status(_do_request, on_retry=_on_retry)

        except Exception as e:
            # Handle upstream exception
            if isinstance(e, UpstreamException):
                status = None
                if e.details and isinstance(e.details, dict):
                    status = e.details.get("status")

                if status is None:
                    status = getattr(e, "status_code", None)

                logger.debug(
                    f"RateLimitsReverse: Upstream error caught: {str(e)}, status={status}"
                )
                raise

            # Handle other non-upstream exceptions
            import traceback

            error_details = traceback.format_exc()
            logger.error(
                f"RateLimitsReverse: Unexpected error, {type(e).__name__}: {str(e)}\n{error_details}"
            )
            raise UpstreamException(
                message=f"RateLimitsReverse: Request failed, {str(e)}",
                details={"status": 502, "error": str(e), "traceback": error_details},
            )

    @staticmethod
    async def request_multi(
        session: AsyncSession,
        token: str,
        model_names: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Query rate limits for multiple models in parallel."""
        if model_names is None:
            model_names = ALL_MODELS

        async def _query_one(model_name: str) -> tuple[str, Any, Exception | None]:
            try:
                response = await RateLimitsReverse.request(session, token, model_name)
                return model_name, response.json(), None
            except Exception as e:
                logger.warning(f"RateLimitsReverse: Failed to query {model_name}: {e}")
                return model_name, None, e

        results = await asyncio.gather(*[_query_one(model) for model in model_names])
        raw: Dict[str, Any] = {}
        primary_error: Exception | None = None
        for model_name, data, error in results:
            raw[model_name] = data
            if (
                primary_error is None
                and error is not None
                and model_name in BUCKET_MODELS
            ):
                primary_error = error

        if raw.get("grok-3") is None and primary_error is not None:
            raise primary_error

        return raw


__all__ = [
    "RateLimitsReverse",
    "BUCKET_MODELS",
    "PROBE_MODELS",
    "ALL_MODELS",
]
