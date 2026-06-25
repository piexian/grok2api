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
    CONSOLE_LIMIT,
    CONSOLE_RECOVERY_REMAINING_THRESHOLD,
    CONSOLE_WINDOW_SECONDS,
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
from app.dataplane.account.selector import current_strategy, set_strategy
from app.main import app
from app.platform.errors import UpstreamError, ValidationError
from app.platform.meta import get_project_version
from app.platform.startup import run_account_backfill_migrations
from app.platform.update_check import _is_newer
from app.products.openai import images as image_service
from app.products.openai import video as video_service
from app.products.openai.router import _available_pools
from app.products.openai.router import _combine_image_edit_uploads
from app.products.openai.router import _parse_image_edit_request
from app.products.openai.router import _parse_xai_video_generation_request
from app.products.openai.router import _parse_videos_create_request
from app.products.openai.schemas import ImageGenerationRequest
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
        self.assertEqual(get_project_version(), "2.0.12")

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
        self.assertEqual(supported_mode_ids("super"), (0, 1, 2, 5))
        self.assertEqual(usage_sync_mode_ids("super"), (0, 1, 2))
        self.assertEqual(_quota_probe_mode_ids("super"), (0, 1, 2))

        console = default_quota_set("basic").console
        self.assertIsNotNone(console)
        self.assertEqual(CONSOLE_LIMIT, 20)
        self.assertEqual(CONSOLE_WINDOW_SECONDS, 3600)
        self.assertEqual(CONSOLE_RECOVERY_REMAINING_THRESHOLD, 12)
        self.assertEqual(console.total, CONSOLE_LIMIT)
        self.assertEqual(console.window_seconds, CONSOLE_WINDOW_SECONDS)
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
                        "account_console_quota_v1": True,
                        "account_console_quota_v2": True,
                        "account_console_quota_v3": True,
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
                self.assertEqual(qs.console.total, 20)
                self.assertEqual(qs.console.window_seconds, 3600)
                self.assertFalse(
                    await repo.needs_basic_fast_only_quota_normalization()
                )
                migrations = config.data["startup"]["migrations"]
                self.assertTrue(migrations["account_grok_4_3_quota_v1"])
                self.assertTrue(migrations["account_basic_fast_only_quota_v2"])
                self.assertTrue(migrations["account_console_quota_v1"])
                self.assertTrue(migrations["account_console_quota_v2"])
                self.assertTrue(migrations["account_console_quota_v3"])
            finally:
                await repo.close()

    async def test_account_startup_backfills_console_quota_after_old_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            try:
                await repo.upsert_accounts(
                    [
                        AccountUpsert(token="basic-console-empty", pool="basic"),
                        AccountUpsert(token="basic-console-expired", pool="basic"),
                    ]
                )
                await repo.patch_accounts(
                    [
                        AccountPatch(token="basic-console-empty", quota_console={}),
                        AccountPatch(
                            token="basic-console-expired",
                            quota_console={
                                "remaining": 0,
                                "total": 30,
                                "window_seconds": 900,
                                "reset_at": 1,
                                "synced_at": None,
                                "source": 2,
                            },
                        ),
                    ]
                )

                config = _MemoryConfigBackend(
                    {
                        "startup": {
                            "migrations": {
                                "account_grok_4_3_quota_v1": True,
                                "account_basic_fast_only_quota_v2": True,
                                "account_console_quota_v1": True,
                                "account_console_quota_v2": True,
                            }
                        }
                    }
                )
                await run_account_backfill_migrations(config, repo)

                records = await repo.get_accounts(
                    ["basic-console-empty", "basic-console-expired"]
                )
                for record in records:
                    console = record.quota_set().console
                    self.assertIsNotNone(console)
                    self.assertEqual(console.remaining, 20)
                    self.assertEqual(console.total, 20)
                    self.assertEqual(console.window_seconds, 3600)
                self.assertTrue(
                    config.data["startup"]["migrations"]["account_console_quota_v3"]
                )
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
            self.assertEqual(qs.console.remaining, 20)
            self.assertEqual(qs.console.total, 20)
            self.assertEqual(qs.console.window_seconds, 3600)

            self.assertEqual(await repo.get_global_success_count(), 0)
            self.assertEqual(await repo.increment_global_success_count(2), 2)
            self.assertEqual(await repo.increment_global_success_count(0), 2)

            await repo.patch_accounts(
                [
                    AccountPatch(
                        token="tok_console",
                        quota_console={
                            "remaining": 7,
                            "total": 20,
                            "window_seconds": 3600,
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

    async def test_console_reserve_any_ignores_global_cooling(self):
        previous = current_strategy()
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            try:
                await repo.upsert_accounts(
                    [AccountUpsert(token="tok_console_cooling", pool="basic")]
                )
                directory = AccountDirectory(repo)
                await directory.bootstrap()
                table = directory._table
                idx = table.idx_by_token["tok_console_cooling"]
                table.cooling_until_s_by_idx[idx] = 2000

                set_strategy("random")
                self.assertIsNone(
                    await directory.reserve_any((0,), now_s_override=1000)
                )
                lease = await directory.reserve_any(
                    (0,),
                    console_model="grok-4.3",
                    now_s_override=1000,
                )
                self.assertIsNotNone(lease)
                self.assertEqual(lease.token, "tok_console_cooling")
                await directory.release(lease)

                set_strategy("quota")
                lease = await directory.reserve_any(
                    (0,),
                    console_model="grok-4.3",
                    now_s_override=1000,
                )
                self.assertIsNotNone(lease)
                self.assertEqual(lease.token, "tok_console_cooling")
                await directory.release(lease)
            finally:
                set_strategy(previous)
                await repo.close()

    async def test_console_reserve_any_uses_console_quota(self):
        previous = current_strategy()
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            try:
                await repo.upsert_accounts(
                    [
                        AccountUpsert(token="tok_console_empty", pool="basic"),
                        AccountUpsert(token="tok_console_available", pool="basic"),
                    ]
                )
                await repo.patch_accounts(
                    [
                        AccountPatch(
                            token="tok_console_empty",
                            quota_console={
                                "remaining": 0,
                                "total": 20,
                                "window_seconds": 3600,
                                "reset_at": None,
                                "synced_at": None,
                                "source": int(QuotaSource.DEFAULT),
                            },
                        )
                    ]
                )

                directory = AccountDirectory(repo)
                await directory.bootstrap()
                for strategy in ("quota", "random"):
                    set_strategy(strategy)
                    lease = await directory.reserve_any(
                        (0,),
                        console_model="grok-4.3",
                        now_s_override=1000,
                    )
                    self.assertIsNotNone(lease)
                    self.assertEqual(lease.token, "tok_console_available")
                    await directory.release(lease)
                    await directory.feedback(
                        lease.token,
                        FeedbackKind.SUCCESS,
                        int(ModeId.CONSOLE),
                        now_s_val=1001,
                        console_model="grok-4.3",
                    )

                table = directory._table
                idx_empty = table.idx_by_token["tok_console_empty"]
                idx_available = table.idx_by_token["tok_console_available"]
                self.assertEqual(table.quota_console_by_idx[idx_empty], 0)
                self.assertEqual(table.quota_console_by_idx[idx_available], 18)
            finally:
                set_strategy(previous)
                await repo.close()

    async def test_console_rate_limit_can_exhaust_local_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            try:
                await repo.upsert_accounts(
                    [AccountUpsert(token="tok_console_limited", pool="basic")]
                )
                directory = AccountDirectory(repo)
                await directory.bootstrap()
                exc = UpstreamError("free limit", status=429)
                exc.details["console"] = {"free_limit": True}

                await directory.feedback(
                    "tok_console_limited",
                    FeedbackKind.RATE_LIMITED,
                    int(ModeId.CONSOLE),
                    now_s_val=1000,
                    console_model="grok-4.3",
                    upstream_error=exc,
                )

                table = directory._table
                idx = table.idx_by_token["tok_console_limited"]
                self.assertEqual(table.quota_console_by_idx[idx], 0)
                self.assertEqual(table.reset_console_at_by_idx[idx], 4600)
            finally:
                await repo.close()

    async def test_console_success_sync_records_usage_with_local_quota_decrement(self):
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
                                "total": 20,
                                "window_seconds": 3600,
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
                self.assertEqual(record.usage_use_count, 1)
                self.assertIsNotNone(record.last_use_at)
            finally:
                await repo.close()

    def test_image_generation_request_accepts_openai_output_options(self):
        req = ImageGenerationRequest(
            model="grok-imagine-image",
            prompt="make an icon",
            quality="high",
            output_format="png",
            output_compression=50,
            background="auto",
            moderation="low",
        )

        self.assertIsNone(req.response_format)
        self.assertEqual(
            image_service.normalize_image_response_format(
                req.response_format,
                output_format=req.output_format,
            ),
            "b64_json",
        )
        image_service.validate_image_output_options(
            quality=req.quality,
            output_format=req.output_format,
            output_compression=req.output_compression,
            background=req.background,
            moderation=req.moderation,
        )

        with self.assertRaises(ValidationError) as ctx:
            image_service.validate_image_output_options(output_format="gif")
        self.assertEqual(ctx.exception.param, "output_format")

    def test_image_edit_upload_combines_openai_field_names(self):
        image_array_item = SimpleNamespace(filename="array.png")
        image_item = SimpleNamespace(filename="single.png")

        uploads = _combine_image_edit_uploads([image_array_item], [image_item])

        self.assertEqual(uploads, [image_item, image_array_item])
        with self.assertRaises(ValidationError) as ctx:
            _combine_image_edit_uploads(None, None)
        self.assertEqual(ctx.exception.param, "image")

    def test_xai_imagine_model_aliases(self):
        image = resolve("grok-imagine-image-quality")
        self.assertTrue(image.is_image())
        self.assertTrue(image.is_image_edit())
        self.assertTrue(resolve("grok-imagine-video-1.5").is_video())

    async def test_xai_image_edit_json_parser(self):
        class FakeRequest:
            headers = {"content-type": "application/json; charset=utf-8"}

            async def json(self):
                return {
                    "model": "grok-imagine-image-quality",
                    "prompt": "make it cinematic",
                    "image": {"type": "image_url", "url": "https://example.test/a.png"},
                    "aspect_ratio": "16:9",
                    "resolution": "2k",
                    "response_format": "b64_json",
                }

        payload = await _parse_image_edit_request(FakeRequest())

        self.assertEqual(payload["model"], "grok-imagine-image-quality")
        self.assertEqual(payload["image_inputs"], ["https://example.test/a.png"])
        self.assertEqual(payload["aspect_ratio"], "16:9")
        self.assertEqual(payload["resolution"], "2k")
        self.assertEqual(payload["response_format"], "b64_json")

    async def test_xai_image_edit_json_parser_rejects_empty_image_and_bad_n(self):
        class FakeRequest:
            headers = {"content-type": "application/json"}

            def __init__(self, body: dict):
                self.body = body

            async def json(self):
                return self.body

        base = {
            "model": "grok-imagine-image-quality",
            "prompt": "make it cinematic",
            "image": {"url": "https://example.test/a.png"},
        }

        with self.assertRaises(ValidationError) as empty_ctx:
            await _parse_image_edit_request(FakeRequest({**base, "image": []}))
        self.assertEqual(empty_ctx.exception.param, "image")

        with self.assertRaises(ValidationError) as n_ctx:
            await _parse_image_edit_request(FakeRequest({**base, "n": 0}))
        self.assertEqual(n_ctx.exception.param, "n")

    async def test_xai_video_generation_json_parser(self):
        class FakeRequest:
            headers = {"content-type": "application/json"}

            async def json(self):
                return {
                    "model": "grok-imagine-video-1.5",
                    "prompt": "pan across a city",
                    "duration": 10,
                    "aspect_ratio": "4:3",
                    "resolution": "480p",
                    "image": {"url": "https://example.test/start.png"},
                }

        payload = await _parse_xai_video_generation_request(FakeRequest())

        self.assertEqual(payload["model"], "grok-imagine-video-1.5")
        self.assertEqual(payload["seconds"], 10)
        self.assertEqual(payload["size"], "1024x768")
        self.assertEqual(payload["resolution_name"], "480p")
        self.assertEqual(
            payload["input_references"],
            [{"image_url": "https://example.test/start.png"}],
        )

    async def test_video_job_openai_shape_list_and_delete(self):
        async with video_service._VIDEO_JOBS_LOCK:
            video_service._VIDEO_JOBS.clear()

        try:
            await video_service._put_video_job(
                video_service._VideoJob(
                    id="video_smoke_1",
                    model="grok-imagine-video",
                    prompt="test one",
                    seconds="6",
                    size="720x1280",
                    quality="standard",
                    created_at=100,
                    status="completed",
                    progress=100,
                    completed_at=120,
                    expires_at=3720,
                )
            )
            await video_service._put_video_job(
                video_service._VideoJob(
                    id="video_smoke_2",
                    model="grok-imagine-video",
                    prompt="test two",
                    seconds="10",
                    size="1280x720",
                    quality="standard",
                    created_at=200,
                    expires_at=3800,
                )
            )

            body = await video_service.retrieve("video_smoke_1")
            self.assertEqual(body["object"], "video")
            self.assertEqual(body["status"], "completed")
            self.assertEqual(body["seconds"], "6")
            self.assertEqual(body["expires_at"], 3720)

            page = await video_service.list_videos(limit=1, order="asc")
            self.assertEqual(page["object"], "list")
            self.assertEqual(page["first_id"], "video_smoke_1")
            self.assertEqual(page["last_id"], "video_smoke_1")
            self.assertTrue(page["has_more"])

            next_page = await video_service.list_videos(
                limit=10,
                after="video_smoke_1",
                order="asc",
            )
            self.assertEqual(
                [item["id"] for item in next_page["data"]],
                ["video_smoke_2"],
            )

            deleted = await video_service.delete_video("video_smoke_1")
            self.assertEqual(
                deleted,
                {"id": "video_smoke_1", "object": "video.deleted", "deleted": True},
            )
            with self.assertRaises(ValidationError):
                await video_service.retrieve("video_smoke_1")
        finally:
            async with video_service._VIDEO_JOBS_LOCK:
                video_service._VIDEO_JOBS.clear()

    async def test_video_content_supports_thumbnail_variant(self):
        async with video_service._VIDEO_JOBS_LOCK:
            video_service._VIDEO_JOBS.clear()

        with tempfile.TemporaryDirectory() as tmp:
            thumbnail_path = Path(tmp) / "thumb.jpg"
            thumbnail_path.write_bytes(b"thumbnail")
            try:
                await video_service._put_video_job(
                    video_service._VideoJob(
                        id="video_thumb",
                        model="grok-imagine-video",
                        prompt="test",
                        seconds="6",
                        size="720x1280",
                        quality="standard",
                        created_at=100,
                        status="completed",
                        progress=100,
                        thumbnail_path=str(thumbnail_path),
                        thumbnail_mime="image/jpeg",
                    )
                )

                path = await video_service.content_path(
                    "video_thumb",
                    variant="thumbnail",
                )
                self.assertEqual(path, thumbnail_path)
            finally:
                async with video_service._VIDEO_JOBS_LOCK:
                    video_service._VIDEO_JOBS.clear()

    async def test_video_content_rejects_unavailable_spritesheet_variant(self):
        with self.assertRaises(ValidationError) as ctx:
            await video_service.content_path("video_missing", variant="spritesheet")
        self.assertEqual(ctx.exception.param, "variant")

    async def test_video_create_json_parser_preserves_input_reference(self):
        class FakeRequest:
            headers = {"content-type": "application/json; charset=utf-8"}

            async def json(self):
                return {
                    "model": "grok-imagine-video",
                    "prompt": "make a video",
                    "seconds": "6",
                    "input_reference": {"image_url": "https://example.test/a.png"},
                }

        payload = await _parse_videos_create_request(FakeRequest())

        self.assertEqual(payload["model"], "grok-imagine-video")
        self.assertEqual(payload["prompt"], "make a video")
        self.assertEqual(payload["seconds"], "6")
        self.assertEqual(
            payload["input_references"],
            {"image_url": "https://example.test/a.png"},
        )

    async def test_xai_video_status_response_shape(self):
        async with video_service._VIDEO_JOBS_LOCK:
            video_service._VIDEO_JOBS.clear()

        try:
            await video_service._put_video_job(
                video_service._VideoJob(
                    id="xai_video_smoke",
                    model="grok-imagine-video-1.5",
                    prompt="test",
                    seconds="10",
                    size="1280x720",
                    quality="standard",
                    created_at=100,
                    status="completed",
                    progress=100,
                    video_url="https://vidgen.x.ai/video.mp4",
                    api_style="xai",
                )
            )

            body = await video_service.retrieve_xai_video("xai_video_smoke")
            self.assertEqual(body["status"], "done")
            self.assertEqual(body["model"], "grok-imagine-video-1.5")
            self.assertEqual(body["video"]["url"], "https://vidgen.x.ai/video.mp4")
            self.assertEqual(body["video"]["duration"], 10)
            self.assertTrue(body["video"]["respect_moderation"])
            self.assertEqual(
                await video_service.retrieve_xai_video("xai_video_missing"),
                {"status": "expired"},
            )
        finally:
            async with video_service._VIDEO_JOBS_LOCK:
                video_service._VIDEO_JOBS.clear()

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
                            "total": 20,
                            "window_seconds": 3600,
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
            await repo.upsert_accounts(
                [
                    AccountUpsert(token="tok_http_basic", pool="basic"),
                    AccountUpsert(token="tok_http_super", pool="super"),
                    AccountUpsert(token="tok_http_heavy", pool="heavy"),
                ]
            )
            app.state.repository = repo
            try:
                status, body = await _asgi_get("/health")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), {"status": "ok"})

                status, body = await _asgi_get("/meta")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["version"], "2.0.12")

                status, body = await _asgi_get("/v1/models")
                self.assertEqual(status, 200)
                ids = {item["id"] for item in json.loads(body)["data"]}
                self.assertIn("grok-4.3-fast", ids)
                self.assertIn("grok-4.3-auto", ids)
                self.assertIn("grok-4.3-expert", ids)
                self.assertIn("grok-4.3-heavy", ids)
                self.assertIn("grok-4.3", ids)
                self.assertIn("grok-build-0.1", ids)
                self.assertIn("grok-4.20-0309-non-reasoning", ids)
                self.assertIn("grok-4.20-0309-reasoning", ids)
                self.assertIn("grok-4.20-multi-agent-0309", ids)
                self.assertNotIn("grok-4.20-auto", ids)
                self.assertNotIn("grok-4.3-beta", ids)
            finally:
                await repo.close()
