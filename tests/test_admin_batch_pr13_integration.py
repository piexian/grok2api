import asyncio
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import orjson

from app.control.account.backends.redis import RedisAccountRepository
from app.control.account.enums import AccountStatus
from app.control.account.models import AccountMutationResult, AccountPage, AccountRecord
from app.control.account.refresh import RefreshResult
from app.platform.errors import ValidationError
from app.products.web.admin import tokens as admin_tokens
from app.products.web.admin.batch import BatchRequest, batch_refresh, batch_nsfw


class _Repo:
    def __init__(self) -> None:
        self.records = {
            "active-token": AccountRecord(token="active-token", status=AccountStatus.ACTIVE, pool="basic"),
            "cooling-token": AccountRecord(token="cooling-token", status=AccountStatus.COOLING, pool="super"),
            "disabled-token": AccountRecord(token="disabled-token", status=AccountStatus.DISABLED, pool="basic"),
            "expired-token": AccountRecord(token="expired-token", status=AccountStatus.EXPIRED, pool="basic"),
        }
        self.requested_tokens: list[str] = []
        self.get_accounts_calls: list[list[str]] = []
        self.deleted_tokens: list[str] = []

    async def get_accounts(self, tokens: list[str]) -> list[AccountRecord]:
        self.requested_tokens = tokens
        self.get_accounts_calls.append(tokens)
        return [self.records[token] for token in tokens if token in self.records]

    async def list_accounts(self, query) -> AccountPage:
        return AccountPage(
            items=list(self.records.values()),
            total=len(self.records),
            page=query.page,
            page_size=query.page_size,
            total_pages=1,
        )

    async def delete_accounts(self, tokens: list[str]) -> AccountMutationResult:
        self.deleted_tokens = tokens
        return AccountMutationResult(deleted=len(tokens), revision=9)


class _RefreshService:
    def __init__(self) -> None:
        self.refreshed_tokens: list[str] = []

    async def refresh_tokens(self, tokens: list[str]) -> RefreshResult:
        self.refreshed_tokens.extend(tokens)
        return RefreshResult(refreshed=len(tokens))


class _Pipeline:
    def __init__(self, redis: "_Redis") -> None:
        self.redis = redis
        self.keys: list[str] = []

    async def __aenter__(self) -> "_Pipeline":
        self.redis.pipeline_count += 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def hgetall(self, key: str) -> None:
        self.keys.append(key)

    async def execute(self) -> list[dict[str, str]]:
        return [self.redis.hashes.get(key, {}) for key in self.keys]


class _Redis:
    def __init__(self) -> None:
        active = AccountRecord(token="active-token", status=AccountStatus.ACTIVE)
        self.hashes = {
            "accounts:record:active-token": RedisAccountRepository._to_hash(active, revision=7),
        }
        self.pipeline_count = 0
        self.hgetall_count = 0

    def pipeline(self) -> _Pipeline:
        return _Pipeline(self)

    async def hgetall(self, key: str) -> dict[str, str]:
        self.hgetall_count += 1
        return self.hashes.get(key, {})


class AdminBatchPr13IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_refresh_filters_non_manageable_explicit_tokens(self):
        repo = _Repo()
        refresh_svc = _RefreshService()

        response = await batch_refresh(
            BatchRequest(tokens=["active-token", "disabled-token"]),
            async_mode=False,
            all_manageable=False,
            concurrency=None,
            refresh_svc=refresh_svc,
            repo=repo,
        )

        body = orjson.loads(response.body)
        self.assertEqual(
            repo.get_accounts_calls,
            [["active-token", "disabled-token"], ["active-token"]],
        )
        self.assertEqual(refresh_svc.refreshed_tokens, ["active-token"])
        self.assertEqual(body["summary"], {"total": 1, "ok": 1, "fail": 0})

    async def test_batch_refresh_rejects_only_non_manageable_explicit_tokens(self):
        repo = _Repo()
        refresh_svc = _RefreshService()

        with self.assertRaises(ValidationError) as cm:
            await batch_refresh(
                BatchRequest(tokens=["disabled-token"]),
                async_mode=False,
                all_manageable=False,
                concurrency=None,
                refresh_svc=refresh_svc,
                repo=repo,
            )

        self.assertIn("No manageable tokens available", str(cm.exception))
        self.assertEqual(refresh_svc.refreshed_tokens, [])

    async def test_batch_nsfw_all_manageable_uses_repository_scope(self):
        repo = _Repo()
        called: list[tuple[str, bool]] = []

        async def _fake_nsfw_one(_repo, token: str, enabled: bool) -> dict:
            called.append((token, enabled))
            return {"success": True}

        with patch("app.products.web.admin.batch._nsfw_one", _fake_nsfw_one):
            response = await batch_nsfw(
                BatchRequest(tokens=[]),
                async_mode=False,
                all_manageable=True,
                concurrency=99,
                enabled=True,
                repo=repo,
            )

        body = orjson.loads(response.body)
        self.assertEqual(called, [("active-token", True), ("cooling-token", True)])
        self.assertEqual(body["summary"], {"total": 2, "ok": 2, "fail": 0})

    async def test_delete_invalid_tokens_only_deletes_expired_status(self):
        repo = _Repo()

        response = await admin_tokens.delete_invalid_tokens(repo=repo)

        self.assertEqual(orjson.loads(response.body), {"deleted": 1})
        self.assertEqual(repo.deleted_tokens, ["expired-token"])


class RedisRepositoryPr13IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_accounts_reads_many_tokens_with_one_pipeline(self):
        redis = _Redis()
        repo = RedisAccountRepository(redis)

        records = await repo.get_accounts(["active-token", "missing-token"])

        self.assertEqual([record.token for record in records], ["active-token"])
        self.assertEqual(redis.pipeline_count, 1)
        self.assertEqual(redis.hgetall_count, 0)

    async def test_get_accounts_empty_list_skips_redis(self):
        redis = _Redis()
        repo = RedisAccountRepository(redis)

        records = await repo.get_accounts([])

        self.assertEqual(records, [])
        self.assertEqual(redis.pipeline_count, 0)
        self.assertEqual(redis.hgetall_count, 0)


class AdminTokenBackgroundTaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        admin_tokens._background_tasks.clear()

    async def asyncTearDown(self) -> None:
        pending = list(admin_tokens._background_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        admin_tokens._background_tasks.clear()

    async def test_fire_and_forget_keeps_task_until_completion(self):
        release = asyncio.Event()

        async def _wait() -> None:
            await release.wait()

        task = admin_tokens._fire_and_forget(_wait())

        self.assertIn(task, admin_tokens._background_tasks)
        release.set()
        await task
        await asyncio.sleep(0)
        self.assertNotIn(task, admin_tokens._background_tasks)


class AdminStaticPr13IntegrationTests(unittest.TestCase):
    def test_account_page_contains_all_manageable_actions_and_safe_query_join(self):
        html = Path("app/statics/admin/account.html").read_text(encoding="utf-8")

        self.assertIn("function batchNSFWAll()", html)
        self.assertIn("function batchRefreshAllManageable()", html)
        self.assertIn("function deleteAllInvalid()", html)
        self.assertIn("endpoint.includes('?') ? '&' : '?'", html)
        self.assertIn("'/batch/nsfw?all_manageable=true'", html)
        self.assertIn("'/batch/refresh?all_manageable=true'", html)

    def test_disabled_nsfw_buttons_use_row_specific_unavailable_reason(self):
        html = Path("app/statics/admin/account.html").read_text(encoding="utf-8")
        nsfw_button_templates = re.findall(
            r"<button type=\"button\" class=\"row-nsfw-btn(?: is-on)?\" .*?</button>",
            html,
            flags=re.S,
        )

        self.assertEqual(len(nsfw_button_templates), 2)
        self.assertTrue(all("canManageNsfw ?" in template for template in nsfw_button_templates))
        self.assertTrue(
            all("account.rowActionNotSupported" in template for template in nsfw_button_templates)
        )

    def test_config_page_preserves_schema_defaults(self):
        html = Path("app/statics/admin/config.html").read_text(encoding="utf-8")

        self.assertIn("auto_nsfw_on_import", html)
        self.assertIn("function _getCurrentValue(section, key, field)", html)
        self.assertIn("_getValue(section, key, field)", html)
        self.assertIn("_getCurrentValue(section, field.key, field)", html)

    def test_row_action_not_supported_is_translated_for_all_locales(self):
        for path in Path("app/statics/i18n").glob("*.json"):
            data = orjson.loads(path.read_bytes())
            with self.subTest(locale=path.name):
                self.assertIn("account", data)
                self.assertIn("rowActionNotSupported", data["account"])


if __name__ == "__main__":
    unittest.main()
