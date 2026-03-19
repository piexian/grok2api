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


def _get_limit_value(data: dict | None, *keys: str) -> Optional[int]:
    """Read a limit value from top-level or nested limit objects."""
    if not data:
        return None
    containers = [
        data,
        data.get("limits") if isinstance(data.get("limits"), dict) else None,
        data.get("rateLimits") if isinstance(data.get("rateLimits"), dict) else None,
    ]
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in keys:
            value = container.get(key)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def _parse_bucket_quota(data: dict | None) -> Optional[BucketQuota]:
    """Parse a rate-limits response into a BucketQuota."""
    if not data:
        return None
    remaining = _get_limit_value(data, "remainingTokens", "remainingQueries")
    total = _get_limit_value(data, "totalTokens", "totalQueries")
    if remaining is None:
        return None
    low = data.get("lowEffortRateLimits") or {}
    high = data.get("highEffortRateLimits") or {}
    return BucketQuota(
        remaining_tokens=remaining,
        total_tokens=total or remaining,
        low_remaining=_get_limit_value(low, "remainingQueries", "remainingTokens") or 0,
        low_total=_get_limit_value(low, "totalQueries", "totalTokens") or 0,
        high_remaining=_get_limit_value(high, "remainingQueries", "remainingTokens")
        or 0,
        high_total=_get_limit_value(high, "totalQueries", "totalTokens") or 0,
    )


def _parse_probe_queries(data: dict | None) -> Optional[int]:
    """Parse a rate-limits response for probe models."""
    return _get_limit_value(data, "remainingQueries", "remainingTokens")


def _pick_model_payload(raw: dict[str, Any], *keys: str) -> dict | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, dict):
            return value
    return None


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

        grok3_quota = _parse_bucket_quota(_pick_model_payload(raw, "grok-3"))
        grok4_quota = _parse_bucket_quota(_pick_model_payload(raw, "grok-4"))
        grok41_queries = _parse_probe_queries(
            _pick_model_payload(raw, "grok-4-1-thinking-1129")
        )
        grok420_queries = _parse_probe_queries(
            _pick_model_payload(raw, "grok420", "grok-420")
        )
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
