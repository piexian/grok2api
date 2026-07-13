#!/usr/bin/env python3
"""将 v2 PostgreSQL 部署迁移到独立的 v3 数据库。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from urllib import error, parse, request


DEFAULT_CONSOLE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


def run(arguments: list[str]) -> str:
    completed = subprocess.run(arguments, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "命令执行失败"
        raise RuntimeError(f"{arguments[0]}: {message}")
    return completed.stdout


def inspect_environment(container: str) -> dict[str, str]:
    values = json.loads(run(["docker", "inspect", container]))
    if not values:
        raise RuntimeError(f"容器不存在: {container}")
    result: dict[str, str] = {}
    for item in values[0].get("Config", {}).get("Env", []):
        key, separator, value = item.partition("=")
        if separator:
            result[key] = value
    return result


def postgres_user(container: str) -> str:
    return inspect_environment(container).get("POSTGRES_USER", "postgres")


def psql(container: str, user: str, database: str, sql: str) -> str:
    return run(
        [
            "docker",
            "exec",
            container,
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            user,
            "-d",
            database,
            "-At",
            "-F",
            "\t",
            "-c",
            sql,
        ]
    )


def decode_base64_rows(value: str, columns: int) -> list[list[str]]:
    result: list[list[str]] = []
    for line in value.splitlines():
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != columns:
            raise RuntimeError("PostgreSQL 导出列数不匹配")
        decoded = [base64.b64decode(field).decode("utf-8") for field in fields]
        result.append(decoded)
    return result


def load_config_store(container: str, user: str, database: str) -> dict[str, object]:
    sql = """
SELECT
  replace(encode(convert_to(key, 'UTF8'), 'base64'), E'\\n', ''),
  replace(encode(convert_to(value, 'UTF8'), 'base64'), E'\\n', '')
FROM config_store
ORDER BY key
"""
    result: dict[str, object] = {}
    for key, raw_value in decode_base64_rows(psql(container, user, database, sql), 2):
        result[key] = json.loads(raw_value)
    return result


def load_accounts(container: str, user: str, database: str) -> list[dict[str, str]]:
    sql = """
SELECT
  replace(encode(convert_to(token, 'UTF8'), 'base64'), E'\\n', ''),
  replace(encode(convert_to(pool, 'UTF8'), 'base64'), E'\\n', ''),
  replace(encode(convert_to(status, 'UTF8'), 'base64'), E'\\n', '')
FROM accounts
WHERE deleted_at IS NULL
ORDER BY created_at, token
"""
    return [
        {"token": token, "pool": pool, "status": status}
        for token, pool, status in decode_base64_rows(psql(container, user, database, sql), 3)
    ]


def load_quota_accounts(container: str, user: str, database: str) -> list[dict[str, object]]:
    sql = """
SELECT
  replace(encode(convert_to(token, 'UTF8'), 'base64'), E'\\n', ''),
  replace(encode(convert_to(quota_auto, 'UTF8'), 'base64'), E'\\n', ''),
  replace(encode(convert_to(quota_fast, 'UTF8'), 'base64'), E'\\n', ''),
  replace(encode(convert_to(quota_expert, 'UTF8'), 'base64'), E'\\n', ''),
  replace(encode(convert_to(quota_heavy, 'UTF8'), 'base64'), E'\\n', ''),
  replace(encode(convert_to(quota_console, 'UTF8'), 'base64'), E'\\n', '')
FROM accounts
WHERE deleted_at IS NULL
ORDER BY created_at, token
"""
    result: list[dict[str, object]] = []
    for token, auto, fast, expert, heavy, console in decode_base64_rows(
        psql(container, user, database, sql), 6
    ):
        result.append(
            {
                "token": token,
                "auto": json.loads(auto),
                "fast": json.loads(fast),
                "expert": json.loads(expert),
                "heavy": json.loads(heavy),
                "console": json.loads(console),
            }
        )
    return result


def require_database_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value):
        raise ValueError("数据库名称无效")
    return value


def create_database(container: str, user: str, source_database: str, target_database: str) -> None:
    target_database = require_database_name(target_database)
    exists = psql(
        container,
        user,
        source_database,
        f"SELECT 1 FROM pg_database WHERE datname = '{target_database}'",
    ).strip()
    if exists:
        raise RuntimeError(f"目标数据库已存在: {target_database}")
    run(["docker", "exec", container, "createdb", "-U", user, "-O", user, target_database])


def rewrite_database_url(raw: str, database: str) -> str:
    if not raw:
        raise RuntimeError("旧容器没有 ACCOUNT_POSTGRESQL_URL")
    value = parse.urlsplit(raw)
    scheme = value.scheme.split("+", 1)[0]
    if scheme not in {"postgres", "postgresql"} or not value.hostname:
        raise RuntimeError("ACCOUNT_POSTGRESQL_URL 格式无效")
    return parse.urlunsplit((scheme, value.netloc, "/" + database, value.query, ""))


def redis_config(raw: str) -> dict[str, object]:
    if not raw:
        return {"driver": "memory"}
    value = parse.urlsplit(raw)
    if value.scheme not in {"redis", "rediss"} or not value.hostname:
        raise RuntimeError("ACCOUNT_REDIS_URL 格式无效")
    host = value.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    database = int(value.path.strip("/") or "0")
    return {
        "driver": "redis",
        "redis": {
            "address": f"{host}:{value.port or 6379}",
            "username": parse.unquote(value.username or ""),
            "password": parse.unquote(value.password or ""),
            "database": database,
            "keyPrefix": "grok2api:v3:",
            "tls": value.scheme == "rediss",
        },
    }


def write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def proxy_urls(config: dict[str, object], prefix: str) -> list[str]:
    mode = str(config.get("proxy.egress.mode", "direct"))
    single = str(config.get(prefix + "proxy_url", "") or "").strip()
    pool = config.get(prefix + "proxy_pool", [])
    if mode == "pool" and isinstance(pool, list):
        return [str(item).strip() for item in pool if str(item).strip()]
    return [single] if single else []


def prepare(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"输出目录不是空目录: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_dir, 0o700)

    postgres_container = args.postgres_container
    user = postgres_user(postgres_container)
    app_environment = inspect_environment(args.old_app_container)
    config = load_config_store(postgres_container, user, args.old_database)
    accounts = load_accounts(postgres_container, user, args.old_database)
    if not accounts:
        raise RuntimeError("旧数据库没有可迁移账号")

    admin_password = str(config.get("app.app_key", "") or "")
    api_key = str(config.get("app.api_key", "") or "")
    if len(admin_password) < 8:
        raise RuntimeError("旧管理员密码少于 8 个字符，无法保持登录兼容")
    if api_key and len(api_key.strip()) < 16:
        raise RuntimeError("旧 API Key 少于 16 个字符，无法安全导入")

    public_url = str(config.get("app.app_url", "") or "").strip().rstrip("/")
    if not public_url:
        public_url = "http://127.0.0.1:8000"
    public_parts = parse.urlsplit(public_url)
    if public_parts.scheme not in {"http", "https"} or not public_parts.netloc:
        raise RuntimeError("旧 app.app_url 不是有效的 HTTP(S) URL")

    target_database = require_database_name(args.new_database)
    create_database(postgres_container, user, args.old_database, target_database)
    postgres_dsn = rewrite_database_url(app_environment.get("ACCOUNT_POSTGRESQL_URL", ""), target_database)
    console_user_agent = str(config.get("proxy.clearance.user_agent", "") or "").strip() or DEFAULT_CONSOLE_USER_AGENT

    new_config: dict[str, object] = {
        "server": {"listen": "0.0.0.0:8000"},
        "secrets": {
            "jwtSecret": secrets.token_hex(32),
            "credentialEncryptionKey": base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
        },
        "bootstrapAdmin": {"username": "admin", "password": admin_password},
        "frontend": {"publicApiBaseURL": public_url, "staticPath": "/app/frontend/dist"},
        "database": {
            "driver": "postgres",
            "postgres": {"dsn": postgres_dsn, "maxOpenConns": 40, "maxIdleConns": 10},
        },
        "runtimeStore": redis_config(app_environment.get("ACCOUNT_REDIS_URL", "")),
        "auth": {"secureCookies": public_parts.scheme == "https"},
        "provider": {
            "console": {
                "baseURL": "https://console.x.ai",
                "userAgent": console_user_agent,
                "chatTimeout": "5m",
            }
        },
        "media": {"driver": "local", "local": {"path": "/app/data/media"}},
    }
    if api_key.strip():
        new_config["bootstrapClientKey"] = {
            "name": "v2 migration compatibility key",
            "secret": api_key.strip(),
            "rpmLimit": 100000,
            "maxConcurrent": 1024,
        }

    web_accounts: list[dict[str, str]] = []
    console_accounts: list[dict[str, str]] = []
    disabled_hashes: list[str] = []
    tier_map = {"basic": "basic", "super": "super", "heavy": "heavy", "lite": "auto"}
    for account in accounts:
        token = account["token"].strip()
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        web_accounts.append(
            {
                "name": "Grok Web " + digest[:8],
                "sso_token": token,
                "tier": tier_map.get(account["pool"].lower(), "auto"),
            }
        )
        console_accounts.append({"name": "Grok Console " + digest[:8], "sso_token": token})
        if account["status"].lower() == "disabled":
            disabled_hashes.append(digest[:8])

    main_proxy_urls = proxy_urls(config, "proxy.egress.")
    resource_proxy_urls = proxy_urls(config, "proxy.egress.resource_")
    state = {
        "disabledHashes": disabled_hashes,
        "mainProxyURLs": main_proxy_urls,
        "resourceProxyURLs": resource_proxy_urls,
        "cloudflareCookies": str(config.get("proxy.clearance.cf_cookies", "") or "").strip(),
        "userAgent": console_user_agent,
    }

    data_dir = output_dir / "data"
    data_dir.mkdir(mode=0o700)
    os.chown(data_dir, 10001, 10001)
    write_private(output_dir / "config.yaml", json_bytes(new_config))
    write_private(
        output_dir / "web-accounts.json",
        json_bytes({"provider": "grok_web", "accounts": web_accounts}),
    )
    write_private(
        output_dir / "console-accounts.json",
        json_bytes({"provider": "grok_console", "accounts": console_accounts}),
    )
    write_private(output_dir / "migration-state.json", json_bytes(state))

    image = args.image.strip()
    if not image or any(character.isspace() for character in image):
        raise RuntimeError("镜像名称无效")
    compose = f"""name: grok2api

services:
  grok2api:
    container_name: grok2api
    image: {json.dumps(image)}
    ports:
      - \"8000:8000\"
    environment:
      TZ: \"Asia/Shanghai\"
    volumes:
      - {json.dumps(str(output_dir / 'config.yaml') + ':/run/grok2api/config.yaml:ro')}
      - {json.dumps(str(data_dir) + ':/app/data')}
    networks:
      - 1panel-network
    restart: always
    init: true
    stop_grace_period: 30s
    security_opt:
      - no-new-privileges:true

networks:
  1panel-network:
    external: true
"""
    write_private(output_dir / "docker-compose.final.yml", compose.encode("utf-8"))
    print(
        f"prepared accounts={len(accounts)} disabled={len(disabled_hashes)} "
        f"database={target_database} output={output_dir}"
    )


class AdminAPI:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.access_token = ""

    def login(self) -> None:
        value = self.json_request(
            "POST",
            "/api/admin/v1/auth/login",
            {"username": self.username, "password": self.password},
            authenticated=False,
        )
        self.access_token = str(value["tokens"]["accessToken"])

    def json_request(
        self,
        method: str,
        path: str,
        body: object | None = None,
        *,
        authenticated: bool = True,
    ) -> object:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if authenticated:
            headers["Authorization"] = "Bearer " + self.access_token
        response = open_request(request.Request(self.base_url + path, data=data, headers=headers, method=method), 120)
        payload = json.loads(response.decode("utf-8"))
        if "error" in payload:
            raise RuntimeError(str(payload["error"].get("message", "管理 API 返回错误")))
        return payload.get("data", payload)

    def upload(self, path: str, file_path: Path) -> dict[str, object]:
        boundary = "----grok2api-migration-" + secrets.token_hex(12)
        payload = io.BytesIO()
        payload.write(f"--{boundary}\r\n".encode())
        payload.write(
            (
                'Content-Disposition: form-data; name="file"; filename="accounts.json"\r\n'
                "Content-Type: application/json\r\n\r\n"
            ).encode()
        )
        payload.write(file_path.read_bytes())
        payload.write(f"\r\n--{boundary}--\r\n".encode())
        headers = {
            "Authorization": "Bearer " + self.access_token,
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Accept": "text/event-stream",
        }
        http_request = request.Request(
            self.base_url + path,
            data=payload.getvalue(),
            headers=headers,
            method="POST",
        )
        try:
            with request.urlopen(http_request, timeout=7200) as response:
                event_name = ""
                data_lines: list[str] = []
                complete: dict[str, object] | None = None
                for raw_line in response:
                    line = raw_line.decode("utf-8").rstrip("\r\n")
                    if not line:
                        if event_name == "complete" and data_lines:
                            complete = json.loads("\n".join(data_lines))
                        elif event_name == "error" and data_lines:
                            value = json.loads("\n".join(data_lines))
                            raise RuntimeError(str(value.get("message", "账号导入失败")))
                        event_name = ""
                        data_lines = []
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                if complete is None:
                    raise RuntimeError("账号导入没有返回 complete 事件")
                return complete
        except error.HTTPError as exc:
            raise_http_error(exc)


def raise_http_error(exc: error.HTTPError) -> None:
    message = ""
    try:
        value = json.loads(exc.read().decode("utf-8"))
        error_value = value.get("error", "") if isinstance(value, dict) else value
        if isinstance(error_value, dict):
            message = str(error_value.get("message", ""))
        else:
            message = str(error_value)
    except (ValueError, UnicodeDecodeError):
        pass
    raise RuntimeError(f"HTTP {exc.code}: {message or exc.reason}")


def open_request(http_request: request.Request, timeout: int) -> bytes:
    try:
        with request.urlopen(http_request, timeout=timeout) as response:
            return response.read()
    except error.HTTPError as exc:
        raise_http_error(exc)
    raise AssertionError("unreachable")


def create_egress_nodes(api: AdminAPI, state: dict[str, object]) -> int:
    existing = api.json_request("GET", "/api/admin/v1/egress-nodes")
    names = {str(item.get("name", "")) for item in existing.get("items", [])}  # type: ignore[union-attr]
    user_agent = str(state.get("userAgent", ""))
    cookies = str(state.get("cloudflareCookies", ""))
    created = 0

    def create_nodes(scope: str, prefix: str, urls: list[object]) -> None:
        nonlocal created
        values = [str(item).strip() for item in urls if str(item).strip()]
        if not values and not cookies:
            return
        if not values:
            values = [""]
        for index, proxy_url in enumerate(values, start=1):
            name = f"{prefix} {index}"
            if name in names:
                continue
            body: dict[str, object] = {
                "name": name,
                "scope": scope,
                "enabled": True,
                "userAgent": user_agent,
            }
            if proxy_url:
                body["proxyURL"] = proxy_url
            if cookies:
                body["cloudflareCookies"] = cookies
            api.json_request("POST", "/api/admin/v1/egress-nodes", body)
            names.add(name)
            created += 1

    create_nodes("all", "v2 migrated egress", list(state.get("mainProxyURLs", [])))
    main = {str(item).strip() for item in state.get("mainProxyURLs", []) if str(item).strip()}
    resource = [item for item in state.get("resourceProxyURLs", []) if str(item).strip() not in main]
    create_nodes("grok_web_asset", "v2 migrated asset egress", resource)
    return created


def disable_migrated_accounts(api: AdminAPI, hashes: list[object]) -> int:
    updated = 0
    for provider in ("grok_web", "grok_console"):
        for digest in hashes:
            query = parse.urlencode(
                {"page": "1", "pageSize": "20", "provider": provider, "search": str(digest)}
            )
            value = api.json_request("GET", "/api/admin/v1/accounts?" + query)
            for item in value.get("items", []):  # type: ignore[union-attr]
                if item.get("provider") == provider and str(digest) in str(item.get("name", "")):
                    api.json_request(
                        "PATCH", f"/api/admin/v1/accounts/{item['id']}", {"enabled": False}
                    )
                    updated += 1
    return updated


def apply_migration(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir).resolve()
    config = json.loads((output_dir / "config.yaml").read_text("utf-8"))
    state = json.loads((output_dir / "migration-state.json").read_text("utf-8"))
    admin = config["bootstrapAdmin"]
    api = AdminAPI(args.base_url, str(admin["username"]), str(admin["password"]))
    api.login()
    egress_created = create_egress_nodes(api, state)
    started_at = time.monotonic()
    web = api.upload("/api/admin/v1/accounts/web/import", output_dir / "web-accounts.json")
    print(
        f"web imported created={web.get('created', 0)} updated={web.get('updated', 0)} "
        f"synced={web.get('synced', 0)} syncFailed={web.get('syncFailed', 0)}"
    )
    if time.monotonic() - started_at > 10 * 60:
        api.login()
    console = api.upload(
        "/api/admin/v1/accounts/console/import", output_dir / "console-accounts.json"
    )
    print(
        f"console imported created={console.get('created', 0)} updated={console.get('updated', 0)} "
        f"synced={console.get('synced', 0)} syncFailed={console.get('syncFailed', 0)}"
    )
    disabled = disable_migrated_accounts(api, list(state.get("disabledHashes", [])))
    summary = api.json_request("GET", "/api/admin/v1/accounts/summary")
    providers = summary.get("providers", {})  # type: ignore[union-attr]
    web_total = providers.get("grok_web", {}).get("total", 0)
    console_total = providers.get("grok_console", {}).get("total", 0)
    print(
        f"migration applied web={web_total} console={console_total} "
        f"disabled={disabled} egressCreated={egress_created}"
    )


def sql_timestamp(value: object) -> str:
    if value in (None, ""):
        return "NULL"
    milliseconds = int(value)
    if milliseconds <= 0:
        return "NULL"
    return f"to_timestamp({milliseconds / 1000:.3f})"


def quota_source(value: object) -> str:
    try:
        source = int(value)
    except (TypeError, ValueError):
        source = 0
    if source == 1:
        return "upstream"
    if source == 2:
        return "estimated"
    return "default"


def quota_row(account_id: int, mode: str, value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    try:
        total = max(0, int(value.get("total", 0)))
        remaining = max(0, int(value.get("remaining", 0)))
        window_seconds = max(0, int(value.get("window_seconds", 0)))
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    remaining = min(remaining, total)
    usage_percent = min(100.0, max(0.0, (total - remaining) * 100.0 / total))
    return (
        f"({account_id},'{mode}',{remaining},{total},{usage_percent:.6f},'[]',"
        f"{window_seconds},{sql_timestamp(value.get('reset_at'))},"
        f"{sql_timestamp(value.get('synced_at'))},'{quota_source(value.get('source'))}',"
        "CURRENT_TIMESTAMP)"
    )


def write_quota_rows(
    container: str,
    user: str,
    database: str,
    rows: list[str],
    conflict_sql: str,
) -> None:
    for start in range(0, len(rows), 500):
        chunk = rows[start : start + 500]
        sql = """
INSERT INTO account_quota_windows
  (account_id, mode, remaining, total, usage_percent, breakdown_json,
   window_seconds, reset_at, synced_at, source, updated_at)
VALUES
""" + ",\n".join(chunk) + "\n" + conflict_sql
        psql(container, user, database, sql)


def repair_quotas(args: argparse.Namespace) -> None:
    container = args.postgres_container
    user = postgres_user(container)
    accounts = load_quota_accounts(container, user, args.old_database)
    mapping_output = psql(
        container,
        user,
        args.new_database,
        "SELECT id, provider, source_key FROM provider_accounts ORDER BY id",
    )
    account_ids: dict[tuple[str, str], int] = {}
    for line in mapping_output.splitlines():
        if not line:
            continue
        account_id, provider, source_key = line.split("\t", 2)
        account_ids[(provider, source_key)] = int(account_id)
    existing_output = psql(
        container,
        user,
        args.new_database,
        "SELECT DISTINCT account_id FROM account_quota_windows ORDER BY account_id",
    )
    existing = {int(value) for value in existing_output.splitlines() if value}

    web_rows: list[str] = []
    console_rows: list[str] = []
    repaired_web_accounts = 0
    for account in accounts:
        token = str(account["token"]).strip()
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        web_id = account_ids.get(("grok_web", "sso:" + digest))
        if web_id is not None and web_id not in existing:
            before = len(web_rows)
            for mode in ("auto", "fast", "expert", "heavy"):
                row = quota_row(web_id, mode, account.get(mode))
                if row is not None:
                    web_rows.append(row)
            if len(web_rows) > before:
                repaired_web_accounts += 1
        console_id = account_ids.get(("grok_console", "console-sso:" + digest))
        if console_id is not None:
            row = quota_row(console_id, "console", account.get("console"))
            if row is not None:
                console_rows.append(row)

    write_quota_rows(
        container,
        user,
        args.new_database,
        web_rows,
        "ON CONFLICT (account_id, mode) DO NOTHING",
    )
    write_quota_rows(
        container,
        user,
        args.new_database,
        console_rows,
        """ON CONFLICT (account_id, mode) DO UPDATE SET
remaining = EXCLUDED.remaining,
total = EXCLUDED.total,
usage_percent = EXCLUDED.usage_percent,
breakdown_json = EXCLUDED.breakdown_json,
window_seconds = EXCLUDED.window_seconds,
reset_at = EXCLUDED.reset_at,
synced_at = EXCLUDED.synced_at,
source = EXCLUDED.source,
updated_at = EXCLUDED.updated_at""",
    )
    print(
        f"quota repair webAccounts={repaired_web_accounts} webWindows={len(web_rows)} "
        f"consoleWindows={len(console_rows)}"
    )


def smoke(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir).resolve()
    config = json.loads((output_dir / "config.yaml").read_text("utf-8"))
    client_key = str(config.get("bootstrapClientKey", {}).get("secret", ""))
    if not client_key:
        raise RuntimeError("配置中没有可用于迁移验证的 bootstrapClientKey")
    for model in args.model:
        body = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with only OK"}],
                "max_tokens": 8,
                "stream": False,
            }
        ).encode("utf-8")
        http_request = request.Request(
            args.base_url.rstrip("/") + "/v1/chat/completions",
            data=body,
            headers={
                "Authorization": "Bearer " + client_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        payload = json.loads(open_request(http_request, 180).decode("utf-8"))
        choices = payload.get("choices", [])
        content = ""
        if choices:
            content = str(choices[0].get("message", {}).get("content", ""))
        print(f"model={model} choices={len(choices)} contentLength={len(content)}")


def probe_console_models(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir).resolve()
    config = json.loads((output_dir / "config.yaml").read_text("utf-8"))
    accounts = json.loads((output_dir / "console-accounts.json").read_text("utf-8"))["accounts"]
    user_agent = str(config["provider"]["console"]["userAgent"])
    for index, account in enumerate(accounts[: args.limit], start=1):
        token = str(account["sso_token"])
        http_request = request.Request(
            args.base_url.rstrip("/") + "/v1/models",
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer anonymous",
                "Cookie": f"sso={token}; sso-rw={token}",
                "Origin": "https://console.x.ai",
                "Referer": "https://console.x.ai/",
                "User-Agent": user_agent,
                "x-cluster": "https://us-east-1.api.x.ai",
            },
            method="GET",
        )
        try:
            payload = json.loads(open_request(http_request, 60).decode("utf-8"))
            items = payload.get("data", []) if isinstance(payload, dict) else []
            model_ids = {
                str(item.get("id", "")) for item in items if isinstance(item, dict)
            }
            print(
                f"account={index} status=200 models={len(model_ids)} "
                f"grok45={'yes' if 'grok-4.5' in model_ids else 'no'}"
            )
            if "grok-4.5" in model_ids:
                return
        except RuntimeError as exc:
            match = re.search(r"HTTP (\d+)", str(exc))
            print(f"account={index} status={match.group(1) if match else 'error'}")


def probe_console_grpc_models(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir).resolve()
    config = json.loads((output_dir / "config.yaml").read_text("utf-8"))
    accounts = json.loads((output_dir / "console-accounts.json").read_text("utf-8"))["accounts"]
    user_agent = str(config["provider"]["console"]["userAgent"])
    for index, account in enumerate(accounts[: args.limit], start=1):
        token = str(account["sso_token"])
        try:
            team_response, team_headers = console_grpc_call(
                token,
                user_agent,
                args.base_url,
                "/auth_mgmt.AuthManagement/GetTeam",
                b"",
            )
            teams = sorted(
                set(
                    match.decode("ascii")
                    for match in re.findall(
                        rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                        team_response,
                    )
                )
            )
            if not teams:
                print(
                    f"account={index} getTeamBytes={len(team_response)} "
                    f"grpcStatus={team_headers.get('grpc-status', 'none')} teams=0"
                )
                continue
            found = False
            model_bytes = 0
            list_status = "none"
            for team in teams:
                message = b"\x0a" + protobuf_varint(len(team)) + team.encode("ascii")
                models_response, model_headers = console_grpc_call(
                    token,
                    user_agent,
                    args.base_url,
                    "/auth_mgmt.AuthManagement/ListModelsForTeam",
                    message,
                )
                model_bytes += len(models_response)
                list_status = model_headers.get("grpc-status", "none")
                if b"grok-4.5" in models_response:
                    found = True
                    break
            print(
                f"account={index} teams={len(teams)} modelBytes={model_bytes} "
                f"grpcStatus={list_status} grok45={'yes' if found else 'no'}"
            )
            if found:
                return
        except RuntimeError as exc:
            match = re.search(r"HTTP (\d+)", str(exc))
            print(f"account={index} status={match.group(1) if match else 'error'}")


def protobuf_varint(value: int) -> bytes:
    output = bytearray()
    while value >= 0x80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def console_grpc_call(
    token: str,
    user_agent: str,
    base_url: str,
    path: str,
    message: bytes,
) -> tuple[bytes, object]:
    frame = b"\x00" + len(message).to_bytes(4, "big") + message
    http_request = request.Request(
        base_url.rstrip("/") + path,
        data=frame,
        headers={
            "Accept": "*/*",
            "Authorization": "Bearer anonymous",
            "Content-Type": "application/grpc-web+proto",
            "Cookie": f"sso={token}; sso-rw={token}",
            "Origin": "https://console.x.ai",
            "Referer": "https://console.x.ai/",
            "User-Agent": user_agent,
            "x-cluster": "https://us-east-1.api.x.ai",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
        },
        method="POST",
    )
    try:
        with request.urlopen(http_request, timeout=60) as response:
            return response.read(), response.headers
    except error.HTTPError as exc:
        raise_http_error(exc)
    raise AssertionError("unreachable")


def finalize(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir).resolve()
    config_path = output_dir / "config.yaml"
    config = json.loads(config_path.read_text("utf-8"))
    config.pop("bootstrapAdmin", None)
    config.pop("bootstrapClientKey", None)
    temporary = output_dir / ".config.yaml.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(json_bytes(config))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, config_path)
    for name in ("web-accounts.json", "console-accounts.json", "migration-state.json"):
        (output_dir / name).unlink(missing_ok=True)
    print("migration plaintext files removed")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    subcommands = value.add_subparsers(dest="command", required=True)

    prepare_parser = subcommands.add_parser("prepare")
    prepare_parser.add_argument("--output-dir", required=True)
    prepare_parser.add_argument("--old-app-container", default="grok2api")
    prepare_parser.add_argument("--postgres-container", default="postgresql")
    prepare_parser.add_argument("--old-database", default="grok2api")
    prepare_parser.add_argument("--new-database", default="grok2api_v3")
    prepare_parser.add_argument("--image", required=True)
    prepare_parser.set_defaults(handler=prepare)

    apply_parser = subcommands.add_parser("apply")
    apply_parser.add_argument("--output-dir", required=True)
    apply_parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    apply_parser.set_defaults(handler=apply_migration)

    repair_parser = subcommands.add_parser("repair-quotas")
    repair_parser.add_argument("--postgres-container", default="postgresql")
    repair_parser.add_argument("--old-database", default="grok2api")
    repair_parser.add_argument("--new-database", default="grok2api_v3")
    repair_parser.set_defaults(handler=repair_quotas)

    smoke_parser = subcommands.add_parser("smoke")
    smoke_parser.add_argument("--output-dir", required=True)
    smoke_parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    smoke_parser.add_argument("--model", action="append", required=True)
    smoke_parser.set_defaults(handler=smoke)

    probe_parser = subcommands.add_parser("probe-console-models")
    probe_parser.add_argument("--output-dir", required=True)
    probe_parser.add_argument("--base-url", default="https://console.x.ai")
    probe_parser.add_argument("--limit", type=int, default=20)
    probe_parser.set_defaults(handler=probe_console_models)

    grpc_probe_parser = subcommands.add_parser("probe-console-grpc-models")
    grpc_probe_parser.add_argument("--output-dir", required=True)
    grpc_probe_parser.add_argument("--base-url", default="https://console.x.ai")
    grpc_probe_parser.add_argument("--limit", type=int, default=5)
    grpc_probe_parser.set_defaults(handler=probe_console_grpc_models)

    finalize_parser = subcommands.add_parser("finalize")
    finalize_parser.add_argument("--output-dir", required=True)
    finalize_parser.set_defaults(handler=finalize)
    return value


def main() -> int:
    try:
        arguments = parser().parse_args()
        arguments.handler(arguments)
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
