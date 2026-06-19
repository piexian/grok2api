"""Default quota windows and pool inference logic.

Canonical quota totals per pool type (from upstream rate-limits API):

              auto    fast    expert    heavy
  basic          —      30       —        —        window: 86400 s
  super         50     140      50        —        window: 7200 s
  heavy        150     400     150       20       window: 7200 s

Console.x.ai models use a local-only quota window shared by every pool:
30 calls / 15 minutes. The grok.com rate-limits API does not know this
mode, so callers that sync upstream usage must use ``usage_sync_mode_ids``.

Pool inference uses ``auto.total`` as the primary signal for paid accounts.
Some Super accounts expose a heavy quota window, but that does not mean they
can call heavy models.
"""

from typing import TYPE_CHECKING

from .enums import QuotaSource
from .models import AccountQuotaSet, QuotaWindow

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _w(remaining: int, total: int, window_seconds: int) -> QuotaWindow:
    return QuotaWindow(
        remaining=remaining,
        total=total,
        window_seconds=window_seconds,
        reset_at=None,
        synced_at=None,
        source=QuotaSource.DEFAULT,
    )


# ---------------------------------------------------------------------------
# Per-pool default quota sets
# ---------------------------------------------------------------------------

BASIC_FAST_LIMIT = 30
BASIC_FAST_WINDOW_SECONDS = 86_400
CONSOLE_LIMIT = 30
CONSOLE_WINDOW_SECONDS = 900

BASIC_QUOTA_DEFAULTS = AccountQuotaSet(
    auto=_w(0, 0, 0),  # unsupported on basic accounts
    fast=_w(BASIC_FAST_LIMIT, BASIC_FAST_LIMIT, BASIC_FAST_WINDOW_SECONDS),
    expert=_w(0, 0, 0),  # unsupported on basic accounts
    console=_w(CONSOLE_LIMIT, CONSOLE_LIMIT, CONSOLE_WINDOW_SECONDS),
)

LITE_QUOTA_DEFAULTS = AccountQuotaSet(
    auto=_w(25, 25, 7_200),  # 25  queries / 2 h
    fast=_w(70, 70, 7_200),  # 70  queries / 2 h
    expert=_w(12, 12, 7_200),  # 12  queries / 2 h
    console=_w(CONSOLE_LIMIT, CONSOLE_LIMIT, CONSOLE_WINDOW_SECONDS),
)

SUPER_QUOTA_DEFAULTS = AccountQuotaSet(
    auto=_w(50, 50, 7_200),  # 50  queries / 2 h
    fast=_w(140, 140, 7_200),  # 140 queries / 2 h
    expert=_w(50, 50, 7_200),  # 50  queries / 2 h
    console=_w(CONSOLE_LIMIT, CONSOLE_LIMIT, CONSOLE_WINDOW_SECONDS),
)

HEAVY_QUOTA_DEFAULTS = AccountQuotaSet(
    auto=_w(150, 150, 7_200),  # 150 queries / 2 h
    fast=_w(400, 400, 7_200),  # 400 queries / 2 h
    expert=_w(150, 150, 7_200),  # 150 queries / 2 h
    heavy=_w(20, 20, 7_200),  # 20  queries / 2 h
    console=_w(CONSOLE_LIMIT, CONSOLE_LIMIT, CONSOLE_WINDOW_SECONDS),
)

# Map pool name → defaults object (used by backends on upsert).
_POOL_DEFAULTS: dict[str, AccountQuotaSet] = {
    "basic": BASIC_QUOTA_DEFAULTS,
    "lite": LITE_QUOTA_DEFAULTS,
    "super": SUPER_QUOTA_DEFAULTS,
    "heavy": HEAVY_QUOTA_DEFAULTS,
}

_SUPPORTED_MODE_IDS_BY_POOL: dict[str, frozenset[int]] = {
    "basic": frozenset((1, 5)),
    "lite": frozenset((0, 1, 2, 5)),
    "super": frozenset((0, 1, 2, 5)),
    "heavy": frozenset((0, 1, 2, 3, 5)),
}


def default_quota_set(pool: str) -> AccountQuotaSet:
    """Return a fresh copy of the default quota set for *pool*."""
    src = _POOL_DEFAULTS.get(pool, BASIC_QUOTA_DEFAULTS)
    qs = AccountQuotaSet(
        auto=_w(src.auto.remaining, src.auto.total, src.auto.window_seconds),
        fast=_w(src.fast.remaining, src.fast.total, src.fast.window_seconds),
        expert=_w(src.expert.remaining, src.expert.total, src.expert.window_seconds),
    )
    if src.heavy is not None:
        qs.heavy = _w(src.heavy.remaining, src.heavy.total, src.heavy.window_seconds)
    if src.grok_4_3 is not None:
        qs.grok_4_3 = _w(
            src.grok_4_3.remaining, src.grok_4_3.total, src.grok_4_3.window_seconds
        )
    if src.console is not None:
        qs.console = _w(
            src.console.remaining, src.console.total, src.console.window_seconds
        )
    return qs


def supports_mode(pool: str, mode_id: int) -> bool:
    """Return whether *pool* has a default quota window for *mode_id*."""
    return mode_id in _SUPPORTED_MODE_IDS_BY_POOL.get(
        pool, _SUPPORTED_MODE_IDS_BY_POOL["basic"]
    )


def supported_mode_ids(pool: str) -> tuple[int, ...]:
    """Return the supported mode IDs for *pool* in stable request order."""
    supported = _SUPPORTED_MODE_IDS_BY_POOL.get(
        pool, _SUPPORTED_MODE_IDS_BY_POOL["basic"]
    )
    return tuple(mode_id for mode_id in (0, 1, 2, 3, 4, 5) if mode_id in supported)


def usage_sync_mode_ids(pool: str) -> tuple[int, ...]:
    """Return grok.com rate-limits API mode IDs for *pool*.

    Console mode (5) is local-only and must never be sent to the upstream
    usage endpoint, whose current model names only cover modes 0..3.
    """
    return tuple(mode_id for mode_id in supported_mode_ids(pool) if mode_id != 5)


def default_quota_window(pool: str, mode_id: int) -> QuotaWindow | None:
    """Return the default quota window for *(pool, mode_id)*, if supported."""
    if not supports_mode(pool, mode_id):
        return None
    return default_quota_set(pool).get(mode_id)


def normalize_quota_window(
    pool: str, mode_id: int, window: QuotaWindow | None
) -> QuotaWindow | None:
    """Apply product-level quota policy for one pool/mode window."""
    if window is None or not supports_mode(pool, mode_id):
        return None
    if pool == "basic" and mode_id == 1:
        return QuotaWindow(
            remaining=max(0, min(int(window.remaining), BASIC_FAST_LIMIT)),
            total=BASIC_FAST_LIMIT,
            window_seconds=BASIC_FAST_WINDOW_SECONDS,
            reset_at=window.reset_at,
            synced_at=window.synced_at,
            source=window.source,
        )
    return window


def normalize_quota_set(pool: str, quota_set: AccountQuotaSet) -> AccountQuotaSet:
    """Return a quota set normalized to the supported modes for *pool*."""
    defaults = default_quota_set(pool)

    auto = normalize_quota_window(pool, 0, quota_set.auto) or defaults.auto
    fast = normalize_quota_window(pool, 1, quota_set.fast) or defaults.fast
    expert = normalize_quota_window(pool, 2, quota_set.expert) or defaults.expert

    qs = AccountQuotaSet(auto=auto, fast=fast, expert=expert)
    qs.heavy = normalize_quota_window(pool, 3, quota_set.heavy)
    qs.grok_4_3 = normalize_quota_window(pool, 4, quota_set.grok_4_3)
    qs.console = normalize_quota_window(pool, 5, quota_set.console) or defaults.console
    return qs


def infer_pool(windows: dict[int, QuotaWindow]) -> str:
    """Infer pool type from live quota windows returned by the rate-limits API.

    Uses ``auto.total`` (mode_id=0) as the discriminating signal:
      - 25 -> lite
      - 50 -> super
      - 150+ -> heavy
      - absent/unrecognised -> basic

    Do not infer heavy from the heavy quota window alone: Super accounts may
    expose ``H:20`` while still receiving 403 from the heavy chat model.
    """
    auto_win = windows.get(0)
    if auto_win is None:
        return "basic"
    total = auto_win.total
    if total == 25:
        return "lite"
    if total == 50:
        return "super"
    if total >= 150:
        return "heavy"
    return "basic"


__all__ = [
    "BASIC_QUOTA_DEFAULTS",
    "LITE_QUOTA_DEFAULTS",
    "SUPER_QUOTA_DEFAULTS",
    "HEAVY_QUOTA_DEFAULTS",
    "default_quota_set",
    "default_quota_window",
    "infer_pool",
    "normalize_quota_set",
    "normalize_quota_window",
    "supported_mode_ids",
    "supports_mode",
    "usage_sync_mode_ids",
]
