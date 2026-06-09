import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from app.control.account.backends.local import LocalAccountRepository
from app.control.account.commands import AccountPatch, AccountUpsert, ListAccountsQuery
from app.control.account.models import AccountMutationResult, AccountPage
from app.main import app
from app.products.web.admin.tokens import delete_tokens, list_tokens


async def _asgi_request(
    method: str,
    path: str,
    body: object | None = None,
) -> tuple[int, bytes]:
    messages = []
    url = urlsplit(path)
    body_bytes = b"" if body is None else json.dumps(body).encode()
    headers = []
    if body is not None:
        headers.append((b"content-type", b"application/json"))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": url.path,
        "raw_path": url.path.encode(),
        "query_string": url.query.encode(),
        "headers": headers,
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
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    return status, body


async def _asgi_get(path: str) -> tuple[int, bytes]:
    return await _asgi_request("GET", path)


class AdminAccountPaginationExportTest(unittest.IsolatedAsyncioTestCase):
    async def test_admin_delete_tokens_reports_repository_result(self):
        class FakeRepo:
            def __init__(self):
                self.tokens = []

            async def delete_accounts(self, tokens):
                self.tokens = tokens
                return AccountMutationResult(deleted=1, revision=42)

        repo = FakeRepo()
        response = await delete_tokens(
            ["sso=tok_delete", "sso-rw=tok_delete", "tok_missing"],
            repo=repo,
        )
        payload = json.loads(response.body)

        self.assertEqual(repo.tokens, ["tok_delete", "tok_missing"])
        self.assertEqual(payload["deleted"], 1)
        self.assertEqual(payload["requested"], 2)
        self.assertEqual(payload["missing"], 1)
        self.assertEqual(payload["revision"], 42)

    async def test_admin_tokens_accepts_2000_page_size(self):
        previous_repo = getattr(app.state, "repository", None)
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            await repo.upsert_accounts(
                [
                    AccountUpsert(token=f"tok_{idx}", pool="basic")
                    for idx in range(3)
                ]
            )
            app.state.repository = repo
            try:
                status, body = await _asgi_get(
                    "/admin/api/tokens?app_key=grok2api&page_size=2000"
                )
                self.assertEqual(status, 200)
                payload = json.loads(body)
                self.assertEqual(payload["page_size"], 2000)
                self.assertEqual(len(payload["tokens"]), 3)
            finally:
                app.state.repository = previous_repo
                await repo.close()

    async def test_admin_tokens_accepts_multi_pool_filter(self):
        class FakeRepo:
            def __init__(self):
                self.query = None

            async def list_accounts(self, query):
                self.query = query
                return AccountPage(page=query.page, page_size=query.page_size)

        repo = FakeRepo()
        response = await list_tokens(
            page=2,
            page_size=10,
            pool="heavy",
            pools="basic, super, basic",
            status=None,
            tags=None,
            exclude_tags=None,
            sort_by="updated_at",
            sort_desc=True,
            repo=repo,
        )
        payload = json.loads(response.body)

        self.assertEqual(payload["page"], 2)
        self.assertEqual(repo.query.pool, "heavy")
        self.assertEqual(repo.query.pools, ["basic", "super"])

    async def test_local_account_repository_filters_multiple_pools(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            try:
                await repo.upsert_accounts(
                    [
                        AccountUpsert(token="tok_basic", pool="basic"),
                        AccountUpsert(token="tok_super", pool="super"),
                        AccountUpsert(token="tok_heavy", pool="heavy"),
                    ]
                )
                page = await repo.list_accounts(
                    ListAccountsQuery(
                        page=1,
                        page_size=10,
                        pools=["basic", "super"],
                        sort_by="token",
                        sort_desc=False,
                    )
                )
                self.assertEqual(
                    [item.token for item in page.items],
                    ["tok_basic", "tok_super"],
                )
            finally:
                await repo.close()

    def test_account_page_export_uses_current_scope(self):
        html = Path("app/statics/admin/account.html").read_text()

        self.assertIn('<option value="1000">1000 / 页</option>', html)
        self.assertIn('<option value="2000">2000 / 页</option>', html)
        self.assertIn("const PAGE_SIZE_OPTIONS = [50, 100, 200, 500, 1000, 2000];", html)
        self.assertIn("const EXPORT_PAGE_SIZE = 2000;", html)
        self.assertIn("const IMPORT_BATCH_SIZE = 500;", html)
        self.assertIn("function buildTokenQueryForPage(page, size)", html)
        self.assertIn("async function fetchAllFiltered()", html)
        self.assertIn("buildTokenQueryForPage(page, EXPORT_PAGE_SIZE)", html)
        self.assertIn("if (sel.size) return [...sel]", html)
        self.assertIn(".map(token => selectedRecords.get(token))", html)

    def test_account_page_has_responsive_multi_pool_filters(self):
        html = Path("app/statics/admin/account.html").read_text()

        self.assertIn("max-height:min(70vh, 520px);", html)
        self.assertIn("overflow-y:auto;", html)
        self.assertIn("overscroll-behavior:contain;", html)
        self.assertIn("flex-wrap:wrap;", html)
        self.assertIn("const curPools = new Set();", html)
        self.assertIn("p.set('pools', [...curPools].join(','));", html)
        self.assertIn("curPools.add(pool);", html)
        self.assertIn("curPools.delete(pool);", html)
        self.assertIn('aria-pressed="${active ? \'true\' : \'false\'}"', html)

    def test_account_page_import_and_delete_show_progress(self):
        html = Path("app/statics/admin/account.html").read_text()

        self.assertIn("const IMPORT_BATCH_SIZE = 500;", html)
        self.assertIn("const DELETE_BATCH_SIZE = 500;", html)
        self.assertIn("const progress = showProgressToast(pendingMessage);", html)
        self.assertIn("unique.slice(offset, offset + IMPORT_BATCH_SIZE)", html)
        self.assertIn("progress.update(processed, total);", html)
        self.assertIn("const progress = showProgressToast(tr('account.deleting'", html)
        self.assertIn("uniqueTokens.slice(offset, offset + DELETE_BATCH_SIZE)", html)
        self.assertIn("progress.update(processed, n);", html)
        self.assertIn("account.importPartialFailed", html)

    async def test_admin_delete_tokens_accepts_raw_jwt_lines_and_sso_prefixes(self):
        raw_jwt_a = (
            "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9."
            "eyJzZXNzaW9uX2lkIjoiZGVsZXRlLWEifQ."
            "delete-token-a"
        )
        raw_jwt_b = (
            "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9."
            "eyJzZXNzaW9uX2lkIjoiZGVsZXRlLWIifQ."
            "delete-token-b"
        )
        raw_jwt_c = (
            "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9."
            "eyJzZXNzaW9uX2lkIjoiZGVsZXRlLWMifQ."
            "delete-token-c"
        )
        previous_repo = getattr(app.state, "repository", None)
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            await repo.upsert_accounts(
                [
                    AccountUpsert(token="tok_keep", pool="basic"),
                    AccountUpsert(token=raw_jwt_a, pool="basic"),
                    AccountUpsert(token=raw_jwt_b, pool="basic"),
                    AccountUpsert(token=raw_jwt_c, pool="basic"),
                ]
            )
            app.state.repository = repo
            try:
                status, body = await _asgi_request(
                    "DELETE",
                    "/admin/api/tokens?app_key=grok2api",
                    [raw_jwt_a, f"sso={raw_jwt_b}", f"sso-rw={raw_jwt_c}", raw_jwt_c],
                )
                self.assertEqual(status, 200)
                payload = json.loads(body)
                self.assertEqual(payload["deleted"], 3)
                self.assertEqual(payload["requested"], 3)
                self.assertEqual(payload["missing"], 0)

                page = await repo.list_accounts(ListAccountsQuery(page=1, page_size=10))
                self.assertEqual(page.total, 1)
                self.assertEqual(page.items[0].token, "tok_keep")
            finally:
                app.state.repository = previous_repo
                await repo.close()

    async def test_admin_tokens_sort_by_success_rate_and_returns_fail_count(self):
        previous_repo = getattr(app.state, "repository", None)
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            await repo.upsert_accounts(
                [
                    AccountUpsert(token="tok_zero", pool="basic"),
                    AccountUpsert(token="tok_low", pool="basic"),
                    AccountUpsert(token="tok_high", pool="basic"),
                ]
            )
            await repo.patch_accounts(
                [
                    AccountPatch(token="tok_low", usage_use_delta=1, usage_fail_delta=9),
                    AccountPatch(token="tok_high", usage_use_delta=9, usage_fail_delta=1),
                ]
            )
            app.state.repository = repo
            try:
                status, body = await _asgi_get(
                    "/admin/api/tokens?app_key=grok2api"
                    "&sort_by=success_rate&sort_desc=true&page_size=3"
                )
                self.assertEqual(status, 200)
                tokens = json.loads(body)["tokens"]
                self.assertEqual(
                    [token["token"] for token in tokens],
                    ["tok_high", "tok_low", "tok_zero"],
                )
                self.assertEqual(tokens[1]["fail_count"], 9)
            finally:
                app.state.repository = previous_repo
                await repo.close()

    def test_account_page_has_delete_sso_entry(self):
        html = Path("app/statics/admin/account.html").read_text()

        self.assertIn("openDeleteSso()", html)
        self.assertIn('id="modal-delete-sso"', html)
        self.assertIn('id="delete-sso-tokens"', html)
        self.assertIn("function parseDeleteSsoInput(raw)", html)
        self.assertIn("String(raw || '').split(/\\r?\\n/)", html)
        self.assertIn("sso(?:-rw)?=([^;\\s,]+)", html)
        self.assertIn("line.split(/[\\s,;]+/).forEach(addToken)", html)
        self.assertIn("const DELETE_BATCH_SIZE = 500;", html)
        self.assertIn("uniqueTokens.slice(offset, offset + DELETE_BATCH_SIZE)", html)
        self.assertIn("account.deletePartialFailed", html)

    def test_account_page_has_page_jump_entry(self):
        html = Path("app/statics/admin/account.html").read_text()

        self.assertIn('onsubmit="jumpToPage(event)"', html)
        self.assertIn('id="page-jump-input"', html)
        self.assertIn('id="btn-page-jump"', html)
        self.assertIn('data-i18n-placeholder="account.pageJumpPlaceholder"', html)
        self.assertIn("function jumpToPage(event)", html)
        self.assertIn("const next = Math.min(target, getPageCount());", html)
        self.assertIn("account.pageJumpInvalid", html)

    def test_account_page_has_sortable_usage_headers(self):
        html = Path("app/statics/admin/account.html").read_text()

        self.assertIn("const SORT_FIELDS = new Set(", html)
        self.assertIn('data-sort-field="usage_use_count"', html)
        self.assertIn('data-sort-field="usage_fail_count"', html)
        self.assertIn('data-sort-field="success_rate"', html)
        self.assertIn('data-sort-field="last_use_at"', html)
        self.assertIn("function setSort(field)", html)
        self.assertIn("curSortDesc = !curSortDesc;", html)
        self.assertIn("function syncSortHeaders()", html)
