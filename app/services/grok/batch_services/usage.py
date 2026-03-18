"""
Batch usage service.
"""

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.core.logger import logger
from app.core.config import get_config
from app.services.reverse.rate_limits import RateLimitsReverse
from app.services.reverse.utils.session import ResettableSession
from app.core.batch import run_batch
from app.services.token.models import BucketQuota

_USAGE_SEMAPHORE = None
_USAGE_SEM_VALUE = None


def _get_usage_semaphore() -> asyncio.Semaphore:
    value = max(1, int(get_config("usage.concurrent")))
    global _USAGE_SEMAPHORE, _USAGE_SEM_VALUE
    if _USAGE_SEMAPHORE is None or value != _USAGE_SEM_VALUE:
        _USAGE_SEM_VALUE = value
        _USAGE_SEMAPHORE = asyncio.Semaphore(value)
    return _USAGE_SEMAPHORE


def _parse_bucket_quota(data: dict | None) -> Optional[BucketQuota]:
    """Parse a rate-limits response into a BucketQuota."""
    if not data or "remainingTokens" not in data:
        return None
    low = data.get("lowEffortRateLimits") or {}
    high = data.get("highEffortRateLimits") or {}
    return BucketQuota(
        remaining_tokens=data.get("remainingTokens", 0),
        total_tokens=data.get("totalTokens", 0),
        low_remaining=low.get("remainingQueries", 0),
        low_total=low.get("totalQueries", 0),
        high_remaining=high.get("remainingQueries", 0),
        high_total=high.get("totalQueries", 0),
    )


def _parse_probe_queries(data: dict | None) -> Optional[int]:
    """Parse a rate-limits response for probe models."""
    if not data:
        return None
    remaining = data.get("remainingQueries")
    if remaining is None:
        remaining = data.get("remainingTokens")
    return remaining


class UsageService:
    """用量查询服务"""

    async def get(self, token: str, model_name: str = "grok-3") -> Dict:
        """
        获取速率限制信息

        Args:
            token: 认证 Token
            model_name: 模型名称

        Returns:
            响应数据

        Raises:
            UpstreamException: 当获取失败且重试耗尽时
        """
        async with _get_usage_semaphore():
            try:
                browser = get_config("proxy.browser")
                if browser:
                    session_ctx = ResettableSession(impersonate=browser)
                else:
                    session_ctx = ResettableSession()
                async with session_ctx as session:
                    response = await RateLimitsReverse.request(
                        session, token, model_name
                    )
                data = response.json()
                remaining = data.get("remainingTokens")
                if remaining is None:
                    remaining = data.get("remainingQueries")
                    if remaining is not None:
                        data["remainingTokens"] = remaining
                logger.debug(
                    "Usage sync success: "
                    f"model={model_name}, remaining={remaining}, token={token[:10]}..."
                )
                return data

            except Exception as e:
                # 最后一次失败已经被记录
                logger.debug(
                    "UsageService.get failed for token "
                    f"{token[:10]}... model={model_name}: {str(e)}"
                )
                raise

    async def get_multi(self, token: str) -> Dict[str, Any]:
        """
        获取所有桶的速率限制信息

        Returns:
            {
                "grok3_quota": BucketQuota or None,
                "grok4_quota": BucketQuota or None,
                "grok41_queries": int or None,
                "grok420_queries": int or None,
                "remainingTokens": int or None,
                "raw": {model_name: data, ...},
            }
        """
        async with _get_usage_semaphore():
            browser = get_config("proxy.browser")
            if browser:
                session_ctx = ResettableSession(impersonate=browser)
            else:
                session_ctx = ResettableSession()
            async with session_ctx as session:
                raw = await RateLimitsReverse.request_multi(session, token)

        grok3_quota = _parse_bucket_quota(raw.get("grok-3"))
        grok4_quota = _parse_bucket_quota(raw.get("grok-4"))
        grok41_queries = _parse_probe_queries(raw.get("grok-4-1-thinking-1129"))
        grok420_queries = _parse_probe_queries(raw.get("grok-420"))
        remaining_tokens = (
            grok3_quota.remaining_tokens if grok3_quota is not None else None
        )

        logger.debug(
            "Usage multi-sync: "
            f"token={token[:10]}..., "
            f"g3={grok3_quota.remaining_tokens if grok3_quota else '?'}/"
            f"{grok3_quota.total_tokens if grok3_quota else '?'}, "
            f"g4={grok4_quota.remaining_tokens if grok4_quota else '?'}/"
            f"{grok4_quota.total_tokens if grok4_quota else '?'}, "
            f"g41={grok41_queries}, g420={grok420_queries}"
        )

        return {
            "grok3_quota": grok3_quota,
            "grok4_quota": grok4_quota,
            "grok41_queries": grok41_queries,
            "grok420_queries": grok420_queries,
            "remainingTokens": remaining_tokens,
            "raw": raw,
        }

    @staticmethod
    async def batch(
        tokens: List[str],
        mgr,
        *,
        on_item: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        batch_size = get_config("usage.batch_size")

        async def _refresh_one(t: str):
            return await mgr.sync_usage(t, consume_on_fail=False, is_usage=False)

        return await run_batch(
            tokens,
            _refresh_one,
            batch_size=batch_size,
            on_item=on_item,
            should_cancel=should_cancel,
        )


__all__ = ["UsageService"]
