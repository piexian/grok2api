import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import app.dataplane.account as account_runtime
from app.control.account.backends.local import LocalAccountRepository
from app.control.account.commands import AccountPatch, AccountUpsert, ListAccountsQuery
from app.control.account.enums import AccountStatus, FeedbackKind, QuotaSource
from app.control.account.invalid_credentials import mark_account_invalid_credentials
from app.control.account.models import AccountRecord, QuotaWindow
from app.control.account.quota_defaults import (
    default_quota_set,
    infer_pool,
    supported_mode_ids,
    usage_sync_mode_ids,
)
from app.control.account.refresh import (
    AccountRefreshService,
    RefreshResult,
    _get_accounts_by_tokens,
    _prioritize_refresh_records,
    _quota_probe_mode_ids,
)
from app.control.account.scheduler import (
    AccountRefreshScheduler,
    _POOL_CONFIG,
    _batch_size,
)
from app.control.model.enums import ModeId
from app.control.model.registry import resolve
from app.dataplane.account import AccountDirectory
from app.dataplane.account.selector import set_strategy
from app.main import app
from app.platform.errors import UpstreamError
from app.platform.meta import get_project_version
from app.platform.startup import run_account_backfill_migrations
from app.platform.update_check import _is_newer
from app.products.openai.router import _available_pools
from app.products.web.admin.batch import _prioritize_refresh_tokens


def _merge_nested(base: dict, patch: dict) -> dict:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_nested(merged[key], value)
        else:
            merged[key] = value
    return merged


class _MemoryConfigBackend:
    def __init__(self, data: dict | None = None) -> None:
        self.data = dict(data or {})
        self.patches: list[dict] = []

    async def load(self) -> dict:
        return self.data

    async def apply_patch(self, patch: dict) -> None:
        self.patches.append(patch)
        self.data = _merge_nested(self.data, patch)

    async def version(self) -> int:
        return len(self.patches)


async def _asgi_get(path: str) -> tuple[int, bytes]:
    messages = []
    url = urlsplit(path)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": url.path,
        "raw_path": url.path.encode(),
        "query_string": url.query.encode(),
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "root_path": "",
    }
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    return status, body


class ReleaseSmokeTest(unittest.IsolatedAsyncioTestCase):
    def test_version_metadata(self):
        self.assertEqual(get_project_version(), "2.0.7")

    def test_prerelease_version_update_ordering(self):
        self.assertTrue(_is_newer("2.0.7", "2.0.7-beta"))
        self.assertTrue(_is_newer("2.0.7-rc1", "2.0.7-beta"))
        self.assertFalse(_is_newer("2.0.7-beta", "2.0.7"))

    def test_console_quota_defaults(self):
        self.assertEqual(int(ModeId.CONSOLE), 5)
        self.assertEqual(ModeId.CONSOLE.to_api_str(), "console")
        self.assertEqual(supported_mode_ids("basic"), (1, 5))
        self.assertEqual(usage_sync_mode_ids("basic"), (1,))
        self.assertEqual(_quota_probe_mode_ids("basic"), (0, 1))
        self.assertEqual(supported_mode_ids("super"), (0, 1, 2, 4, 5))
        self.assertEqual(usage_sync_mode_ids("super"), (0, 1, 2, 4))
        self.assertEqual(_quota_probe_mode_ids("super"), (0, 1, 2, 4))

        console = default_quota_set("basic").console
        self.assertIsNotNone(console)
        self.assertEqual(console.total, 30)
        self.assertEqual(console.window_seconds, 900)
        self.assertEqual(resolve("grok-4.3").mode_id, ModeId.CONSOLE)
        self.assertEqual(resolve("grok-build-0.1").mode_id, ModeId.CONSOLE)
        self.assertEqual(resolve("grok-build-0.1").console_model, "grok-build-0.1")

    async def test_account_startup_backfill_skips_completed_markers(self):
        config = _MemoryConfigBackend(
            {
                "startup": {
                    "migrations": {
                        "account_grok_4_3_quota_v1": True,
                        "account_basic_fast_only_quota_v2": True,
                    }
                }
            }
        )

        class RaisingRepo:
            async def list_accounts(self, query):
                raise AssertionError("completed startup migrations must not scan")

        await run_account_backfill_migrations(config, RaisingRepo())
        self.assertEqual(config.patches, [])

    async def test_account_startup_backfill_normalizes_basic_quota(self):
        def win(total: int, window_seconds: int = 7200) -> dict:
            return QuotaWindow(
                remaining=total,
                total=total,
                window_seconds=window_seconds,
                reset_at=None,
                synced_at=None,
                source=QuotaSource.REAL,
            ).to_dict()

        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            try:
                await repo.upsert_accounts(
                    [AccountUpsert(token="basic-token", pool="basic")]
                )
                await repo.patch_accounts(
                    [
                        AccountPatch(
                            token="basic-token",
                            quota_auto=win(50),
                            quota_fast=win(125),
                            quota_expert=win(40),
                            quota_heavy=win(20),
                            quota_grok_4_3=win(50),
                            quota_console={},
                        )
                    ]
                )
                self.assertTrue(await repo.needs_basic_fast_only_quota_normalization())

                config = _MemoryConfigBackend()
                await run_account_backfill_migrations(config, repo)

                records = await repo.get_accounts(["basic-token"])
                qs = records[0].quota_set()
                self.assertEqual(qs.auto.total, 0)
                self.assertEqual(qs.fast.total, 30)
                self.assertEqual(qs.fast.window_seconds, 86400)
                self.assertEqual(qs.expert.total, 0)
                self.assertIsNone(qs.heavy)
                self.assertIsNone(qs.grok_4_3)
                self.assertIsNotNone(qs.console)
                self.assertEqual(qs.console.total, 30)
                self.assertEqual(qs.console.window_seconds, 900)
                self.assertFalse(
                    await repo.needs_basic_fast_only_quota_normalization()
                )
                migrations = config.data["startup"]["migrations"]
                self.assertTrue(migrations["account_grok_4_3_quota_v1"])
                self.assertTrue(migrations["account_basic_fast_only_quota_v2"])
            finally:
                await repo.close()

    def test_pool_inference_from_quota_modes(self):
        def win(total: int) -> QuotaWindow:
            return QuotaWindow(
                remaining=total,
                total=total,
                window_seconds=7200,
                reset_at=None,
                synced_at=None,
                source=QuotaSource.REAL,
            )

        self.assertEqual(infer_pool({1: win(30)}), "basic")
        self.assertEqual(infer_pool({1: win(139), 2: win(40), 3: win(20)}), "basic")
        self.assertEqual(infer_pool({0: win(50), 1: win(125)}), "super")
        self.assertEqual(
            infer_pool({0: win(50), 1: win(125), 2: win(40), 3: win(20), 4: win(50)}),
            "super",
        )
        self.assertEqual(
            infer_pool({0: win(150), 1: win(400), 2: win(150), 3: win(20), 4: win(150)}),
            "heavy",
        )

    def test_refresh_record_priority_prefers_paid_pools(self):
        records = [
            AccountRecord(token="basic-b", pool="basic"),
            AccountRecord(token="super-a", pool="super"),
            AccountRecord(token="heavy-b", pool="heavy"),
            AccountRecord(token="lite-a", pool="lite"),
            AccountRecord(token="heavy-a", pool="heavy"),
            AccountRecord(token="basic-a", pool="basic"),
        ]

        ordered = _prioritize_refresh_records(records)

        self.assertEqual(
            [record.token for record in ordered],
            ["heavy-a", "heavy-b", "super-a", "lite-a", "basic-a", "basic-b"],
        )
        self.assertEqual(list(_POOL_CONFIG), ["heavy", "super", "lite", "basic"])

    async def test_refresh_account_lookup_is_chunked(self):
        class TrackingRepo:
            def __init__(self) -> None:
                self.calls: list[list[str]] = []

            async def get_accounts(self, tokens: list[str]) -> list[AccountRecord]:
                self.calls.append(tokens)
                return [AccountRecord(token=token) for token in tokens]

        repo = TrackingRepo()
        tokens = [f"tok_{i}" for i in range(1205)]

        records = await _get_accounts_by_tokens(repo, tokens)  # type: ignore[arg-type]

        self.assertEqual(len(records), len(tokens))
        self.assertEqual([len(call) for call in repo.calls], [500, 500, 205])

    async def test_admin_batch_refresh_prioritizes_tokens_by_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            await repo.upsert_accounts(
                [
                    AccountUpsert(token="tok_basic_2", pool="basic"),
                    AccountUpsert(token="tok_super", pool="super"),
                    AccountUpsert(token="tok_heavy", pool="heavy"),
                    AccountUpsert(token="tok_lite", pool="lite"),
                    AccountUpsert(token="tok_basic_1", pool="basic"),
                ]
            )

            ordered = await _prioritize_refresh_tokens(
                repo,
                [
                    "tok_basic_2",
                    "tok_super",
                    "tok_missing",
                    "tok_heavy",
                    "tok_lite",
                    "tok_basic_1",
                ],
            )

            self.assertEqual(
                ordered,
                [
                    "tok_heavy",
                    "tok_super",
                    "tok_lite",
                    "tok_basic_2",
                    "tok_basic_1",
                    "tok_missing",
                ],
            )
            await repo.close()

    async def test_refresh_scheduler_advances_large_pool_in_batches(self):
        class FakeRefreshService:
            def __init__(self, checked: int) -> None:
                self.checked = checked
                self.cursor: str | None = "tok_0500"
                self.calls: list[dict[str, int | str | None]] = []

            async def refresh_scheduled(
                self,
                pool: str | None = None,
                *,
                limit: int | None = None,
                after_token: str | None = None,
            ) -> RefreshResult:
                self.calls.append(
                    {"pool": pool, "limit": limit, "after_token": after_token}
                )
                return RefreshResult(checked=self.checked, cursor=self.cursor)

        batch_size = _batch_size()
        fake = FakeRefreshService(checked=batch_size)
        scheduler = AccountRefreshScheduler(fake)  # type: ignore[arg-type]

        await scheduler._refresh_pool_batch("basic")

        self.assertEqual(
            fake.calls[-1],
            {"pool": "basic", "limit": batch_size, "after_token": None},
        )
        self.assertEqual(scheduler._cursors["basic"], "tok_0500")
        self.assertEqual(scheduler._next_due["basic"], 0.0)

        fake.checked = 1
        fake.cursor = "tok_0501"
        await scheduler._refresh_pool_batch("basic")

        self.assertEqual(
            fake.calls[-1],
            {"pool": "basic", "limit": batch_size, "after_token": "tok_0500"},
        )
        self.assertIsNone(scheduler._cursors["basic"])

    async def test_local_repo_directory_console_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            await repo.upsert_accounts(
                [AccountUpsert(token="tok_console", pool="basic", tags=["smoke"])]
            )

            records = await repo.get_accounts(["tok_console"])
            self.assertEqual(len(records), 1)
            qs = records[0].quota_set()
            self.assertIsNotNone(qs.console)
            self.assertEqual(qs.console.remaining, 30)
            self.assertEqual(qs.console.total, 30)
            self.assertEqual(qs.console.window_seconds, 900)

            self.assertEqual(await repo.get_global_success_count(), 0)
            self.assertEqual(await repo.increment_global_success_count(2), 2)
            self.assertEqual(await repo.increment_global_success_count(0), 2)

            await repo.patch_accounts(
                [
                    AccountPatch(
                        token="tok_console",
                        quota_console={
                            "remaining": 7,
                            "total": 30,
                            "window_seconds": 900,
                            "reset_at": None,
                            "synced_at": None,
                            "source": 0,
                        },
                    )
                ]
            )
            records = await repo.get_accounts(["tok_console"])
            self.assertEqual(records[0].quota_set().console.remaining, 7)

            set_strategy("quota")
            directory = AccountDirectory(repo)
            await directory.bootstrap()
            lease = await directory.reserve(
                (0,), int(ModeId.CONSOLE), now_s_override=1000
            )
            self.assertIsNotNone(lease)
            self.assertEqual(lease.token, "tok_console")
            await directory.release(lease)
            await directory.feedback(
                "tok_console",
                FeedbackKind.SUCCESS,
                int(ModeId.CONSOLE),
                now_s_val=1001,
            )
            self.assertEqual(await repo.get_global_success_count(), 3)

            table = directory._table
            idx = table.idx_by_token["tok_console"]
            self.assertEqual(table.quota_console_by_idx[idx], 6)
            self.assertIn((0, int(ModeId.CONSOLE)), table.mode_available)

            page = await repo.list_accounts(ListAccountsQuery(page=1, page_size=10))
            self.assertEqual(page.total, 1)
            await repo.close()

    async def test_console_quota_reset_timer_starts_at_low_remaining_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            try:
                await repo.upsert_accounts(
                    [AccountUpsert(token="tok_console_timer", pool="basic")]
                )
                await repo.patch_accounts(
                    [
                        AccountPatch(
                            token="tok_console_timer",
                            quota_console={
                                "remaining": 17,
                                "total": 30,
                                "window_seconds": 900,
                                "reset_at": None,
                                "synced_at": None,
                                "source": int(QuotaSource.DEFAULT),
                            },
                        )
                    ]
                )

                svc = AccountRefreshService(repo)
                await svc.refresh_call_async(
                    "tok_console_timer",
                    int(ModeId.CONSOLE),
                )
                record = (await repo.get_accounts(["tok_console_timer"]))[0]
                console = record.quota_set().console
                self.assertIsNotNone(console)
                self.assertEqual(console.remaining, 16)
                self.assertIsNone(console.reset_at)

                await svc.refresh_call_async(
                    "tok_console_timer",
                    int(ModeId.CONSOLE),
                )
                record = (await repo.get_accounts(["tok_console_timer"]))[0]
                console = record.quota_set().console
                self.assertIsNotNone(console)
                self.assertEqual(console.remaining, 15)
                self.assertIsNotNone(console.reset_at)
            finally:
                await repo.close()

    async def test_admin_tokens_includes_console_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            await repo.upsert_accounts(
                [AccountUpsert(token="tok_admin_console", pool="basic")]
            )
            await repo.patch_accounts(
                [
                    AccountPatch(
                        token="tok_admin_console",
                        quota_console={
                            "remaining": 9,
                            "total": 30,
                            "window_seconds": 900,
                            "reset_at": None,
                            "synced_at": None,
                            "source": 2,
                        },
                    )
                ]
            )

            app.state.repository = repo
            try:
                status, body = await _asgi_get("/admin/api/tokens?app_key=grok2api")
                self.assertEqual(status, 200)
                tokens = json.loads(body)["tokens"]
                token = next(t for t in tokens if t["token"] == "tok_admin_console")
                self.assertEqual(token["quota"]["console"]["remaining"], 9)
                self.assertEqual(token["quota"]["console"]["source"], 2)
            finally:
                await repo.close()

    async def test_admin_stats_call_total_survives_deleted_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            await repo.upsert_accounts([AccountUpsert(token="tok_deleted", pool="basic")])
            await repo.patch_accounts(
                [AccountPatch(token="tok_deleted", usage_use_delta=5)]
            )
            await repo.increment_global_success_count(5)

            directory = AccountDirectory(repo)
            await directory.bootstrap()
            await repo.delete_accounts(["tok_deleted"])

            previous_directory = account_runtime._directory
            account_runtime._directory = directory
            app.state.repository = repo
            try:
                status, body = await _asgi_get("/admin/api/stats?app_key=grok2api")
                self.assertEqual(status, 200)
                stats = json.loads(body)
                self.assertEqual(stats["total"], 0)
                self.assertEqual(stats["calls"], 5)
                self.assertEqual(stats["success"], 5)
            finally:
                account_runtime._directory = previous_directory
                await repo.close()

    async def test_invalid_credentials_auto_disable_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            await repo.upsert_accounts([AccountUpsert(token="tok_invalid")])

            marked = await mark_account_invalid_credentials(
                repo,
                "tok_invalid",
                UpstreamError(
                    "invalid credentials",
                    status=401,
                    body="invalid-credentials",
                ),
                source="smoke",
            )

            records = await repo.get_accounts(["tok_invalid"])
            self.assertTrue(marked)
            self.assertEqual(records[0].status, AccountStatus.DISABLED)
            self.assertEqual(records[0].ext["disabled_reason"], "invalid_credentials")
            self.assertNotIn("expired_reason", records[0].ext)
            await repo.close()

    async def test_available_pools_uses_runtime_table_when_bootstrapped(self):
        class RaisingRepo:
            async def runtime_snapshot(self):
                raise AssertionError("runtime_snapshot should not be used")

        previous_directory = account_runtime._directory
        previous_repo = getattr(app.state, "repository", None)
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            await repo.upsert_accounts(
                [
                    AccountUpsert(token="tok_basic_pool", pool="basic"),
                    AccountUpsert(token="tok_heavy_pool", pool="heavy"),
                    AccountUpsert(token="tok_disabled_pool", pool="super"),
                ]
            )
            await repo.patch_accounts(
                [
                    AccountPatch(
                        token="tok_disabled_pool",
                        status=AccountStatus.DISABLED,
                    )
                ]
            )
            directory = AccountDirectory(repo)
            await directory.bootstrap()
            account_runtime._directory = directory
            app.state.repository = RaisingRepo()

            try:
                request = SimpleNamespace(app=app)
                self.assertEqual(await _available_pools(request), frozenset({"basic", "heavy"}))
            finally:
                account_runtime._directory = previous_directory
                app.state.repository = previous_repo
                await repo.close()

    async def test_asgi_health_meta_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            await repo.upsert_accounts([AccountUpsert(token="tok_http", pool="basic")])
            app.state.repository = repo
            try:
                status, body = await _asgi_get("/health")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), {"status": "ok"})

                status, body = await _asgi_get("/meta")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["version"], "2.0.7")

                status, body = await _asgi_get("/v1/models")
                self.assertEqual(status, 200)
                ids = {item["id"] for item in json.loads(body)["data"]}
                self.assertIn("grok-4.3", ids)
                self.assertIn("grok-4.20", ids)
                self.assertIn("grok-build-0.1", ids)
            finally:
                await repo.close()
