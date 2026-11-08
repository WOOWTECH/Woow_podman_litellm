"""WOOW — ChatGPT subscriptions for LiteLLM's `chatgpt/` provider (core + CLI).

Several ChatGPT accounts can be connected to one LiteLLM. Each one is an *account* (a short id):
  * ``default`` — the account used by chatgpt/ models that name no account (LiteLLM's own
    ``$CHATGPT_TOKEN_DIR/auth.json``; what 0.2.0–0.2.2 had as the only account);
  * one per LiteLLM credential of provider "ChatGPT Subscription": the credential's value
    ``api_key = "woow-chatgpt:<account>"`` is a *reference*, not a secret. LiteLLM copies a
    credential's values into every request of a model that uses it (``litellm_credential_name``),
    and the patched provider turns the reference into that account's access token. Models added
    from the sign-in page also carry the reference themselves, so they keep pointing at their
    account even if the credential is renamed or deleted.
Models with the same name on several accounts form one LiteLLM model group: requests are spread
over the accounts and a rate-limited account (429) is retried on another.

Where the tokens live:
  * ``$CHATGPT_TOKEN_DIR/auth.json`` (default) and ``$CHATGPT_TOKEN_DIR/accounts/<id>.json``
    — tmpfs in the WOOW chart;
  * ``woow_platform.chatgpt_accounts`` in LiteLLM's own PostgreSQL (DATABASE_HOST/… parts or
    DATABASE_URL) — so a restart or upgrade signs nobody out. The default account is mirrored
    to the 0.2.x table ``woow_platform.chatgpt_auth`` (a chart rollback keeps it). Without a
    database the files are the only copy: put CHATGPT_TOKEN_DIR on a persistent volume.

Roles:
1. ``install_import_hook`` (from the WOOW chart's sitecustomize, or from woow_chatgpt_plugin.py
   loaded by ``litellm_settings.callbacks``) patches LiteLLM's ChatGPT provider:
   * ``Authenticator``: no interactive device-code login inside a request (LiteLLM 1.103 prints
     the code to a log nobody reads and blocks up to 15 min; upstream fails fast from 1.106,
     #39585) — 401 with a hint at once; token writes atomic, 0600, copied to the database; one
     refresh at a time per account (OpenAI rotates refresh tokens);
   * ``ChatGPTConfig`` / ``ChatGPTResponsesAPIConfig``: the token and ChatGPT-Account-Id come
     from the account the request names (the credential's reference), not one global file;
     no token is fetched while LiteLLM merely builds a deployment, so models can be added before
     their account is signed in; a 429 usage_limit_reached (weekly quota used up) without a
     Retry-After gets one (woow_usage_limit, chart 0.3.14) so the router keeps that account out
     of the rotation for minutes instead of 5 s;
   * ``litellm.main.responses_api_bridge_check``: every chat completion of provider chatgpt goes
     through the Responses API bridge (the only API the ChatGPT backend serves, and the path
     that resolves the account reference), also when the model map does not say
     ``mode: responses`` (chart 0.3.16);
   and puts the /woow/chatgpt page (woow_chatgpt_plugin.py) on the proxy app.
2. ``remote_models`` / ``register_model_costs``: an account's model list straight from ChatGPT,
   and LiteLLM taught the ones its bundled model map does not know yet.
3. A CLI (``python woow_chatgpt.py <command> [--account <id>]``) used by start-litellm.sh
   (``restore``) and by operators over ``kubectl exec``. It prints exactly ONE JSON object on
   stdout and never a token.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import importlib.abc
import json
import os
import re
import sys
import tempfile
import threading
import time

# ── OAuth (= litellm/llms/chatgpt/common_utils.py @ v1.103.3 and v1.104.0, derived from openai/codex)
AUTH_BASE = "https://auth.openai.com"
DEVICE_CODE_URL = f"{AUTH_BASE}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{AUTH_BASE}/api/accounts/deviceauth/token"
OAUTH_TOKEN_URL = f"{AUTH_BASE}/oauth/token"
DEVICE_VERIFY_URL = f"{AUTH_BASE}/codex/device"
DEVICE_REDIRECT_URI = f"{AUTH_BASE}/deviceauth/callback"
# Browser login (PKCE), as the Codex CLI and pi do: OpenAI only accepts this client's registered
# localhost callback, so a remote LiteLLM cannot receive it — the user pastes the URL their
# browser was sent to (the page itself fails to load, the code is in the address bar).
AUTHORIZE_URL = f"{AUTH_BASE}/oauth/authorize"
BROWSER_REDIRECT_URI = "http://localhost:1455/auth/callback"
BROWSER_SCOPE = "openid profile email offline_access"
BROWSER_TTL = 30 * 60
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_CLAIM = "https://api.openai.com/auth"
DEVICE_CODE_TTL = 15 * 60  # LiteLLM DEVICE_CODE_TIMEOUT_SECONDS
HTTP_TIMEOUT = 20.0

TOKEN_DIR = os.environ.get("CHATGPT_TOKEN_DIR") or os.path.expanduser("~/.config/litellm/chatgpt")
AUTH_FILE = os.path.join(TOKEN_DIR, os.environ.get("CHATGPT_AUTH_FILE", "auth.json"))
ACCOUNTS_DIR = os.path.join(TOKEN_DIR, "accounts")
PATCH_TARGET = "litellm.llms.chatgpt.authenticator"
CHAT_TARGET = "litellm.llms.chatgpt.chat.transformation"
RESPONSES_TARGET = "litellm.llms.chatgpt.responses.transformation"
BRIDGE_TARGET = "litellm.main"
PROXY_UTILS_TARGET = "litellm.proxy.utils"
REQUEST_PROCESSING_TARGET = "litellm.proxy.common_request_processing"
PROXY_TARGET = "litellm.proxy.proxy_server"
# The account's model list is the one the Codex CLI reads (codex-rs models manager); the
# backend wants a client_version and answers with the models this plan may use.
CHATGPT_API_BASE = os.environ.get("CHATGPT_API_BASE") or "https://chatgpt.com/backend-api/codex"
CODEX_CLIENT_VERSION = os.environ.get("WOOW_CODEX_CLIENT_VERSION", "0.160.1")

DEFAULT_ACCOUNT = "default"
ACCOUNT_REF_PREFIX = "woow-chatgpt:"
_ACCOUNT_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")


def not_connected_message(account: str) -> str:
    which = "" if account == DEFAULT_ACCOUNT else f" ({account})"
    which_zh = "" if account == DEFAULT_ACCOUNT else f"（{account}）"
    return (
        f"ChatGPT subscription{which} is not connected to this LiteLLM, or its sign-in has expired: "
        "sign in at /woow/chatgpt on this LiteLLM (LiteLLM admin login), or in LLM Credentials. "
        f"此 LiteLLM 的 ChatGPT 訂閱{which_zh}尚未連接或登入已失效：請用 LiteLLM 管理員登入後打開本服務網址的 "
        "/woow/chatgpt（或後台 LLM Credentials）重新連接。"
    )


NOT_CONNECTED = not_connected_message(DEFAULT_ACCOUNT)

# R02: a chatgpt/ deployment uses the account it is set up with (model_list / DB). A request
# naming another account (api_key "woow-chatgpt:<account>", litellm_credential_name, …) is refused.
ACCOUNT_OVERRIDE_MESSAGE = (
    "A ChatGPT subscription model uses the account its deployment is set up with; a request cannot choose "
    "the account (api_key woow-chatgpt:<account>, litellm_credential_name, custom_llm_provider or another key). "
    "ChatGPT 訂閱模型一律使用部署設定的帳號，請求不可指定帳號（api_key woow-chatgpt:<帳號>、"
    "litellm_credential_name、custom_llm_provider 或其他金鑰）。"
)


def _log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} woow-chatgpt: {msg}", file=sys.stderr, flush=True)


# ── accounts ──────────────────────────────────────────────────────────────────
def valid_account(account) -> bool:
    return isinstance(account, str) and bool(_ACCOUNT_RE.fullmatch(account))


def account_for_name(name: str) -> str:
    """A credential name → its account id: readable slug + a short hash (two names that slug the
    same never share an account). Deterministic, so a credential re-created under the same name
    finds its sign-in again."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:32] or "acct"
    if not slug[0].isalnum():
        slug = "a" + slug
    digest = hashlib.sha256(name.strip().encode()).hexdigest()[:6]
    return f"{slug}-{digest}"  # never "default": it always ends in -<hash>


def account_ref(account: str) -> str:
    return ACCOUNT_REF_PREFIX + account


def parse_account_ref(value) -> str | None:
    if isinstance(value, str) and value.startswith(ACCOUNT_REF_PREFIX):
        account = value[len(ACCOUNT_REF_PREFIX):]
        return account if valid_account(account) else None
    return None


def auth_path(account: str) -> str:
    return AUTH_FILE if account == DEFAULT_ACCOUNT else os.path.join(ACCOUNTS_DIR, f"{account}.json")


def _pending_path(account: str) -> str:
    return os.path.join(TOKEN_DIR, "woow-pending.json" if account == DEFAULT_ACCOUNT else f"woow-pending-{account}.json")


def _browser_pending_path(account: str) -> str:
    return os.path.join(
        TOKEN_DIR, "woow-browser-pending.json" if account == DEFAULT_ACCOUNT else f"woow-browser-pending-{account}.json"
    )


def _account_for_path(path: str) -> str | None:
    path = os.path.abspath(path)
    if path == os.path.abspath(AUTH_FILE):
        return DEFAULT_ACCOUNT
    if os.path.dirname(path) == os.path.abspath(ACCOUNTS_DIR) and path.endswith(".json"):
        account = os.path.basename(path)[:-5]
        return account if valid_account(account) else None
    return None


def file_accounts() -> list[str]:
    out = [DEFAULT_ACCOUNT] if os.path.exists(AUTH_FILE) else []
    try:
        for name in sorted(os.listdir(ACCOUNTS_DIR)):
            if name.endswith(".json") and valid_account(name[:-5]):
                out.append(name[:-5])
    except OSError:
        pass
    return out


# ── files ─────────────────────────────────────────────────────────────────────
def _read_json(path: str) -> dict | None:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path: str, data: dict) -> None:
    """Atomic replace, 0600 (mkstemp), same directory."""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".woow-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _has_tokens(record: dict | None) -> bool:
    return bool(record and record.get("access_token") and record.get("refresh_token"))


def account_record(account: str) -> dict | None:
    return _PENDING_AUTH_WRITES.get(os.path.abspath(auth_path(account))) or _read_json(auth_path(account))


# ── database copy ─────────────────────────────────────────────────────────────
# libpq understands these URL query parameters; Prisma-style ones (schema, connection_limit,
# pool_timeout, …) that LiteLLM's DATABASE_URL may carry would make psycopg refuse the URL.
_LIBPQ_URL_PARAMS = frozenset(
    {"sslmode", "sslrootcert", "sslcert", "sslkey", "sslpassword", "connect_timeout", "options",
     "target_session_attrs", "application_name", "gssencmode", "channel_binding", "host", "port"}
)


def db_available() -> bool:
    """A database to keep the sign-ins in: the WOOW chart's DATABASE_* parts, or a generic
    LiteLLM DATABASE_URL. Without one the token files (CHATGPT_TOKEN_DIR) must be persistent."""
    if not (os.environ.get("DATABASE_HOST") or os.environ.get("DATABASE_URL")):
        return False
    try:
        import psycopg  # noqa: F401 — in litellm[extra-proxy] images
    except ImportError:
        return False
    return True


def _connect():
    import psycopg

    common = {"connect_timeout": 5, "application_name": "woow-chatgpt", "autocommit": True}
    if os.environ.get("DATABASE_HOST"):
        host, _, port = os.environ["DATABASE_HOST"].partition(":")
        return psycopg.connect(
            host=host,
            port=int(port or 5432),
            user=os.environ["DATABASE_USERNAME"],
            password=os.environ["DATABASE_PASSWORD"],
            dbname=os.environ["DATABASE_NAME"],
            **common,
        )
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    url = urlsplit(os.environ["DATABASE_URL"])
    query = urlencode([(k, v) for k, v in parse_qsl(url.query) if k in _LIBPQ_URL_PARAMS])
    return psycopg.connect(urlunsplit(url._replace(query=query)), **common)


_TABLES_READY = False


def _ensure_tables(conn) -> None:
    """The WOOW chart's db-prepare creates them; anywhere else the first save does."""
    global _TABLES_READY
    if _TABLES_READY:
        return
    conn.execute("CREATE SCHEMA IF NOT EXISTS woow_platform")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS woow_platform.chatgpt_accounts ("
        " account text PRIMARY KEY, auth jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS woow_platform.chatgpt_auth ("
        " id smallint PRIMARY KEY CHECK (id = 1), auth jsonb NOT NULL,"
        " updated_at timestamptz NOT NULL DEFAULT now())"
    )
    _TABLES_READY = True


def _table_exists(conn, table: str) -> bool:
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL", (f"woow_platform.{table}",)).fetchone()[0] is True


def db_load(account: str = DEFAULT_ACCOUNT) -> dict | None:
    if not db_available():
        return None
    with _connect() as conn:
        if _table_exists(conn, "chatgpt_accounts"):
            row = conn.execute("SELECT auth::text FROM woow_platform.chatgpt_accounts WHERE account = %s", (account,)).fetchone()
            if row:
                return json.loads(row[0])
        if account == DEFAULT_ACCOUNT and _table_exists(conn, "chatgpt_auth"):  # 0.2.0–0.2.2
            row = conn.execute("SELECT auth::text FROM woow_platform.chatgpt_auth WHERE id = 1").fetchone()
            if row:
                return json.loads(row[0])
    return None


def db_accounts() -> dict[str, dict]:
    """Every stored sign-in: {account: record}."""
    if not db_available():
        return {}
    out: dict[str, dict] = {}
    with _connect() as conn:
        if _table_exists(conn, "chatgpt_auth"):
            row = conn.execute("SELECT auth::text FROM woow_platform.chatgpt_auth WHERE id = 1").fetchone()
            if row:
                out[DEFAULT_ACCOUNT] = json.loads(row[0])
        if _table_exists(conn, "chatgpt_accounts"):
            for account, auth in conn.execute("SELECT account, auth::text FROM woow_platform.chatgpt_accounts"):
                if valid_account(account):
                    out[account] = json.loads(auth)
    return out


def db_save(account: str, record: dict) -> None:
    if not db_available():
        return
    with _connect() as conn:
        _ensure_tables(conn)
        conn.execute(
            "INSERT INTO woow_platform.chatgpt_accounts (account, auth, updated_at) VALUES (%s, %s::jsonb, now()) "
            "ON CONFLICT (account) DO UPDATE SET auth = EXCLUDED.auth, updated_at = now()",
            (account, json.dumps(record)),
        )
        if account == DEFAULT_ACCOUNT:  # keep the 0.2.x row current: a chart rollback reads it
            conn.execute(
                "INSERT INTO woow_platform.chatgpt_auth (id, auth, updated_at) VALUES (1, %s::jsonb, now()) "
                "ON CONFLICT (id) DO UPDATE SET auth = EXCLUDED.auth, updated_at = now()",
                (json.dumps(record),),
            )


def db_delete(account: str, connection=None) -> None:
    """Delete both schema generations in the caller's transaction when supplied."""
    if connection is None:
        if not db_available():
            return
        with _connect() as conn, conn.transaction():
            db_delete(account, conn)
        return
    if _table_exists(connection, "chatgpt_accounts"):
        connection.execute("DELETE FROM woow_platform.chatgpt_accounts WHERE account = %s", (account,))
    if account == DEFAULT_ACCOUNT and _table_exists(connection, "chatgpt_auth"):
        connection.execute("DELETE FROM woow_platform.chatgpt_auth WHERE id = 1")


def _db_save_logged(account: str, record: dict) -> bool:
    try:
        db_save(account, record)
        return True
    except Exception as exc:  # noqa: BLE001 — the file is written; a restart would fall back to the older copy
        _log(f"WARNING could not copy the ChatGPT sign-in ({account}) into the database: {type(exc).__name__}")
        return False


# ── tokens ────────────────────────────────────────────────────────────────────
def _jwt_claims(token: str | None) -> dict:
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return claims if isinstance(claims, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _token_account_id(token: str | None) -> str | None:
    auth = _jwt_claims(token).get(AUTH_CLAIM)
    account_id = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    return account_id if isinstance(account_id, str) and account_id else None


def build_record(access_token: str, refresh_token: str, id_token: str) -> dict:
    """Same shape as LiteLLM's Authenticator._build_auth_record (1.103.3, 1.104.0)."""
    exp = _jwt_claims(access_token).get("exp")
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "expires_at": int(exp) if isinstance(exp, (int, float)) else None,
        "account_id": _token_account_id(id_token) or _token_account_id(access_token),
    }


def describe(record: dict | None) -> dict:
    """Who is signed in — never a token."""
    if not _has_tokens(record):
        return {"connected": False}
    claims = _jwt_claims(record.get("id_token")) or {}
    auth = claims.get(AUTH_CLAIM) if isinstance(claims.get(AUTH_CLAIM), dict) else {}
    account_id = record.get("account_id") or auth.get("chatgpt_account_id") or ""
    return {
        "connected": True,
        "email": claims.get("email"),
        "plan_type": auth.get("chatgpt_plan_type"),
        "account_id_tail": account_id[-6:] if account_id else None,
        "access_token_expires_at": record.get("expires_at"),
        "subscription_active_until": auth.get("chatgpt_subscription_active_until"),
    }


# ── LiteLLM patches (inside the proxy) ────────────────────────────────────────
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_AUTHENTICATORS: dict[str, object] = {}
_PENDING_AUTH_WRITES: dict[str, dict] = {}
_AUTH_WRITE_ERRORS: dict[str, str] = {}
# R14 (all keyed by the token file's absolute path, this process only):
_DB_HOLDS: dict[str, dict] = {}  # the record the database is known to hold (no re-save needed)
_RECONCILED: set[str] = set()  # compared with the database copy since this process started
_RECONCILE_FAILED_AT: dict[str, float] = {}  # monotonic time of the last failed comparison
_TOKEN_UNUSABLE: dict[str, str] = {}  # the last token attempt failed with 401 (no success since)
RECONCILE_RETRY_SECONDS = 30.0


def _record_revision(record: dict | None) -> int:
    revision = (record or {}).get("_woow_updated_at_ns", 0)
    return revision if isinstance(revision, int) and not isinstance(revision, bool) else 0


def _persist_auth_record(path: str, record: dict) -> bool:
    """Attempt both copies independently; keep an unsaved rotation available for retry.

    The revision lets restore choose a newer DB copy over an old file after ENOSPC,
    or a newer file over an old DB copy after a database outage. Legacy ties keep
    the historical file-first behavior. The caller holds this account's lock.
    """
    path = os.path.abspath(path)
    _PENDING_AUTH_WRITES[path] = record
    file_ok = False
    try:
        _write_json(path, record)
        file_ok = True
    except OSError as exc:
        _log(f"ERROR ChatGPT sign-in file write failed: {type(exc).__name__}")
    account = _account_for_path(path)
    needs_db = bool(account and _has_tokens(record) and db_available())
    db_ok = _db_save_logged(account, record) if needs_db else False
    if db_ok:
        _DB_HOLDS[path] = record
    else:
        _DB_HOLDS.pop(path, None)
    if file_ok:
        _PENDING_AUTH_WRITES.pop(path, None)
    if not file_ok:
        _AUTH_WRITE_ERRORS[path] = "token_file_write_failed"
    elif needs_db and not db_ok:
        _AUTH_WRITE_ERRORS[path] = "token_database_write_failed"
    else:
        _AUTH_WRITE_ERRORS.pop(path, None)
    if not file_ok and not db_ok:
        raise OSError("ChatGPT sign-in could not be saved; retry after storage recovery")
    return db_ok if needs_db else file_ok


def _new_auth_record(path: str, data: dict) -> dict:
    record = dict(data)
    record["_woow_updated_at_ns"] = max(
        time.time_ns(), _record_revision(_read_json(path)) + 1,
        _record_revision(_PENDING_AUTH_WRITES.get(os.path.abspath(path))) + 1,
    )
    return record


def _lock_for(path: str) -> threading.RLock:
    key = os.path.abspath(path)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _is_newer(stored: dict | None, record: dict | None) -> bool:
    """The durable (database) copy should replace what this process uses."""
    return _has_tokens(stored) and (not _has_tokens(record) or _record_revision(stored) > _record_revision(record))


def _adopt_durable(account: str, path: str, stored: dict | None) -> str | None:
    """Caller holds the account's lock. When the database copy is newer than what this process
    uses (pending or file), use it from now on — memory first, so a failing file write can never
    put the older (already rotated) refresh token back in use; then write the file.
    → None (not newer), "file" (file written) or "memory" (file write failed; kept in memory)."""
    key = os.path.abspath(path)
    if not _is_newer(stored, _PENDING_AUTH_WRITES.get(key) or _read_json(path)):
        return None
    _PENDING_AUTH_WRITES[key] = stored
    _DB_HOLDS[key] = stored
    _TOKEN_UNUSABLE.pop(key, None)
    try:
        _write_json(path, stored)
    except OSError as exc:
        _AUTH_WRITE_ERRORS[key] = "token_file_write_failed"
        _log(f"WARNING ChatGPT sign-in ({account}): the database holds a newer copy than the token file, "
             f"which cannot be written ({type(exc).__name__}); using the database copy from memory")
        return "memory"
    _PENDING_AUTH_WRITES.pop(key, None)
    _AUTH_WRITE_ERRORS.pop(key, None)
    return "file"


def _reconcile_once(path: str) -> None:
    """First token use of an account in this process (caller holds its lock): adopt a newer
    database copy. The WOOW chart restores in a separate process before the proxy starts; when
    that restore could not write the token file, the proxy would otherwise read the old file and
    send an already-rotated refresh token to OpenAI (R14). A failed comparison is retried after
    RECONCILE_RETRY_SECONDS, never on every request."""
    key = os.path.abspath(path)
    if key in _RECONCILED:
        return
    account = _account_for_path(path)
    if account is None or not db_available():
        _RECONCILED.add(key)
        return
    failed_at = _RECONCILE_FAILED_AT.get(key)
    if failed_at is not None and time.monotonic() - failed_at < RECONCILE_RETRY_SECONDS:
        return
    try:
        _adopt_durable(account, path, db_load(account))
    except Exception as exc:  # noqa: BLE001 — the file stays in use; retried later
        _RECONCILE_FAILED_AT[key] = time.monotonic()
        _log(f"WARNING could not compare the ChatGPT sign-in ({account}) with its database copy: {type(exc).__name__}")
        return
    _RECONCILE_FAILED_AT.pop(key, None)
    _RECONCILED.add(key)


def _retry_pending(path: str, pending: dict, error_cls) -> None:
    """Caller holds the lock. A record only this process holds in memory: try to save it again.
    When the database already holds exactly this record only the file is behind — retry the
    file alone (no database write per request while the token directory is unwritable)."""
    key = os.path.abspath(path)
    if _DB_HOLDS.get(key) == pending:
        try:
            _write_json(path, pending)
        except OSError:
            return  # still served from memory; _AUTH_WRITE_ERRORS keeps saying why
        if _PENDING_AUTH_WRITES.get(key) is pending:
            _PENDING_AUTH_WRITES.pop(key, None)
        _AUTH_WRITE_ERRORS.pop(key, None)
        return
    try:
        _persist_auth_record(path, pending)
    except OSError:
        raise error_cls(message="ChatGPT sign-in storage failed; retry after storage recovery", status_code=503) from None


def authenticator(account: str):
    """LiteLLM's (patched) Authenticator reading this account's token file."""
    with _LOCKS_GUARD:
        auth = _AUTHENTICATORS.get(account)
    if auth is None:
        from litellm.llms.chatgpt.authenticator import Authenticator

        auth = Authenticator()
        auth.auth_file = auth_path(account)
        os.makedirs(os.path.dirname(auth.auth_file), mode=0o700, exist_ok=True)
        with _LOCKS_GUARD:
            auth = _AUTHENTICATORS.setdefault(account, auth)
    return auth


def _looks_like_access_token(value) -> bool:
    return isinstance(value, str) and value.count(".") == 2 and _token_account_id(value) is not None


def resolve(api_key) -> tuple[str, str | None, str]:
    """api_key of a chatgpt/ request → (access token, ChatGPT account id, account).
    ``woow-chatgpt:<account>`` → that account; an already-resolved access token → itself;
    anything else (none) → the default account. Raises LiteLLM's GetAccessTokenError."""
    if _looks_like_access_token(api_key):
        return api_key, _token_account_id(api_key), "?"
    account = parse_account_ref(api_key) or DEFAULT_ACCOUNT
    auth = authenticator(account)
    token = auth.get_access_token()
    return token, _token_account_id(token) or auth.get_account_id(), account


# ── account binding (R02) ─────────────────────────────────────────────────────
def credential_accounts() -> dict[str, str]:
    """{credential name: account} for every ChatGPT credential. A ChatGPT credential without an
    account reference (made by chart 0.2.2, before several accounts existed) belongs to the
    default account — at request time it carries no api_key, so it resolves there too."""
    import litellm

    out = {}
    for cred in list(getattr(litellm, "credential_list", None) or []):
        values = getattr(cred, "credential_values", None) or {}
        account = parse_account_ref(values.get("api_key"))
        if account:
            out[cred.credential_name] = account
        elif _is_chatgpt_credential(cred) and not values.get("api_key"):
            out[cred.credential_name] = DEFAULT_ACCOUNT
    return out


def _is_chatgpt_credential(cred) -> bool:
    info = getattr(cred, "credential_info", None) or {}
    return str(info.get("custom_llm_provider") or "").lower() == "chatgpt"


def is_chatgpt_params(params) -> bool:
    params = params or {}
    return str(params.get("model") or "").startswith("chatgpt/") or params.get("custom_llm_provider") == "chatgpt"


def deployment_account(params: dict, credentials: dict | None = None) -> str:
    """The account a chatgpt/ deployment's litellm_params are bound to: its own reference, else its
    ChatGPT credential's account, else the default account (what resolve() picks for it)."""
    params = params or {}
    if credentials is None:
        credentials = credential_accounts()
    return (parse_account_ref(params.get("api_key")) or credentials.get(params.get("litellm_credential_name"))
            or DEFAULT_ACCOUNT)


def _call_model_id(litellm_params) -> str | None:
    """The router deployment a call was made for. When the caller supplied credentials, LiteLLM's
    Router serves the call from a per-request copy of the deployment with a new id and keeps the
    configured deployment's id as original_model_id."""
    info = litellm_params.get("model_info") if isinstance(litellm_params, dict) else getattr(litellm_params, "model_info", None)
    if not isinstance(info, dict):
        info = {"id": getattr(info, "id", None), "original_model_id": getattr(info, "original_model_id", None)}
    for model_id in (info.get("original_model_id"), info.get("id")):
        if isinstance(model_id, str) and model_id:
            return model_id
    return None


def _router_deployment(model_id: str | None) -> dict | None:
    if not model_id:
        return None
    router = getattr(sys.modules.get(PROXY_TARGET), "llm_router", None)
    for d in list(getattr(router, "model_list", None) or []):
        if isinstance(d, dict) and (d.get("model_info") or {}).get("id") == model_id:
            return d
    return None


def account_override_error(model):
    from litellm.exceptions import BadRequestError

    return BadRequestError(message=ACCOUNT_OVERRIDE_MESSAGE, model=model, llm_provider="chatgpt")


# Headers that carry the ChatGPT account (lower-cased): only the resolved account sets them.
ACCOUNT_HEADERS = frozenset({"authorization", "chatgpt-account-id"})


# What validate_environment resolved for the call in progress (same task, lower-cased names).
_RESOLVED_ACCOUNT_HEADERS: contextvars.ContextVar = contextvars.ContextVar("woow_chatgpt_account_headers", default=None)


def remember_account_headers(headers: dict) -> None:
    _RESOLVED_ACCOUNT_HEADERS.set({name.lower(): value for name, value in headers.items() if name.lower() in ACCOUNT_HEADERS})


def check_account_headers(model, headers) -> None:
    """R02, header seam (provider half; the proxy's pre-call guard is the other). LiteLLM applies a
    request's extra_headers after validate_environment (``headers.update(extra_headers)``), so an
    Authorization or ChatGPT-Account-Id header in the request, in any spelling, replaced the
    account's: the bound token went out with another account id, or another token went out. The
    final headers must carry exactly what validate_environment resolved for this call; anything
    else is refused like the other account choices, also on paths the pre-call guard cannot see
    (router-level fallbacks)."""
    resolved = _RESOLVED_ACCOUNT_HEADERS.get()
    if resolved is None:
        return
    for name, value in (headers or {}).items():
        lowered = str(name).lower()
        if lowered in ACCOUNT_HEADERS and value != resolved.get(lowered):
            raise account_override_error(model)


def check_deployment_binding(model, api_key, litellm_params) -> None:
    """Provider-side half of R02 (the proxy's pre-call guard is the other): a call the router made
    for one of its deployments must use that deployment's account. LiteLLM merges request values
    over the deployment's (``{**deployment, **request}``), so a request-level api_key — a
    reference, a credential's reference, or any other string (which would resolve to the default
    account) — would otherwise switch accounts, also on paths the pre-call guard cannot see
    (router-level fallbacks). A real access token is the caller's own ChatGPT sign-in and passes.
    Calls that are not a router deployment's (the admin /api/test, SDK use) are not checked."""
    if _looks_like_access_token(api_key):
        return
    deployment = _router_deployment(_call_model_id(litellm_params))
    if deployment is None:
        return
    params = deployment.get("litellm_params") or {}
    if not is_chatgpt_params(params) or (parse_account_ref(api_key) or DEFAULT_ACCOUNT) != deployment_account(params):
        raise account_override_error(model)


def _patch_authenticator(module) -> None:
    cls = module.Authenticator
    if getattr(cls, "_woow_patched", False):
        return
    error_cls = module.GetAccessTokenError

    def _login_device_code(self):
        account = _account_for_path(self.auth_file) or DEFAULT_ACCOUNT
        raise error_cls(message=not_connected_message(account), status_code=401)

    def _get_device_code_cooldown_remaining(self, auth_data):  # noqa: ARG001
        return 0.0

    def _write_auth_file(self, data):
        with _lock_for(self.auth_file):
            try:
                _persist_auth_record(self.auth_file, _new_auth_record(self.auth_file, data))
            except OSError:
                raise error_cls(message="ChatGPT sign-in storage failed; retry after storage recovery", status_code=503) from None

    original_read_auth_file = cls._read_auth_file

    def _read_auth_file(self):
        return _PENDING_AUTH_WRITES.get(os.path.abspath(self.auth_file)) or original_read_auth_file(self)

    original_get_access_token = cls.get_access_token

    def get_access_token(self):
        key = os.path.abspath(self.auth_file)
        with _lock_for(self.auth_file):
            _reconcile_once(self.auth_file)  # R14: never start from an older file than the database
            pending = _PENDING_AUTH_WRITES.get(key)
            if pending:
                _retry_pending(self.auth_file, pending, error_cls)
            try:
                token = original_get_access_token(self)
            except error_cls as exc:
                if getattr(exc, "status_code", None) == 401:  # refresh refused / not signed in
                    _TOKEN_UNUSABLE[key] = "sign_in_rejected"
                raise
            _TOKEN_UNUSABLE.pop(key, None)
            return token

    cls._read_auth_file = _read_auth_file
    cls._login_device_code = _login_device_code
    cls._get_device_code_cooldown_remaining = _get_device_code_cooldown_remaining
    cls._write_auth_file = _write_auth_file
    cls.get_access_token = get_access_token
    cls._woow_patched = True
    drift = _constant_drift()
    if drift:
        _log(f"WARNING LiteLLM's ChatGPT OAuth constants differ from woow_chatgpt.py ({', '.join(drift)}): re-check the CLI")
    _log(
        "ChatGPT sign-in guard installed (no in-request device login; per-account sign-ins kept in "
        + ("the database)" if db_available() else f"{TOKEN_DIR} — keep it on a persistent volume)")
    )


def _auth_error(model, exc):
    from litellm.exceptions import AuthenticationError

    return AuthenticationError(model=model, llm_provider="chatgpt", message=str(getattr(exc, "message", None) or exc))


def _usage_limit_module():
    """woow_usage_limit (chart 0.3.14), or None: optional like woow_upstream_errors — a missing
    file or an import error only leaves a 429's headers as the backend sent them."""
    try:
        import woow_usage_limit
    except Exception as exc:  # noqa: BLE001
        if not getattr(_usage_limit_module, "_logged", False):
            _usage_limit_module._logged = True
            _log(f"exhausted-quota 429 gets no cooldown hint: {exc!r}")
        return None
    return woow_usage_limit


def _patch_error_class(cls) -> None:
    """An exhausted weekly quota (429 usage_limit_reached, no Retry-After) keeps its deployment out
    of the router's rotation for a while instead of the default 5 s (see woow_usage_limit)."""
    original = cls.get_error_class
    if getattr(original, "_woow_usage_limit", False):
        return

    def get_error_class(self, error_message, status_code, headers):
        module = _usage_limit_module()
        if module is not None:
            headers = module.with_cooldown_hint(status_code, error_message, headers)
        return original(self, error_message, status_code, headers)

    get_error_class._woow_usage_limit = True
    get_error_class.__wrapped__ = original
    cls.get_error_class = get_error_class


def _patch_chat(module) -> None:
    cls = module.ChatGPTConfig
    if getattr(cls, "_woow_patched", False):
        return
    from litellm.llms.chatgpt.common_utils import ensure_chatgpt_session_id, get_chatgpt_default_headers

    def _get_openai_compatible_provider_info(self, model, api_base, api_key, custom_llm_provider):
        # No token here: LiteLLM also calls this while it only builds a deployment. Pass the
        # account reference on; validate_environment (a real request) resolves it.
        ref = api_key if (parse_account_ref(api_key) or _looks_like_access_token(api_key)) else account_ref(DEFAULT_ACCOUNT)
        return self.authenticator.get_api_base(), ref, custom_llm_provider

    def validate_environment(self, headers, model, messages, optional_params, litellm_params, api_key=None, api_base=None):
        key = api_key or (litellm_params or {}).get("api_key")
        check_deployment_binding(model, key, litellm_params)
        try:
            token, account_id, _ = resolve(key)
        except Exception as exc:  # noqa: BLE001 — GetAccessTokenError
            raise _auth_error(model, exc) from None
        validated = super(cls, self).validate_environment(
            headers, model, messages, optional_params, litellm_params, token, api_base
        )
        session_id = ensure_chatgpt_session_id(litellm_params)
        resolved = get_chatgpt_default_headers(token, account_id, session_id)
        remember_account_headers(resolved)
        return {**resolved, **validated}

    cls._get_openai_compatible_provider_info = _get_openai_compatible_provider_info
    cls.validate_environment = validate_environment
    _patch_error_class(cls)
    cls._woow_patched = True


def _patch_responses_bridge(module) -> None:
    """chatgpt is one of LiteLLM's openai-compatible providers: a chat completion whose model
    the model map does not mark ``mode: responses`` goes out through the OpenAI SDK, which never
    calls ``validate_environment`` — the bearer is the unresolved reference
    "woow-chatgpt:<account>" and the backend (Responses API only) refuses it. Models added
    from the sign-in page are registered as responses models (``register_model_costs``); one
    added by hand, on an instance with no signed-in account to load the model list from, was
    not (M4 PLG-13 control on HA 0.1.14, 2026-10-24). Every caller looks the function up on
    ``litellm.main`` at call time, so replacing the module attribute covers them all."""
    original = getattr(module, "responses_api_bridge_check", None)
    if original is None or getattr(original, "_woow_chatgpt", False):
        return

    def responses_api_bridge_check(model, custom_llm_provider, *args, **kwargs):
        model_info, model = original(model, custom_llm_provider, *args, **kwargs)
        if custom_llm_provider == "chatgpt" and (model_info or {}).get("mode") != "responses":
            model_info = {**(model_info or {}), "mode": "responses"}
        return model_info, model

    responses_api_bridge_check._woow_chatgpt = True
    responses_api_bridge_check.__wrapped__ = original
    module.responses_api_bridge_check = responses_api_bridge_check


def _patch_responses(module) -> None:
    cls = module.ChatGPTResponsesAPIConfig
    if getattr(cls, "_woow_patched", False):
        return
    from litellm.llms.chatgpt.common_utils import ensure_chatgpt_session_id, get_chatgpt_default_headers

    def validate_environment(self, headers, model, litellm_params):
        key = getattr(litellm_params, "api_key", None) if litellm_params is not None else None
        if key is None and isinstance(litellm_params, dict):
            key = litellm_params.get("api_key")
        check_deployment_binding(model, key, litellm_params)
        try:
            token, account_id, _ = resolve(key)
        except Exception as exc:  # noqa: BLE001 — GetAccessTokenError
            raise _auth_error(model, exc) from None
        session_id = ensure_chatgpt_session_id(litellm_params)
        resolved = get_chatgpt_default_headers(token, account_id, session_id)
        remember_account_headers(resolved)
        return {**resolved, **headers}

    original_transform = cls.transform_responses_api_request

    def transform_responses_api_request(self, *args, **kwargs):
        # Runs on the final headers, after LiteLLM applied the request's extra_headers.
        headers = kwargs["headers"] if "headers" in kwargs else (args[4] if len(args) > 4 else None)
        check_account_headers(kwargs.get("model", args[0] if args else None), headers)
        return original_transform(self, *args, **kwargs)

    cls.validate_environment = validate_environment
    cls.transform_responses_api_request = transform_responses_api_request
    _patch_error_class(cls)
    cls._woow_patched = True


def _patch_proxy_logging(module) -> None:
    """R02: run the ChatGPT account guard (woow_chatgpt_plugin.check_request) in front of every
    proxy pre-call hook. The WOOW chart and the HA add-on load the plugin through sitecustomize,
    not litellm_settings.callbacks, so a CustomLogger hook alone never ran there — and a callbacks
    list set in the admin UI could drop it anyway. This seam runs before routing, while request
    values are still separate from the deployment's."""
    cls = getattr(module, "ProxyLogging", None)
    original = getattr(cls, "pre_call_hook", None)
    if original is None or getattr(original, "_woow_account_guard", False):
        return

    async def pre_call_hook(self, *args, **kwargs):
        data = kwargs["data"] if "data" in kwargs else (args[1] if len(args) > 1 else None)
        user = kwargs["user_api_key_dict"] if "user_api_key_dict" in kwargs else (args[0] if args else None)
        if isinstance(data, dict):
            import woow_chatgpt_plugin

            woow_chatgpt_plugin.check_request(user, data)
            reasoning = _reasoning_module()
            if reasoning is not None:
                reasoning.pre_call(data)
            unpriced = _unpriced_module()
            if unpriced is not None:
                unpriced.pre_call(user, data)
        return await original(self, *args, **kwargs)

    pre_call_hook._woow_account_guard = True
    pre_call_hook.__wrapped__ = original
    cls.pre_call_hook = pre_call_hook
    _patch_reasoning_floor(cls)


def _reasoning_module():
    """woow_reasoning (AI-FIX-LLM-2), or None: the module is optional — a missing file (an older
    HA add-on bundle) or an import error only leaves requests as they were."""
    try:
        import woow_reasoning
    except Exception as exc:  # noqa: BLE001
        if not getattr(_reasoning_module, "_logged", False):
            _reasoning_module._logged = True
            _log(f"reasoning output floor NOT active: {exc!r}")
        return None
    return woow_reasoning


def _unpriced_module():
    """woow_unpriced (AI-FIX-LLM-4), or None: optional like woow_reasoning — a missing file or an
    import error only leaves requests as they were."""
    try:
        import woow_unpriced
    except Exception as exc:  # noqa: BLE001
        if not getattr(_unpriced_module, "_logged", False):
            _unpriced_module._logged = True
            _log(f"unpriced-model budget warning NOT active: {exc!r}")
        return None
    return woow_unpriced


def _patch_reasoning_floor(cls) -> None:
    """Explain a raised output limit / an empty `length` reply / a model without a price on the
    response (the request side runs inside the pre-call wrapper above, after the account guard)."""
    post = getattr(cls, "post_call_success_hook", None)
    if post is None or getattr(post, "_woow_reasoning_floor", False):
        return

    async def post_call_success_hook(self, *args, **kwargs):
        out = await post(self, *args, **kwargs)
        reasoning = _reasoning_module()
        if reasoning is not None:
            reasoning.post_call(out if out is not None else kwargs.get("response"))
        unpriced = _unpriced_module()
        if unpriced is not None:
            unpriced.post_call(out if out is not None else kwargs.get("response"))
        return out

    post_call_success_hook._woow_reasoning_floor = True
    post_call_success_hook.__wrapped__ = post
    cls.post_call_success_hook = post_call_success_hook


def _upstream_errors_module():
    """woow_upstream_errors (AUDITFIX litellm-retry-timeout), or None: optional like woow_reasoning —
    a missing file or an import error only leaves error answers as they were."""
    try:
        import woow_upstream_errors
    except Exception as exc:  # noqa: BLE001
        if not getattr(_upstream_errors_module, "_logged", False):
            _upstream_errors_module._logged = True
            _log(f"upstream retry-after / timeout answers NOT adjusted: {exc!r}")
        return None
    return woow_upstream_errors


def _patch_request_processing(module) -> None:
    """Upstream retry hints and timeouts: woow_upstream_errors fixes up the answer that
    ProxyBaseLLMRequestProcessing._handle_llm_api_exception raises for a failed request
    (forwards Retry-After / x-should-retry under their own names; an upstream timeout is 504)."""
    cls = getattr(module, "ProxyBaseLLMRequestProcessing", None)
    original = getattr(cls, "_handle_llm_api_exception", None)
    if original is None or getattr(original, "_woow_upstream_errors", False):
        return

    async def _handle_llm_api_exception(self, *args, **kwargs):
        cause = kwargs["e"] if "e" in kwargs else (args[0] if args else None)
        try:
            return await original(self, *args, **kwargs)
        except Exception as answer:
            upstream_errors = _upstream_errors_module()
            if upstream_errors is not None:
                upstream_errors.adjust(answer, cause)
            raise

    _handle_llm_api_exception._woow_upstream_errors = True
    _handle_llm_api_exception.__wrapped__ = original
    cls._handle_llm_api_exception = _handle_llm_api_exception


def _install_plugin_routes(module) -> None:
    """litellm.proxy.proxy_server imported → put the sign-in page and API on its app."""
    import woow_chatgpt_plugin

    woow_chatgpt_plugin.install(module.app)


# module → what to do once it has been executed
_HOOKS = {
    PATCH_TARGET: _patch_authenticator,
    CHAT_TARGET: _patch_chat,
    RESPONSES_TARGET: _patch_responses,
    BRIDGE_TARGET: _patch_responses_bridge,
    PROXY_UTILS_TARGET: _patch_proxy_logging,
    REQUEST_PROCESSING_TARGET: _patch_request_processing,
    PROXY_TARGET: _install_plugin_routes,
}


class _PatchFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        hook = _HOOKS.get(fullname)
        if hook is None:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None and spec.loader is not None:
                break
        else:
            return None
        original_exec = spec.loader.exec_module

        def exec_module(module):
            original_exec(module)
            try:
                hook(module)
            except Exception as exc:  # noqa: BLE001 — never stop LiteLLM from starting
                _log(f"ERROR could not hook {fullname}: {exc!r}")

        spec.loader.exec_module = exec_module
        return spec


def install_import_hook(include_routes: bool = True) -> None:
    """Patch LiteLLM's ChatGPT provider and (include_routes) put the sign-in page on the proxy
    app — now for modules already imported, at import time for the others."""
    pending = False
    for name, hook in _HOOKS.items():
        if name == PROXY_TARGET and not include_routes:
            continue
        if name in sys.modules:
            hook(sys.modules[name])
        else:
            pending = True
    if pending and not any(isinstance(f, _PatchFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _PatchFinder())


def _constant_drift() -> list[str]:
    try:
        from litellm.llms.chatgpt import common_utils as cu
    except Exception as exc:  # noqa: BLE001
        return [f"import failed: {exc!r}"]
    expected = {
        "CHATGPT_AUTH_BASE": AUTH_BASE,
        "CHATGPT_DEVICE_CODE_URL": DEVICE_CODE_URL,
        "CHATGPT_DEVICE_TOKEN_URL": DEVICE_TOKEN_URL,
        "CHATGPT_OAUTH_TOKEN_URL": OAUTH_TOKEN_URL,
        "CHATGPT_DEVICE_VERIFY_URL": DEVICE_VERIFY_URL,
        "CHATGPT_CLIENT_ID": CLIENT_ID,
    }
    return [k for k, v in expected.items() if getattr(cu, k, None) != v]


# ── an account's models ───────────────────────────────────────────────────────
def remote_models(account: str = DEFAULT_ACCOUNT) -> list[dict]:
    """Models this account's ChatGPT plan may use, straight from the Codex backend. Runs inside
    the proxy (the patched Authenticator refreshes the token under the lock and persists it).
    Raises when not signed in."""
    import httpx
    from litellm.llms.chatgpt import common_utils as cu

    token, account_id, _ = resolve(account_ref(account))
    originator = cu.get_chatgpt_originator()
    headers = {
        "Authorization": f"Bearer {token}",
        "originator": originator,
        "User-Agent": cu.get_chatgpt_user_agent(originator),
        "Accept": "application/json",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    resp = httpx.get(
        f"{CHATGPT_API_BASE}/models", params={"client_version": CODEX_CLIENT_VERSION}, headers=headers, timeout=HTTP_TIMEOUT
    )
    resp.raise_for_status()
    data = resp.json()
    rows = data.get("models", []) if isinstance(data, dict) else data
    out = []
    for m in rows:
        slug = m.get("slug") if isinstance(m, dict) else None
        if not slug:
            continue
        out.append(
            {
                "slug": slug,
                "model": f"chatgpt/{slug}",
                "display_name": m.get("display_name") or slug,
                "description": (m.get("description") or "")[:300],
                "visible": m.get("visibility") != "hide",
                "context_window": m.get("context_window"),
                "priority": m.get("priority"),
            }
        )
    out.sort(key=lambda x: (not x["visible"], x["priority"] if isinstance(x["priority"], int) else 999, x["slug"]))
    return out


_BUNDLED_CONTEXT: dict | None = None


def _bundled_context(key: str):
    """max_input_tokens of `key` in the cost map shipped inside the installed LiteLLM package
    (both PaaS and HA run with LITELLM_LOCAL_MODEL_COST_MAP=True), or None."""
    global _BUNDLED_CONTEXT
    if _BUNDLED_CONTEXT is None:
        try:
            import importlib.resources

            raw = importlib.resources.files("litellm").joinpath(
                "model_prices_and_context_window_backup.json").read_text(encoding="utf-8")
            _BUNDLED_CONTEXT = {k: v.get("max_input_tokens") for k, v in json.loads(raw).items()
                                if isinstance(v, dict)}
        except Exception:  # noqa: BLE001 — without the bundled map nothing is treated as bundled
            _BUNDLED_CONTEXT = {}
    return _BUNDLED_CONTEXT.get(key)


def register_model_costs(models: list[dict]) -> list[str]:
    """Teach LiteLLM unknown models and repair Router-created sparse entries (the map lags behind
    ChatGPT): mode=responses, so /chat/completions is bridged to the Codex Responses API instead
    of being sent to /codex/chat/completions (Cloudflare 403)."""
    import litellm

    new = {}
    for m in models:
        key = m["model"]
        existing = litellm.model_cost.get(key) or {}
        # Router can register a responses-only entry before the delayed catalog sync.
        # Presence alone does not mean LiteLLM knows its context/capabilities.
        if existing.get("max_input_tokens") is not None:
            # The account's catalog is the authority for a ChatGPT subscription's context
            # window; LiteLLM's bundled map lists the API window (e.g. 1050000 vs 272000).
            # Replace only the untouched bundled value. A value a deployment registered
            # (Router) or anyone else set differs from the bundled one and stays as it is.
            ctx = m.get("context_window")
            bundled = _bundled_context(key)
            if (isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0
                    and bundled is not None and existing["max_input_tokens"] == bundled != ctx):
                new[key] = {"max_input_tokens": ctx}
            continue
        ctx = m.get("context_window") if isinstance(m.get("context_window"), int) else 272000
        new[key] = {
            "litellm_provider": "chatgpt",
            "mode": "responses",
            "max_input_tokens": ctx,
            "max_tokens": 128000,
            "max_output_tokens": 128000,
            "input_cost_per_token": 0.0,
            "output_cost_per_token": 0.0,
            "supported_endpoints": ["/v1/chat/completions", "/v1/responses"],
            "supports_function_calling": True,
            "supports_parallel_function_calling": True,
            "supports_reasoning": True,
            "supports_vision": True,
        }
        # Missing/None is unknown; False, zero, empty lists and custom prices are
        # intentional overrides. register_model merges this delta into the entry.
        new[key] = {field: value for field, value in new[key].items()
                    if existing.get(field) is None}
        if not new[key]:
            del new[key]
    if new:
        litellm.register_model(new)
    return sorted(new)


# ── sign-in flows (per account) ───────────────────────────────────────────────
def _http():
    import httpx

    return httpx.Client(timeout=HTTP_TIMEOUT)


def _pending_view(pending: dict | None) -> dict | None:
    if not pending:
        return None
    return {
        "verification_url": DEVICE_VERIFY_URL,
        "user_code": pending.get("user_code"),
        "expires_at": int(pending.get("expires_at", 0)),
        "interval": int(pending.get("interval", 5)),
    }


def sign_in_state(account: str = DEFAULT_ACCOUNT) -> dict:
    """Who is signed in; serialize status healing with refresh and disconnect."""
    with _lock_for(auth_path(account)):
        return _sign_in_state_locked(account)


def _sign_in_state_locked(account: str) -> dict:
    """Reports the very record the request path uses (R14): a newer database copy is adopted
    first — also when the token file is missing — exactly as the first request would."""
    path = os.path.abspath(auth_path(account))
    stored, adopted, db_error = None, None, None
    if db_available():
        try:
            stored = db_load(account)
            # A refresh may have saved only its DB copy before the process died.
            # Status must never "heal" that newer copy back to the stale file.
            adopted = _adopt_durable(account, path, stored)
            _RECONCILED.add(path)
        except Exception as exc:  # noqa: BLE001
            db_error = type(exc).__name__
    record = account_record(account)
    out = {"account": account, **describe(record)}
    out["storage"] = "database" if db_available() else "file"
    if _has_tokens(record):
        try:
            if db_error:
                raise RuntimeError(db_error)
            if adopted:
                out["persisted"] = True
            elif not stored or stored.get("refresh_token") != record.get("refresh_token"):
                out["persisted"] = _db_save_logged(account, record)
            else:
                out["persisted"] = True
            if out["persisted"] and _AUTH_WRITE_ERRORS.get(path) == "token_database_write_failed":
                _AUTH_WRITE_ERRORS.pop(path, None)
        except Exception as exc:  # noqa: BLE001
            out["persisted"] = False
            out["persist_error"] = db_error or type(exc).__name__
        if out.get("connected") and _TOKEN_UNUSABLE.get(path):
            # The last request on this account could not get a token (refresh refused): the
            # sign-in on record is not usable, so the status must not say "connected".
            out["connected"] = False
            out["error"] = _TOKEN_UNUSABLE[path]
    write_error = _AUTH_WRITE_ERRORS.get(path)
    if write_error:
        out["persist_error"] = write_error
    pending = _read_json(_pending_path(account))
    if pending and time.time() >= float(pending.get("expires_at", 0)):
        _remove(_pending_path(account))
        pending = None
    out["pending"] = _pending_view(pending)
    browser = _read_json(_browser_pending_path(account))
    out["browser_pending"] = bool(browser and time.time() < float(browser.get("expires_at", 0)))
    return out


def device_start(account: str = DEFAULT_ACCOUNT) -> dict:
    with _http() as client:
        resp = client.post(DEVICE_CODE_URL, json={"client_id": CLIENT_ID})
    if resp.status_code != 200:
        return {"ok": False, "error": "device_code_request_failed", "status_code": resp.status_code}
    data = resp.json()
    device_auth_id = data.get("device_auth_id")
    user_code = data.get("user_code") or data.get("usercode")
    if not device_auth_id or not user_code:
        return {"ok": False, "error": "device_code_response_incomplete"}
    now = time.time()
    pending = {
        "device_auth_id": device_auth_id,
        "user_code": user_code,
        "interval": int(data.get("interval") or 5),
        "started_at": now,
        "expires_at": now + DEVICE_CODE_TTL,
    }
    _write_json(_pending_path(account), pending)
    return {"ok": True, "state": "pending", "account": account, **describe(account_record(account)), "pending": _pending_view(pending)}


def device_poll(account: str = DEFAULT_ACCOUNT) -> dict:
    path = _pending_path(account)
    pending = _read_json(path)
    if not pending:
        return {"ok": True, "account": account, "state": "connected" if _has_tokens(account_record(account)) else "idle"}
    if time.time() >= float(pending.get("expires_at", 0)):
        _remove(path)
        return {"ok": True, "account": account, "state": "expired"}
    with _http() as client:
        resp = client.post(
            DEVICE_TOKEN_URL,
            json={"device_auth_id": pending["device_auth_id"], "user_code": pending["user_code"]},
        )
        if resp.status_code in (403, 404):
            return {"ok": True, "account": account, "state": "pending", "pending": _pending_view(pending)}
        if resp.status_code != 200:
            return {"ok": False, "account": account, "state": "error", "error": "poll_failed", "status_code": resp.status_code}
        code = resp.json()
        if not (code.get("authorization_code") and code.get("code_verifier")):
            return {"ok": True, "account": account, "state": "pending", "pending": _pending_view(pending)}
        tok = client.post(
            OAUTH_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code["authorization_code"],
                "redirect_uri": DEVICE_REDIRECT_URI,
                "client_id": CLIENT_ID,
                "code_verifier": code["code_verifier"],
            },
        )
    if tok.status_code != 200:
        _remove(path)
        return {"ok": False, "account": account, "state": "error", "error": "token_exchange_failed", "status_code": tok.status_code}
    result = _connected_result(account, tok.json())
    if not result.get("ok"):
        _remove(path)
    return result


def identity(record: dict | None) -> tuple | None:
    """The ChatGPT user behind a sign-in: (user, workspace account). Team/Enterprise members share
    a chatgpt_account_id, so the user part (chatgpt_user_id, else e-mail) is what tells two
    sign-ins apart."""
    if not _has_tokens(record):
        return None
    claims = _jwt_claims(record.get("id_token")) or {}
    auth = claims.get(AUTH_CLAIM) if isinstance(claims.get(AUTH_CLAIM), dict) else {}
    user = auth.get("chatgpt_user_id") or auth.get("user_id") or claims.get("email")
    if not user:
        return None
    return (str(user).lower(), record.get("account_id") or auth.get("chatgpt_account_id"))


def duplicate_of(account: str, record: dict) -> str | None:
    """Another account already signed in as the same ChatGPT user: connecting it twice adds no
    allowance (limits are per ChatGPT user) and doubles every model in the shared groups."""
    me = identity(record)
    if me is None:
        return None
    others: dict[str, dict] = {}
    try:
        others.update(db_accounts())
    except Exception:  # noqa: BLE001
        pass
    for other in file_accounts():
        others[other] = account_record(other) or others.get(other) or {}
    for other, rec in others.items():
        if other != account and identity(rec) == me:
            return other
    return None


def _connected_result(account: str, t: dict) -> dict:
    if not (t.get("access_token") and t.get("refresh_token") and t.get("id_token")):
        return {"ok": False, "account": account, "state": "error", "error": "token_response_incomplete"}
    record = build_record(t["access_token"], t["refresh_token"], t["id_token"])
    other = duplicate_of(account, record)
    if other:
        _remove(_pending_path(account))
        _remove(_browser_pending_path(account))
        return {"ok": False, "account": account, "state": "error", "error": "duplicate_account",
                "duplicate_of": other, "email": describe(record).get("email")}
    with _lock_for(auth_path(account)):
        try:
            persisted = _persist_auth_record(auth_path(account), _new_auth_record(auth_path(account), record))
        except OSError:
            return {"ok": False, "account": account, "state": "error", "error": "token_storage_failed"}
        _TOKEN_UNUSABLE.pop(os.path.abspath(auth_path(account)), None)
    _remove(_pending_path(account))
    _remove(_browser_pending_path(account))
    return {"ok": True, "state": "connected", "account": account, **describe(record), "persisted": persisted}


def browser_start(account: str = DEFAULT_ACCOUNT) -> dict:
    """PKCE authorize URL for "browser login". The verifier stays in the token dir (0600)."""
    import secrets
    from urllib.parse import urlencode

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_hex(16)
    now = time.time()
    _write_json(_browser_pending_path(account), {"verifier": verifier, "state": state, "started_at": now, "expires_at": now + BROWSER_TTL})
    query = urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": BROWSER_REDIRECT_URI,
            "scope": BROWSER_SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": os.environ.get("CHATGPT_ORIGINATOR") or "codex_cli_rs",
        }
    )
    return {
        "ok": True,
        "state": "pending",
        "account": account,
        "authorize_url": f"{AUTHORIZE_URL}?{query}",
        "expires_at": int(now + BROWSER_TTL),
    }


def _parse_authorization_input(text: str) -> tuple[str | None, str | None]:
    """The pasted redirect URL, a query string, "code#state" or a bare code → (code, state)."""
    from urllib.parse import parse_qs, urlsplit

    value = (text or "").strip()
    if not value:
        return None, None
    if "://" in value:
        q = parse_qs(urlsplit(value).query)
        return (q.get("code") or [None])[0], (q.get("state") or [None])[0]
    if "code=" in value:
        q = parse_qs(value.lstrip("?"))
        return (q.get("code") or [None])[0], (q.get("state") or [None])[0]
    if "#" in value:
        code, state = value.split("#", 1)
        return code or None, state or None
    return value, None


def browser_finish(text: str, account: str = DEFAULT_ACCOUNT) -> dict:
    path = _browser_pending_path(account)
    pending = _read_json(path)
    if not pending:
        return {"ok": False, "account": account, "state": "error", "error": "no_browser_login"}
    if time.time() >= float(pending.get("expires_at", 0)):
        _remove(path)
        return {"ok": False, "account": account, "state": "expired", "error": "browser_login_expired"}
    code, state = _parse_authorization_input(text)
    if not code:
        return {"ok": False, "account": account, "state": "error", "error": "no_code"}
    if state and state != pending.get("state"):
        return {"ok": False, "account": account, "state": "error", "error": "state_mismatch"}
    with _http() as client:
        tok = client.post(
            OAUTH_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "code_verifier": pending["verifier"],
                "redirect_uri": BROWSER_REDIRECT_URI,
            },
        )
    if tok.status_code != 200:
        return {"ok": False, "account": account, "state": "error", "error": "token_exchange_failed", "status_code": tok.status_code}
    return _connected_result(account, tok.json())


def disconnect(account: str = DEFAULT_ACCOUNT) -> dict:
    """Keep the newest durable sign-in if deleting either store fails."""
    path = auth_path(account)
    cache_key = os.path.abspath(path)

    def remove_files():
        _remove(_pending_path(account))
        _remove(_browser_pending_path(account))
        _remove(path)

    with _lock_for(path):
        phase = "db_delete_failed"
        try:
            if db_available():
                record, saved = account_record(account), db_load(account)
                latest = saved if _has_tokens(saved) and (
                    not _has_tokens(record) or _record_revision(saved) > _record_revision(record)
                ) else record
                # Before unlinking the file, make its newer rotation durable in DB.
                # This is outside the deletion transaction: rollback must retain the
                # NEW token even when DB commit fails after local unlink succeeded.
                if _has_tokens(latest):
                    if latest != saved:
                        db_save(account, latest)
                    _PENDING_AUTH_WRITES[cache_key] = latest
                with _connect() as conn, conn.transaction():
                    db_delete(account, conn)
                    phase = "token_file_delete_failed"
                    remove_files()
                    phase = "db_delete_failed"  # transaction commit may still fail
            else:
                phase = "token_file_delete_failed"
                remove_files()
        except Exception as exc:  # noqa: BLE001 — rollback occurs before reporting failure
            return {"ok": False, "account": account, "state": "error", "error": phase, "detail": type(exc).__name__}
        for table in (_PENDING_AUTH_WRITES, _AUTH_WRITE_ERRORS, _DB_HOLDS, _TOKEN_UNUSABLE):
            table.pop(cache_key, None)
    return {"ok": True, "account": account, "state": "disconnected", "connected": False}


def restore() -> dict:
    """Restore the newest durable copy (before the proxy starts, and again inside it before the
    first model-list sync); never roll back rotation. Every account on its own (R14): one that
    fails is reported and the others still restore. A newer database copy whose token file
    cannot be written (EACCES, ENOSPC) is kept in memory — the same as a refresh whose file write
    failed — so this process never falls back to the older file; the proxy process adopts it on
    first use (_reconcile_once). A kept file whose database copy cannot be saved is reported
    (db_save_failed — what the HA add-on's apply_restore_fix.py added).
    → ok false with error (one kind, or restore_incomplete) and failed=[{account, error}]."""
    kept, restored, failed = [], [], []
    try:
        stored = db_accounts()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "state": "error", "error": "db_load_failed", "detail": type(exc).__name__, "kept": kept}
    for account in sorted(set(file_accounts()) | set(stored)):
        path = auth_path(account)
        try:
            with _lock_for(path):
                saved = stored.get(account)
                adopted = _adopt_durable(account, path, saved)
                if adopted:
                    restored.append(account)
                    if adopted == "memory":
                        failed.append({"account": account, "error": "token_file_write_failed"})
                elif _has_tokens(account_record(account)):
                    if not _db_save_logged(account, account_record(account)):
                        _log(f"WARNING ChatGPT restore persistence failed ({account}): db_save_failed; the token file is kept")
                        failed.append({"account": account, "error": "db_save_failed"})
                    elif db_available():
                        _DB_HOLDS[os.path.abspath(path)] = account_record(account)
                    kept.append(account)
                _RECONCILED.add(os.path.abspath(path))
        except Exception as exc:  # noqa: BLE001 — the next account still restores
            _log(f"WARNING ChatGPT sign-in restore failed ({account}): {type(exc).__name__}")
            failed.append({"account": account, "error": "restore_failed"})
    state = "restored" if restored else ("kept" if kept else "none")
    out = {"ok": not failed, "state": state, "connected": bool(kept or restored), "kept": kept, "restored": restored}
    if failed:
        kinds = {f["error"] for f in failed}
        out.update(state="error", error=kinds.pop() if len(kinds) == 1 else "restore_incomplete", failed=failed)
    return out


def list_accounts() -> list[str]:
    """Accounts with a sign-in (file or database) or one in progress."""
    names = set(file_accounts())
    try:
        names.update(a for a, r in db_accounts().items() if _has_tokens(r))
    except Exception:  # noqa: BLE001
        pass
    try:
        for name in os.listdir(TOKEN_DIR):
            m = re.fullmatch(r"woow-(?:browser-)?pending-([a-z0-9][a-z0-9_-]{0,47})\.json", name)
            if m:
                names.add(m.group(1))
    except OSError:
        pass
    names.add(DEFAULT_ACCOUNT)
    return [DEFAULT_ACCOUNT] + sorted(names - {DEFAULT_ACCOUNT})


# ── CLI ───────────────────────────────────────────────────────────────────────
def _proxy(method: str, path: str, body: dict | None = None, timeout: float = 30.0):
    import httpx

    port = os.environ.get("WOOW_PROXY_PORT", "4000")
    key = os.environ["LITELLM_MASTER_KEY"]
    with httpx.Client(timeout=timeout) as client:
        return client.request(
            method, f"http://127.0.0.1:{port}{path}", json=body, headers={"Authorization": f"Bearer {key}"}
        )


def _chatgpt_models() -> list[str] | None:
    try:
        resp = _proxy("GET", "/model/info", timeout=10.0)
    except Exception:  # noqa: BLE001 — proxy not up yet
        return None
    if resp.status_code != 200:
        return None
    return sorted(
        {
            m.get("model_name")
            for m in resp.json().get("data", [])
            if str((m.get("litellm_params") or {}).get("model", "")).startswith("chatgpt/")
        }
        - {None}
    )


def cmd_status(args, account) -> dict:
    return {"ok": True, **sign_in_state(account), "models": _chatgpt_models()}


def cmd_accounts(args, account) -> dict:
    return {"ok": True, "accounts": [{"account": a, **describe(account_record(a))} for a in list_accounts()]}


def cmd_add_models(args, account) -> dict:
    """Register chatgpt/<model> entries through the in-proxy plugin (it also teaches LiteLLM
    models newer than its bundled map). No args: WOOW_CHATGPT_DEFAULT_MODELS — "visible"
    (default) = every model the account lists, "all" = hidden ones too, or a comma list."""
    if not _has_tokens(account_record(account)):
        return {"ok": False, "error": "not_connected", "account": account}
    raw = list(args) or [m.strip() for m in os.environ.get("WOOW_CHATGPT_DEFAULT_MODELS", "visible").split(",") if m.strip()]
    selection = raw[0] if len(raw) == 1 and raw[0] in ("visible", "all") else raw
    resp = _proxy("POST", "/woow/chatgpt/api/models", {"action": "add", "account": account, "models": selection}, timeout=90.0)
    if resp.status_code != 200:
        return {"ok": False, "error": "model_add_failed", "status_code": resp.status_code}
    body = resp.json()
    return {"ok": not body.get("failed"), "added": body.get("added", []), "kept": body.get("kept", []), "failed": body.get("failed", [])}


def cmd_test(args, account) -> dict:
    """One tiny request on this account (uses a little of the plan's allowance)."""
    model = args[0] if args else "chatgpt/gpt-5.5"
    resp = _proxy("POST", "/woow/chatgpt/api/test", {"account": account, "model": model}, timeout=150.0)
    if resp.status_code != 200:
        return {"ok": False, "error": "test_failed", "status_code": resp.status_code}
    return resp.json()


def cmd_selfcheck(args, account) -> dict:
    drift = _constant_drift()
    return {"ok": not drift, "drift": drift}


COMMANDS = {
    "status": cmd_status,
    "accounts": cmd_accounts,
    "start": lambda args, account: device_start(account),
    "poll": lambda args, account: device_poll(account),
    "disconnect": lambda args, account: disconnect(account),
    "browser-start": lambda args, account: browser_start(account),
    # the pasted URL/code comes on stdin (never argv: it is a live authorization code)
    "browser-finish": lambda args, account: browser_finish(sys.stdin.read(), account),
    "restore": lambda args, account: restore(),
    "add-models": cmd_add_models,
    "test": cmd_test,
    "selfcheck": cmd_selfcheck,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in COMMANDS:
        print(json.dumps({"ok": False, "error": "usage", "commands": sorted(COMMANDS)}))
        return 2
    os.umask(0o077)
    args, account = list(argv[1:]), DEFAULT_ACCOUNT
    if "--account" in args:
        i = args.index("--account")
        account = args[i + 1] if i + 1 < len(args) else ""
        del args[i:i + 2]
    if not valid_account(account):
        print(json.dumps({"ok": False, "error": "invalid_account"}))
        return 2
    try:
        result = COMMANDS[argv[0]](args, account)
    except Exception as exc:  # noqa: BLE001 — one JSON line, no traceback with request bodies
        result = {"ok": False, "error": "internal", "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
