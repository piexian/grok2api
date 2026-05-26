import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from app.control.account.backends.local import LocalAccountRepository
from app.control.account.commands import AccountPatch, AccountUpsert, ListAccountsQuery
from app.control.account.enums import AccountStatus, FeedbackKind
from app.control.account.invalid_credentials import mark_account_invalid_credentials
from app.control.account.quota_defaults import (
    default_quota_set,
    supported_mode_ids,
    usage_sync_mode_ids,
)
from app.control.model.enums import ModeId
from app.control.model.registry import resolve
from app.dataplane.account import AccountDirectory
from app.dataplane.account.selector import set_strategy
from app.main import app
from app.platform.errors import UpstreamError
from app.platform.meta import get_project_version


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
        self.assertEqual(get_project_version(), "2.0.4.rc6")

    def test_console_quota_defaults(self):
        self.assertEqual(int(ModeId.CONSOLE), 5)
        self.assertEqual(ModeId.CONSOLE.to_api_str(), "console")
        self.assertEqual(supported_mode_ids("basic"), (1, 5))
        self.assertEqual(usage_sync_mode_ids("basic"), (1,))
        self.assertEqual(supported_mode_ids("super"), (0, 1, 2, 4, 5))
        self.assertEqual(usage_sync_mode_ids("super"), (0, 1, 2, 4))

        console = default_quota_set("basic").console
        self.assertIsNotNone(console)
        self.assertEqual(console.total, 30)
        self.assertEqual(console.window_seconds, 900)
        self.assertEqual(resolve("grok-4.3").mode_id, ModeId.CONSOLE)
        self.assertEqual(resolve("grok-build-0.1").mode_id, ModeId.CONSOLE)
        self.assertEqual(resolve("grok-build-0.1").console_model, "grok-build-0.1")

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
                self.assertEqual(json.loads(body)["version"], "2.0.4.rc6")

                status, body = await _asgi_get("/v1/models")
                self.assertEqual(status, 200)
                ids = {item["id"] for item in json.loads(body)["data"]}
                self.assertIn("grok-4.3", ids)
                self.assertIn("grok-4.20", ids)
                self.assertIn("grok-build-0.1", ids)
            finally:
                await repo.close()
