"""
Reverse interface: rate limits.
"""

import asyncio
import orjson
from typing import Any, Dict, List, Optional
from curl_cffi.requests import AsyncSession

from app.core.logger import logger
from app.core.config import get_config
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
            # Get proxies
            base_proxy = get_config("proxy.base_proxy_url")
            proxies = {"http": base_proxy, "https": base_proxy} if base_proxy else None

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

            async def _do_request():
                response = await session.post(
                    RATE_LIMITS_API,
                    headers=headers,
                    data=orjson.dumps(payload),
                    timeout=timeout,
                    proxies=proxies,
                    impersonate=browser,
                )

                if response.status_code != 200:
                    logger.error(
                        f"RateLimitsReverse: Request failed for {model_name}, {response.status_code}",
                        extra={"error_type": "UpstreamException"},
                    )
                    raise UpstreamException(
                        message=f"RateLimitsReverse: Request failed for {model_name}, {response.status_code}",
                        details={"status": response.status_code},
                    )

                return response

            return await retry_on_status(_do_request)

        except Exception as e:
            # Handle upstream exception
            if isinstance(e, UpstreamException):
                status = None
                if e.details and "status" in e.details:
                    status = e.details["status"]
                else:
                    status = getattr(e, "status_code", None)
                raise

            # Handle other non-upstream exceptions
            logger.error(
                f"RateLimitsReverse: Request failed for {model_name}, {str(e)}",
                extra={"error_type": type(e).__name__},
            )
            raise UpstreamException(
                message=f"RateLimitsReverse: Request failed for {model_name}, {str(e)}",
                details={"status": 502, "error": str(e)},
            )

    @staticmethod
    async def request_multi(
        session: AsyncSession,
        token: str,
        model_names: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Query rate limits for multiple models in parallel.

        Args:
            session: AsyncSession, the session to use.
            token: str, the SSO token.
            model_names: list of model names to query. Defaults to ALL_MODELS.

        Returns:
            Dict mapping model_name -> parsed JSON response (or None on error).
        """
        if model_names is None:
            model_names = ALL_MODELS

        async def _query_one(model_name: str) -> tuple:
            try:
                response = await RateLimitsReverse.request(session, token, model_name)
                data = response.json()
                return model_name, data
            except Exception as e:
                logger.warning(
                    f"RateLimitsReverse: Failed to query {model_name}: {e}"
                )
                return model_name, None

        results = await asyncio.gather(*[_query_one(m) for m in model_names])
        return dict(results)


__all__ = [
    "RateLimitsReverse",
    "BUCKET_MODELS",
    "PROBE_MODELS",
    "ALL_MODELS",
]
