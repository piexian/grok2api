"""Redis account repository.

Layout:
  accounts:rev                 — STRING  global revision counter
  accounts:record:<token>      — HASH    flattened AccountRecord fields
  accounts:pool:<pool>         — SET     token members per pool (live)
  accounts:revision_log        — ZSET    token → revision (for scan_changes)
"""

import json

from app.platform.runtime.clock import now_ms
from ..commands import (
    AccountPatch,
    AccountUpsert,
    BulkReplacePoolCommand,
    ListAccountsQuery,
)
from ..enums import AccountStatus
from ..state_machine import derive_status
from ..models import (
    AccountChangeSet,
    AccountMutationResult,
    AccountPage,
    AccountRecord,
    RuntimeSnapshot,
)
from redis.asyncio import Redis

from ..quota_defaults import default_quota_set

_KEY_REV = "accounts:rev"
_KEY_GLOBAL_SUCCESS_COUNT = "accounts:global_success_count"
_KEY_RECORD = "accounts:record:{token}"
_KEY_POOL = "accounts:pool:{pool}"
_KEY_REV_LOG = "accounts:revision_log"


def _success_rate(record: AccountRecord) -> float:
    total = record.usage_use_count + record.usage_fail_count
    return record.usage_use_count / total if total else 0


def _sort_value(record: AccountRecord, sort_key: str):
    if sort_key == "success_rate":
        return _success_rate(record)
    return getattr(record, sort_key, 0) or 0


def _record_key(token: str) -> str:
    return f"accounts:record:{token}"


def _pool_key(pool: str) -> str:
    return f"accounts:pool:{pool}"


class RedisAccountRepository:
    """Redis-backed account repository.

    Requires redis-py >= 5 with async support.
    """

    def __init__(self, redis: "Redis") -> None:
        self._r = redis

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_hash(record: AccountRecord, revision: int) -> dict[str, str]:
        qs = record.quota_set()
        return {
            "account_id": record.account_id or "",
            "pool": record.pool,
            "status": record.status.value,
            "created_at": str(record.created_at),
            "updated_at": str(record.updated_at),
            "tags": json.dumps(record.tags),
            "quota_auto": json.dumps(qs.auto.to_dict()),
            "quota_fast": json.dumps(qs.fast.to_dict()),
            "quota_expert": json.dumps(qs.expert.to_dict()),
            "quota_heavy": json.dumps(qs.heavy.to_dict()) if qs.heavy else "{}",
            "quota_grok_4_3": json.dumps(qs.grok_4_3.to_dict())
            if qs.grok_4_3
            else "{}",
            "quota_console": json.dumps(qs.console.to_dict()) if qs.console else "{}",
            "usage_use_count": str(record.usage_use_count),
            "usage_fail_count": str(record.usage_fail_count),
            "usage_sync_count": str(record.usage_sync_count),
            "last_use_at": str(record.last_use_at or ""),
            "last_fail_at": str(record.last_fail_at or ""),
            "last_fail_reason": record.last_fail_reason or "",
            "last_sync_at": str(record.last_sync_at or ""),
            "last_clear_at": str(record.last_clear_at or ""),
            "state_reason": record.state_reason or "",
            "deleted_at": str(record.deleted_at or ""),
            "ext": json.dumps(record.ext),
            "revision": str(revision),
        }

    @staticmethod
    def _from_hash(token: str, h: dict[bytes | str, bytes | str]) -> AccountRecord:
        def _s(k: str) -> str:
            v = h.get(k) or h.get(k.encode())
            return v.decode() if isinstance(v, bytes) else (v or "")

        def _i(k: str) -> int | None:
            v = _s(k)
            return int(v) if v else None

        aid = _s("account_id")
        return AccountRecord.model_validate(
            {
                "token": token,
                "account_id": aid if aid else None,
                "pool": _s("pool") or "basic",
                "status": _s("status") or "active",
                "created_at": _i("created_at") or now_ms(),
                "updated_at": _i("updated_at") or now_ms(),
                "tags": json.loads(_s("tags") or "[]"),
                "quota": {
                    "auto": json.loads(_s("quota_auto") or "{}"),
                    "fast": json.loads(_s("quota_fast") or "{}"),
                    "expert": json.loads(_s("quota_expert") or "{}"),
                    **(
                        {"heavy": json.loads(_s("quota_heavy"))}
                        if _s("quota_heavy") and _s("quota_heavy") != "{}"
                        else {}
                    ),
                    **(
                        {"grok_4_3": json.loads(_s("quota_grok_4_3"))}
                        if _s("quota_grok_4_3") and _s("quota_grok_4_3") != "{}"
                        else {}
                    ),
                    **(
                        {"console": json.loads(_s("quota_console"))}
                        if _s("quota_console") and _s("quota_console") != "{}"
                        else {}
                    ),
                },
                "usage_use_count": int(_s("usage_use_count") or 0),
                "usage_fail_count": int(_s("usage_fail_count") or 0),
                "usage_sync_count": int(_s("usage_sync_count") or 0),
                "last_use_at": _i("last_use_at"),
                "last_fail_at": _i("last_fail_at"),
                "last_fail_reason": _s("last_fail_reason") or None,
                "last_sync_at": _i("last_sync_at"),
                "last_clear_at": _i("last_clear_at"),
                "state_reason": _s("state_reason") or None,
                "deleted_at": _i("deleted_at"),
                "ext": json.loads(_s("ext") or "{}"),
                "revision": int(_s("revision") or 0),
            }
        )

    # ------------------------------------------------------------------
    # Revision management
    # ------------------------------------------------------------------

    async def _bump_revision(self) -> int:
        return int(await self._r.incr(_KEY_REV))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        await self._r.setnx(_KEY_REV, "0")
        if await self._r.setnx(_KEY_GLOBAL_SUCCESS_COUNT, "0"):
            total = 0
            async for key in self._r.scan_iter("accounts:record:*"):
                value = await self._r.hget(key, "usage_use_count")
                if value:
                    total += int(value)
            if total:
                await self._r.set(_KEY_GLOBAL_SUCCESS_COUNT, str(total))

    async def get_revision(self) -> int:
        v = await self._r.get(_KEY_REV)
        return int(v) if v else 0

    async def get_global_success_count(self) -> int:
        v = await self._r.get(_KEY_GLOBAL_SUCCESS_COUNT)
        return int(v) if v else 0

    async def increment_global_success_count(self, delta: int = 1) -> int:
        delta = max(0, int(delta))
        if delta == 0:
            return await self.get_global_success_count()
        return int(await self._r.incrby(_KEY_GLOBAL_SUCCESS_COUNT, delta))

    async def aggregate_usage(self) -> dict[str, int]:
        use_total = 0
        fail_total = 0
        async for key in self._r.scan_iter("accounts:record:*"):
            h = await self._r.hmget(
                key, "usage_use_count", "usage_fail_count", "deleted_at"
            )
            if h[2]:  # deleted_at set → skip soft-deleted
                continue
            use_total += int(h[0] or 0)
            fail_total += int(h[1] or 0)
        return {"use_count": use_total, "fail_count": fail_total}

    async def runtime_snapshot(self) -> RuntimeSnapshot:
        rev = await self.get_revision()
        # Scan all record keys.
        keys: list[str] = []
        async for k in self._r.scan_iter("accounts:record:*"):
            keys.append(k.decode() if isinstance(k, bytes) else k)

        items: list[AccountRecord] = []
        for key in keys:
            token = key.split(":", 2)[-1]
            h = await self._r.hgetall(key)
            if not h:
                continue
            record = self._from_hash(token, h)
            if not record.is_deleted():
                items.append(record)
        return RuntimeSnapshot(revision=rev, items=items)

    async def scan_changes(
        self,
        since_revision: int,
        *,
        limit: int = 5000,
    ) -> AccountChangeSet:
        rev = await self.get_revision()
        # Tokens whose revision > since_revision.
        entries = await self._r.zrangebyscore(
            _KEY_REV_LOG,
            since_revision + 1,
            "+inf",
            withscores=False,
            start=0,
            num=limit,
        )
        tokens = [(e.decode() if isinstance(e, bytes) else e) for e in entries]
        items: list[AccountRecord] = []
        deleted: list[str] = []
        for token in tokens:
            h = await self._r.hgetall(_record_key(token))
            if not h:
                deleted.append(token)
                continue
            record = self._from_hash(token, h)
            if record.is_deleted():
                deleted.append(token)
            else:
                items.append(record)
        return AccountChangeSet(
            revision=rev,
            items=items,
            deleted_tokens=deleted,
            has_more=len(entries) == limit,
        )

    async def upsert_accounts(
        self,
        items: list[AccountUpsert],
    ) -> AccountMutationResult:
        if not items:
            return AccountMutationResult()
        rev = await self._bump_revision()
        count = 0
        for item in items:
            try:
                token = AccountRecord.model_validate(
                    {"token": item.token, "pool": item.pool}
                ).token
            except ValueError:
                continue
            pool = (
                item.pool if item.pool in ("basic", "lite", "super", "heavy") else "basic"
            )
            qs = default_quota_set(pool)
            ts = now_ms()
            account_id = item.account_id or None

            key = _record_key(token)
            # Preserve an account_id discovered earlier (via subscription patch)
            # when this upsert omits it (e.g. token edit/replace), so dedup keeps
            # working instead of being reset to empty.
            if account_id is None:
                existing_aid = await self._r.hget(key, "account_id")
                if existing_aid:
                    if isinstance(existing_aid, bytes):
                        existing_aid = existing_aid.decode()
                    account_id = existing_aid or None
            if account_id:
                async for key in self._r.scan_iter("accounts:record:*"):
                    h = await self._r.hgetall(key)
                    if not h:
                        continue
                    existing_aid = h.get(b"account_id") or h.get("account_id") or b""
                    if isinstance(existing_aid, bytes):
                        existing_aid = existing_aid.decode()
                    if existing_aid == account_id:
                        dup_token = (
                            key.decode() if isinstance(key, bytes) else key
                        ).split(":", 2)[-1]
                        if dup_token != token:
                            await self._r.hset(
                                key,
                                mapping={
                                    "deleted_at": str(ts),
                                    "updated_at": str(ts),
                                    "revision": str(rev),
                                },
                            )
                            # Remove from pool set and record the soft-delete in
                            # the revision log so scan_changes() propagates it to
                            # the runtime table (otherwise routing keeps the dup
                            # until a full bootstrap).
                            dup_pool = h.get(b"pool") or h.get("pool") or b"basic"
                            if isinstance(dup_pool, bytes):
                                dup_pool = dup_pool.decode()
                            await self._r.srem(_pool_key(dup_pool), dup_token)
                            await self._r.zadd(_KEY_REV_LOG, {dup_token: rev})

            record = AccountRecord(
                token=token,
                account_id=account_id,
                pool=pool,
                tags=item.tags,
                ext=item.ext,
                quota=qs.to_dict(),
                created_at=ts,
                updated_at=ts,
            )
            key = _record_key(token)
            await self._r.hset(key, mapping=self._to_hash(record, rev))
            await self._r.sadd(_pool_key(pool), token)
            await self._r.zadd(_KEY_REV_LOG, {token: rev})
            count += 1
        return AccountMutationResult(upserted=count, revision=rev)

    async def patch_accounts(
        self,
        patches: list[AccountPatch],
    ) -> AccountMutationResult:
        if not patches:
            return AccountMutationResult()
        rev = await self._bump_revision()
        count = 0
        ts = now_ms()
        for patch in patches:
            key = _record_key(patch.token)
            h = await self._r.hgetall(key)
            if not h:
                continue
            record = self._from_hash(patch.token, h)
            updates: dict[str, str] = {
                "updated_at": str(ts),
                "revision": str(rev),
            }
            if patch.status is not None:
                updates["status"] = patch.status.value
            if patch.state_reason is not None:
                updates["state_reason"] = patch.state_reason
            if patch.last_use_at is not None:
                updates["last_use_at"] = str(patch.last_use_at)
            if patch.last_fail_at is not None:
                updates["last_fail_at"] = str(patch.last_fail_at)
            if patch.last_fail_reason is not None:
                updates["last_fail_reason"] = patch.last_fail_reason
            if patch.last_sync_at is not None:
                updates["last_sync_at"] = str(patch.last_sync_at)
            if patch.last_clear_at is not None:
                updates["last_clear_at"] = str(patch.last_clear_at)
            if patch.account_id is not None:
                updates["account_id"] = patch.account_id
            if patch.pool is not None:
                updates["pool"] = patch.pool
            if patch.quota_auto is not None:
                updates["quota_auto"] = json.dumps(patch.quota_auto)
            if patch.quota_fast is not None:
                updates["quota_fast"] = json.dumps(patch.quota_fast)
            if patch.quota_expert is not None:
                updates["quota_expert"] = json.dumps(patch.quota_expert)
            if patch.quota_heavy is not None:
                updates["quota_heavy"] = json.dumps(patch.quota_heavy)
            if patch.quota_grok_4_3 is not None:
                updates["quota_grok_4_3"] = json.dumps(patch.quota_grok_4_3)
            if patch.quota_console is not None:
                updates["quota_console"] = json.dumps(patch.quota_console)

            # Usage counters.
            if patch.usage_use_delta is not None:
                updates["usage_use_count"] = str(
                    max(0, record.usage_use_count + patch.usage_use_delta)
                )
            if patch.usage_fail_delta is not None:
                updates["usage_fail_count"] = str(
                    max(0, record.usage_fail_count + patch.usage_fail_delta)
                )
            if patch.usage_sync_delta is not None:
                updates["usage_sync_count"] = str(
                    max(0, record.usage_sync_count + patch.usage_sync_delta)
                )

            # Tags.
            tags = list(record.tags)
            if patch.tags is not None:
                tags = patch.tags
            if patch.add_tags:
                for t in patch.add_tags:
                    if t not in tags:
                        tags.append(t)
            if patch.remove_tags:
                tags = [t for t in tags if t not in patch.remove_tags]
            updates["tags"] = json.dumps(tags)

            # ext.
            ext = dict(record.ext)
            if patch.ext_merge:
                ext.update(patch.ext_merge)
            if patch.clear_failures:
                for k in (
                    "cooldown_until",
                    "cooldown_reason",
                    "disabled_at",
                    "disabled_reason",
                    "expired_at",
                    "expired_reason",
                    "forbidden_strikes",
                ):
                    ext.pop(k, None)
                updates["status"] = AccountStatus.ACTIVE.value
                updates["usage_fail_count"] = "0"
                updates["last_fail_at"] = ""
                updates["last_fail_reason"] = ""
                updates["state_reason"] = ""
            updates["ext"] = json.dumps(ext)

            await self._r.hset(key, mapping=updates)
            await self._r.zadd(_KEY_REV_LOG, {patch.token: rev})
            # Pool change must move the token between pool sets, else the stale
            # membership lets a later pool replace soft-delete an upgraded token.
            if patch.pool is not None and patch.pool != record.pool:
                await self._r.srem(_pool_key(record.pool), patch.token)
                await self._r.sadd(_pool_key(patch.pool), patch.token)
            # Dedup: a patch assigning an account_id (subscription API on the
            # normal import path) soft-deletes any other live token sharing that
            # xaiUserId, keeping one canonical token per account.
            if patch.account_id:
                async for dkey in self._r.scan_iter("accounts:record:*"):
                    dh = await self._r.hgetall(dkey)
                    if not dh:
                        continue
                    daid = dh.get(b"account_id") or dh.get("account_id") or b""
                    if isinstance(daid, bytes):
                        daid = daid.decode()
                    ddel = dh.get(b"deleted_at") or dh.get("deleted_at") or b""
                    if isinstance(ddel, bytes):
                        ddel = ddel.decode()
                    if daid != patch.account_id or ddel:
                        continue
                    dup_token = (
                        dkey.decode() if isinstance(dkey, bytes) else dkey
                    ).split(":", 2)[-1]
                    if dup_token == patch.token:
                        continue
                    await self._r.hset(
                        dkey,
                        mapping={
                            "deleted_at": str(ts),
                            "updated_at": str(ts),
                            "revision": str(rev),
                        },
                    )
                    dpool = dh.get(b"pool") or dh.get("pool") or b"basic"
                    if isinstance(dpool, bytes):
                        dpool = dpool.decode()
                    await self._r.srem(_pool_key(dpool), dup_token)
                    await self._r.zadd(_KEY_REV_LOG, {dup_token: rev})
            count += 1
        return AccountMutationResult(patched=count, revision=rev)

    async def delete_accounts(
        self,
        tokens: list[str],
    ) -> AccountMutationResult:
        if not tokens:
            return AccountMutationResult()
        rev = await self._bump_revision()
        ts = now_ms()
        count = 0
        for token in tokens:
            key = _record_key(token)
            exists = await self._r.hget(key, "deleted_at")
            if exists and exists not in (b"", b"None", None):
                continue  # Already deleted.
            h = await self._r.hgetall(key)
            if not h:
                continue
            pool = h.get(b"pool") or h.get("pool") or b"basic"
            if isinstance(pool, bytes):
                pool = pool.decode()
            await self._r.hset(
                key,
                mapping={
                    "deleted_at": str(ts),
                    "updated_at": str(ts),
                    "revision": str(rev),
                },
            )
            await self._r.srem(_pool_key(pool), token)
            await self._r.zadd(_KEY_REV_LOG, {token: rev})
            count += 1
        return AccountMutationResult(deleted=count, revision=rev)

    async def get_accounts(
        self,
        tokens: list[str],
    ) -> list[AccountRecord]:
        if not tokens:
            return []
        results: list[AccountRecord] = []
        async with self._r.pipeline() as pipe:
            for token in tokens:
                pipe.hgetall(_record_key(token))
            hashes = await pipe.execute()
        for token, h in zip(tokens, hashes, strict=True):
            if h:
                results.append(self._from_hash(token, h))
        return results

    async def list_accounts(
        self,
        query: ListAccountsQuery,
    ) -> AccountPage:
        # Full scan — Redis is not optimised for filtered listing.
        all_records: list[AccountRecord] = []
        pool_filters = {
            p for p in (query.pools or ([query.pool] if query.pool else [])) if p
        }
        async for key in self._r.scan_iter("accounts:record:*"):
            token = (key.decode() if isinstance(key, bytes) else key).split(":", 2)[-1]
            h = await self._r.hgetall(key)
            if not h:
                continue
            r = self._from_hash(token, h)
            if not query.include_deleted and r.is_deleted():
                continue
            if pool_filters and r.pool not in pool_filters:
                continue
            if query.status:
                # In-memory scan, so use the true effective status. The
                # "disabled" chip groups all non-active/non-cooling statuses.
                eff = derive_status(r)
                if query.status == AccountStatus.DISABLED:
                    if eff in (AccountStatus.ACTIVE, AccountStatus.COOLING):
                        continue
                elif eff != query.status:
                    continue
            if query.tags and not set(query.tags).issubset(r.tags):
                continue
            if query.exclude_tags and set(query.exclude_tags) & set(r.tags):
                continue
            all_records.append(r)

        # Sort.
        sort_key = query.sort_by
        all_records.sort(
            key=lambda r: _sort_value(r, sort_key),
            reverse=query.sort_desc,
        )
        total = len(all_records)
        start = (query.page - 1) * query.page_size
        items = all_records[start : start + query.page_size]
        total_pages = max(1, (total + query.page_size - 1) // query.page_size)
        rev = await self.get_revision()
        return AccountPage(
            items=items,
            total=total,
            page=query.page,
            page_size=query.page_size,
            total_pages=total_pages,
            revision=rev,
        )

    async def replace_pool(
        self,
        command: BulkReplacePoolCommand,
    ) -> AccountMutationResult:
        existing = await self._r.smembers(_pool_key(command.pool))
        tokens = [(t.decode() if isinstance(t, bytes) else t) for t in existing]
        deleted_result = await self.delete_accounts(tokens)
        upserted_result = await self.upsert_accounts(command.upserts)
        return AccountMutationResult(
            upserted=upserted_result.upserted,
            deleted=deleted_result.deleted,
            revision=upserted_result.revision,
        )

    async def close(self) -> None:
        """Close the underlying Redis connection pool."""
        await self._r.aclose()


__all__ = ["RedisAccountRepository"]
