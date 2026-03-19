"""
Token 数据模型

额度规则:
- Basic 新号默认 80 配额
- Super 新号默认 140 配额
- 重置后恢复默认值
- lowEffort 扣 1，highEffort 扣 4
"""

from enum import Enum
import time
from typing import Dict, List, Optional
from pydantic import BaseModel, Field, field_validator
from datetime import datetime


# 默认配额
BASIC__DEFAULT_QUOTA = 80
SUPER_DEFAULT_QUOTA = 140

# 失败阈值
FAIL_THRESHOLD = 5


class TokenStatus(str, Enum):
    """Token 状态"""

    ACTIVE = "active"
    DISABLED = "disabled"
    EXPIRED = "expired"
    COOLING = "cooling"


class EffortType(str, Enum):
    """请求消耗类型"""

    LOW = "low"  # 扣 1
    HIGH = "high"  # 扣 4


EFFORT_COST = {
    EffortType.LOW: 1,
    EffortType.HIGH: 4,
}


class BucketQuota(BaseModel):
    """单桶额度（grok-3 / grok-4）"""

    remaining_tokens: int = 0
    total_tokens: int = 0
    low_remaining: int = 0
    low_total: int = 0
    high_remaining: int = 0
    high_total: int = 0


class TokenInfo(BaseModel):
    """Token 信息"""

    token: str
    status: TokenStatus = TokenStatus.ACTIVE
    quota: int = BASIC__DEFAULT_QUOTA

    # 消耗记录（本地累加，不依赖 API 返回值）
    # 仅在 consumed_mode_enabled=true 时使用
    consumed: int = 0

    # 分桶额度（来自 /rest/rate-limits 同步）
    grok3_quota: Optional[BucketQuota] = None
    grok4_quota: Optional[BucketQuota] = None
    grok41_queries: Optional[int] = None  # 探针桶，仅参考
    grok420_queries: Optional[int] = None  # 探针桶，仅参考

    # 模型级冷却
    model_cooldowns: Dict[str, int] = Field(default_factory=dict)

    # 统计
    created_at: int = Field(
        default_factory=lambda: int(datetime.now().timestamp() * 1000)
    )
    last_used_at: Optional[int] = None
    use_count: int = 0

    # 失败追踪
    fail_count: int = 0
    last_fail_at: Optional[int] = None
    last_fail_reason: Optional[str] = None

    # 冷却管理
    last_sync_at: Optional[int] = None  # 上次同步时间

    # 扩展
    tags: List[str] = Field(default_factory=list)
    note: str = ""
    last_asset_clear_at: Optional[int] = None

    @field_validator("token", mode="before")
    @classmethod
    def _normalize_token(cls, value):
        """Normalize copied tokens to avoid unicode punctuation issues."""
        if value is None:
            raise ValueError("token cannot be empty")
        token = str(value)
        token = token.translate(
            str.maketrans(
                {
                    "\u2010": "-",
                    "\u2011": "-",
                    "\u2012": "-",
                    "\u2013": "-",
                    "\u2014": "-",
                    "\u2212": "-",
                    "\u00a0": " ",
                    "\u2007": " ",
                    "\u202f": " ",
                    "\u200b": "",
                    "\u200c": "",
                    "\u200d": "",
                    "\ufeff": "",
                }
            )
        )
        token = "".join(token.split())
        if token.startswith("sso="):
            token = token[4:]
        token = token.encode("ascii", errors="ignore").decode("ascii")
        if not token:
            raise ValueError("token cannot be empty")
        return token

    def clear_expired_model_cooldowns(self, now_ms: Optional[int] = None) -> bool:
        """Remove expired model cooldown markers."""
        if not self.model_cooldowns:
            return False
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        expired = [
            model_id
            for model_id, until_ms in self.model_cooldowns.items()
            if not until_ms or until_ms <= now_ms
        ]
        if not expired:
            return False
        for model_id in expired:
            self.model_cooldowns.pop(model_id, None)
        return True

    def get_model_remaining(self, model_id: Optional[str]) -> Optional[int]:
        """Return model-specific remaining quota when available."""
        if not model_id:
            return None

        if model_id == "grok-3":
            return (
                int(self.grok3_quota.remaining_tokens)
                if self.grok3_quota is not None
                else None
            )
        if model_id == "grok-4":
            return (
                int(self.grok4_quota.remaining_tokens)
                if self.grok4_quota is not None
                else None
            )
        if model_id == "grok-4-1-thinking-1129":
            return (
                int(self.grok4_quota.remaining_tokens)
                if self.grok4_quota is not None
                else None
            )
        if model_id == "grok-420":
            return (
                int(self.grok4_quota.remaining_tokens)
                if self.grok4_quota is not None
                else None
            )
        if model_id in {
            "grok-imagine-1.0",
            "grok-imagine-1.0-fast",
            "grok-imagine-1.0-video",
        }:
            return (
                int(self.grok3_quota.remaining_tokens)
                if self.grok3_quota is not None
                else None
            )
        return None

    def is_model_cooling(
        self,
        model_id: Optional[str],
        now_ms: Optional[int] = None,
    ) -> bool:
        """Check whether the given model is still cooling down."""
        if not model_id:
            return False
        self.clear_expired_model_cooldowns(now_ms=now_ms)
        until_ms = self.model_cooldowns.get(model_id)
        if not until_ms:
            return False
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        return until_ms > now_ms

    def is_available(
        self,
        consumed_mode: bool = False,
        model_id: Optional[str] = None,
    ) -> bool:
        """检查当前模式下 token 是否可用。"""
        if self.status != TokenStatus.ACTIVE:
            return False
        if self.is_model_cooling(model_id):
            return False

        model_remaining = self.get_model_remaining(model_id)
        if model_remaining is not None:
            return model_remaining > 0

        if consumed_mode:
            return True
        return self.quota > 0

    def enter_cooling(self, reset_consumed: bool = True):
        """进入冷却状态，并在新窗口开始时清空 consumed。"""
        self.status = TokenStatus.COOLING
        if reset_consumed:
            self.consumed = 0

    def recover_active(self, allow_from_expired: bool = False):
        """仅在允许的前提下恢复为 active。"""
        if self.status == TokenStatus.COOLING:
            self.status = TokenStatus.ACTIVE
        elif allow_from_expired and self.status == TokenStatus.EXPIRED:
            self.status = TokenStatus.ACTIVE

    def consume(self, effort: EffortType = EffortType.LOW) -> int:
        """
        消耗配额（默认：扣减 quota）

        Args:
            effort: LOW 计 1 次，HIGH 计 4 次

        Returns:
            实际扣除的配额
        """
        cost = EFFORT_COST[effort]

        # 默认行为：扣减 quota
        actual_cost = min(cost, self.quota)

        self.last_used_at = int(datetime.now().timestamp() * 1000)
        self.consumed += cost  # 无论是否开启消耗模式，都记录消耗
        self.use_count += actual_cost
        self.quota = max(0, self.quota - actual_cost)

        # 默认行为：quota 耗尽时标记冷却，并重置消耗记录
        if self.quota == 0:
            self.enter_cooling()
        else:
            self.recover_active()

        return actual_cost

    def consume_with_consumed(self, effort: EffortType = EffortType.LOW) -> int:
        """
        消耗配额（consumed 模式：累加 consumed 而非扣减 quota）

        仅在 consumed_mode_enabled=true 时使用

        Args:
            effort: LOW 计 1 次，HIGH 计 4 次

        Returns:
            实际计入的消耗次数
        """
        cost = EFFORT_COST[effort]

        self.consumed += cost  # 累加消耗记录
        self.last_used_at = int(datetime.now().timestamp() * 1000)
        self.use_count += 1

        # consumed 模式下不自动判断冷却，由 Rate Limits 检查或 429 触发
        self.recover_active()

        return cost

    def update_quota(self, new_quota: int):
        """
        更新配额（用于 API 同步 - 默认模式）

        Args:
            new_quota: 新的配额值
        """
        self.quota = max(0, new_quota)

        if self.quota == 0:
            self.enter_cooling()
        else:
            self.recover_active(allow_from_expired=True)

    def update_quota_with_consumed(self, new_quota: int):
        """
        更新配额（consumed 模式）

        仅在 consumed_mode_enabled=true 时使用

        Args:
            new_quota: 新的配额值
        """
        self.quota = max(0, new_quota)

        if self.quota == 0:
            self.enter_cooling()
        else:
            self.recover_active()

    def reset(self, default_quota: Optional[int] = None):
        """重置配额到默认值"""
        quota = BASIC__DEFAULT_QUOTA if default_quota is None else default_quota
        self.quota = max(0, int(quota))
        self.status = TokenStatus.ACTIVE
        self.fail_count = 0
        self.last_fail_reason = None
        # 重置消耗记录
        self.consumed = 0

    def record_fail(
        self,
        status_code: int = 401,
        reason: str = "",
        threshold: Optional[int] = None,
    ):
        """记录失败，达到阈值后自动标记为 expired"""
        # 仅 401 计入失败
        if status_code != 401:
            return

        self.fail_count += 1
        self.last_fail_at = int(datetime.now().timestamp() * 1000)
        self.last_fail_reason = reason

        limit = FAIL_THRESHOLD if threshold is None else threshold
        if self.fail_count >= limit:
            self.status = TokenStatus.EXPIRED

    def record_success(self, is_usage: bool = True):
        """记录成功，清空失败计数"""
        self.fail_count = 0
        self.last_fail_at = None
        self.last_fail_reason = None

        if is_usage:
            self.use_count += 1
            self.last_used_at = int(datetime.now().timestamp() * 1000)

    def need_refresh(self, interval_hours: int = 8) -> bool:
        """检查是否需要刷新配额"""
        if self.status != TokenStatus.COOLING:
            return False

        if self.last_sync_at is None:
            return True

        now = int(datetime.now().timestamp() * 1000)
        interval_ms = interval_hours * 3600 * 1000
        return (now - self.last_sync_at) >= interval_ms

    def mark_synced(self):
        """标记已同步"""
        self.last_sync_at = int(datetime.now().timestamp() * 1000)

    def should_cool_down(self, remaining_tokens: int, threshold: int = 10) -> bool:
        """
        根据 Rate Limits 返回值判断是否应该冷却

        Args:
            remaining_tokens: API 返回的剩余配额
            threshold: 冷却阈值，默认 10

        Returns:
            是否应该进入冷却状态
        """
        if remaining_tokens <= threshold:
            self.status = TokenStatus.COOLING
            return True
        return False


class TokenPoolStats(BaseModel):
    """Token 池统计"""

    total: int = 0
    active: int = 0
    disabled: int = 0
    expired: int = 0
    cooling: int = 0
    total_quota: int = 0
    avg_quota: float = 0.0
    total_consumed: int = 0
    avg_consumed: float = 0.0


__all__ = [
    "TokenStatus",
    "TokenInfo",
    "TokenPoolStats",
    "BucketQuota",
    "EffortType",
    "EFFORT_COST",
    "BASIC__DEFAULT_QUOTA",
    "SUPER_DEFAULT_QUOTA",
    "FAIL_THRESHOLD",
]
