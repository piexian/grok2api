"""Background scheduler for periodic account quota refresh.

Runs one priority loop for pool types (heavy / super / lite / basic), each with
its own configurable interval read from:

    account.refresh.basic_interval_sec  (default 86400 — 24 h)
    account.refresh.super_interval_sec  (default  7200 —  2 h)
    account.refresh.heavy_interval_sec  (default  7200 —  2 h)
"""

import asyncio
import time

from app.platform.config.snapshot import get_config
from app.platform.logging.logger import logger
from .refresh import AccountRefreshService

# Pool → (config key, built-in default seconds)
_POOL_CONFIG: dict[str, tuple[str, int]] = {
    "heavy": ("account.refresh.heavy_interval_sec", 7_200),
    "super": ("account.refresh.super_interval_sec", 7_200),
    "lite": ("account.refresh.super_interval_sec", 7_200),
    "basic": ("account.refresh.basic_interval_sec", 86_400),
}
_DEFAULT_BATCH_SIZE = 500


def _interval(pool: str) -> int:
    key, default = _POOL_CONFIG[pool]
    v = get_config(key, None)
    return int(v) if v is not None else default


def _batch_size() -> int:
    v = get_config("account.refresh.scheduler_batch_size", _DEFAULT_BATCH_SIZE)
    return max(1, int(v))


class AccountRefreshScheduler:
    """Runs a priority refresh loop with pool-specific intervals.

    Lifecycle:  ``start()`` → loops run in background → ``stop()`` to cancel.
    """

    def __init__(self, refresh_service: AccountRefreshService) -> None:
        self._service = refresh_service
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._cursors: dict[str, str | None] = {pool: None for pool in _POOL_CONFIG}
        self._next_due: dict[str, float] = {}

    def bind_service(self, refresh_service: AccountRefreshService) -> None:
        """Update the refresh service used by the singleton scheduler."""
        self._service = refresh_service

    def is_running(self) -> bool:
        """Return True while any pool refresh loop is still active."""
        return any(not task.done() for task in self._tasks)

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        now = time.monotonic()
        self._cursors = {pool: None for pool in _POOL_CONFIG}
        self._next_due = {
            pool: now + float(_interval(pool))
            for pool in _POOL_CONFIG
        }
        self._tasks = [asyncio.create_task(self._loop(), name="account-refresh")]
        intervals = {p: _interval(p) for p in _POOL_CONFIG}
        logger.info(
            "account refresh scheduler started: basic_interval_s={} super_interval_s={} "
            "heavy_interval_s={} batch_size={} priority={}",
            intervals["basic"],
            intervals["super"],
            intervals["heavy"],
            _batch_size(),
            list(_POOL_CONFIG),
        )

    def stop(self) -> None:
        was_running = self.is_running()
        self._stop.set()
        for t in self._tasks:
            if not t.done():
                t.cancel()
        self._tasks = []
        if was_running:
            logger.info("account refresh scheduler stopped")

    async def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            due = [
                pool
                for pool in _POOL_CONFIG
                if now >= self._next_due.get(pool, now + float(_interval(pool)))
            ]

            if not due:
                timeout = max(0.1, min(self._next_due.values()) - now)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=timeout)
                    break
                except asyncio.TimeoutError:
                    continue

            for pool in due:
                if self._stop.is_set():
                    break
                await self._refresh_pool_batch(pool)

    async def _refresh_pool_batch(self, pool: str) -> None:
        batch_size = _batch_size()
        after_token = self._cursors.get(pool)
        try:
            result = await self._service.refresh_scheduled(
                pool=pool,
                limit=batch_size,
                after_token=after_token,
            )
            has_more = result.checked >= batch_size and result.cursor is not None
            if has_more:
                self._cursors[pool] = result.cursor
                self._next_due[pool] = 0.0
            else:
                self._cursors[pool] = None
                self._next_due[pool] = time.monotonic() + float(_interval(pool))
            logger.info(
                "account refresh cycle completed: pool={} after_token={} limit={} checked={} refreshed={} recovered={} failed={} has_more={}",
                pool,
                after_token,
                batch_size,
                result.checked,
                result.refreshed,
                result.recovered,
                result.failed,
                has_more,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._next_due[pool] = time.monotonic() + float(_interval(pool))
            try:
                self._cursors[pool] = None
            finally:
                logger.error(
                    "account refresh cycle failed: pool={} error_type={} error={}",
                    pool,
                    type(exc).__name__,
                    exc,
                )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_scheduler: AccountRefreshScheduler | None = None


def get_account_refresh_scheduler(
    refresh_service: AccountRefreshService,
) -> AccountRefreshScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AccountRefreshScheduler(refresh_service)
    else:
        _scheduler.bind_service(refresh_service)
    return _scheduler


__all__ = ["AccountRefreshScheduler", "get_account_refresh_scheduler"]
