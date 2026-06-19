"""Model registry — all supported model variants defined in one place."""

from .enums import Capability, ModeId, Tier
from .spec import ModelSpec

# ---------------------------------------------------------------------------
# Master model list.
# Add new models here; no other files need to change.
# ---------------------------------------------------------------------------

# fmt: off
MODELS: tuple[ModelSpec, ...] = (
    # === Chat ==============================================================

    # grok.com web-chat modes. As of 2026-06-19 these are backed by Grok 4.3.
    ModelSpec("grok-4.3-fast",                          ModeId.FAST,     Tier.BASIC, Capability.CHAT,       True, "Grok 4.3 Fast"),
    ModelSpec("grok-4.3-auto",                          ModeId.AUTO,     Tier.SUPER, Capability.CHAT,       True, "Grok 4.3 Auto",          prefer_best=True),
    ModelSpec("grok-4.3-expert",                        ModeId.EXPERT,   Tier.SUPER, Capability.CHAT,       True, "Grok 4.3 Expert",        prefer_best=True),
    ModelSpec("grok-4.3-heavy",                         ModeId.HEAVY,    Tier.HEAVY, Capability.CHAT,       True, "Grok 4.3 Heavy",         prefer_best=True),

    # === Console API (console.x.ai/v1/responses) ============================
    # 通过 SSO cookie 直接调用 console.x.ai，basic 账号即可使用所有模型
    # 速率限制由 console.x.ai 控制（免费 tier: 1 rps / 60 RPM）
    # Console effort support is model-specific. grok-4.3 accepts
    # reasoning.effort; build and grok-4.20 variants reject it with HTTP 400.
    ModelSpec("grok-4.3",                               ModeId.CONSOLE, Tier.BASIC, Capability.CHAT,        True, "Grok 4.3 (Console)",                    console_model="grok-4.3",                       default_reasoning_effort="high"),
    ModelSpec("grok-build-0.1",                         ModeId.CONSOLE, Tier.BASIC, Capability.CHAT,        True, "Grok Build 0.1 (Console)",              console_model="grok-build-0.1"),
    # Fixed-intensity reasoning model — upstream rejects reasoning.effort.
    ModelSpec("grok-4.20-0309-reasoning",               ModeId.CONSOLE, Tier.BASIC, Capability.CHAT,        True, "Grok 4.20 0309 Reasoning (Console)",    console_model="grok-4.20-0309-reasoning"),
    # Non-reasoning model — effort is not applicable.
    ModelSpec("grok-4.20-0309-non-reasoning",           ModeId.CONSOLE, Tier.BASIC, Capability.CHAT,        True, "Grok 4.20 0309 Non-Reasoning (Console)", console_model="grok-4.20-0309-non-reasoning"),
    # Multi-agent — left default; effort behaviour with this variant has not
    # been verified, so we don't auto-inject "high" to avoid surprising 400s.
    ModelSpec("grok-4.20-multi-agent-0309",             ModeId.CONSOLE, Tier.BASIC, Capability.CHAT,        True, "Grok 4.20 Multi-Agent 0309 (Console)",  console_model="grok-4.20-multi-agent-0309"),

    # === Image ==============================================================

    # Basic fast
    ModelSpec("grok-imagine-image-lite",                ModeId.FAST,     Tier.BASIC, Capability.IMAGE,      True, "Grok Imagine Image Lite"),
    # Super+
    ModelSpec("grok-imagine-image",                     ModeId.AUTO,     Tier.SUPER, Capability.IMAGE,      True, "Grok Imagine Image"),
    ModelSpec("grok-imagine-image-pro",                 ModeId.AUTO,     Tier.SUPER, Capability.IMAGE,      True, "Grok Imagine Image Pro"),

    # === Image Edit =========================================================

    # Super+
    ModelSpec("grok-imagine-image-edit",                ModeId.AUTO,     Tier.SUPER, Capability.IMAGE_EDIT, True, "Grok Imagine Image Edit"),

    # === Video ==============================================================

    # Super+
    ModelSpec("grok-imagine-video",                     ModeId.AUTO,     Tier.SUPER, Capability.VIDEO,      True, "Grok Imagine Video"),
)
# fmt: on

# ---------------------------------------------------------------------------
# Internal lookup structures — built once at import time.
# ---------------------------------------------------------------------------

_BY_NAME: dict[str, ModelSpec] = {m.model_name: m for m in MODELS}

_BY_CAP: dict[int, list[ModelSpec]] = {}
for _m in MODELS:
    _BY_CAP.setdefault(int(_m.capability), []).append(_m)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get(model_name: str) -> ModelSpec | None:
    """Return the spec for *model_name*, or ``None`` if not registered."""
    return _BY_NAME.get(model_name)


def resolve(model_name: str) -> ModelSpec:
    """Return the spec for *model_name*; raise ``ValueError`` if unknown."""
    spec = _BY_NAME.get(model_name)
    if spec is None:
        raise ValueError(f"Unknown model: {model_name!r}")
    return spec


def list_enabled() -> list[ModelSpec]:
    """Return all enabled models in registration order."""
    return [m for m in MODELS if m.enabled]


def list_by_capability(cap: Capability) -> list[ModelSpec]:
    """Return enabled models that include *cap* in their capability mask."""
    return [m for m in MODELS if m.enabled and bool(m.capability & cap)]


__all__ = ["MODELS", "get", "resolve", "list_enabled", "list_by_capability"]
