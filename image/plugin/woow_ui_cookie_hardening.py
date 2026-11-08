"""Harden LiteLLM dashboard sessions without breaking its cookie-reading UI.

LiteLLM 1.104 writes its dashboard JWT to the ``token`` cookie without the
HttpOnly attribute and also returns it from ``POST /v2/login``.  The bundled
dashboard reads that cookie to derive its per-session proxy key, and its role,
user and expiry to build the menus.  This module keeps the real JWT HttpOnly and
gives browser JavaScript a sentinel JWT instead: the real JWT's claims with only
the key replaced (``sentinel_for``), unsigned.  Inside the proxy it maps that
sentinel back to the key carried by the real cookie before LiteLLM
authenticates it.

The proxy remains the authority: the middleware only replaces a sentinel
Authorization header with a key decoded from the cookie, then LiteLLM's normal
authentication validates it. A request with the sentinel but no valid
signed-session cookie remains unauthorized.

A dashboard page load whose session key LiteLLM's database no longer holds
(deleted, expired, blocked) gets an expired sentinel and a cleared cookie, so
the dashboard's own expiry check returns to the login page; an API call that
LiteLLM refuses for that reason reloads the page once (at most once a minute).
"""
from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import importlib.abc
import json
import sys
import threading


PROXY_TARGET = "litellm.proxy.proxy_server"
TOKEN_COOKIE = "token"
SENTINEL_KEY = "woow_ui_cookie_session"


def _log(message: str) -> None:
    print(f"woow ui-cookie: {message}", file=sys.stderr, flush=True)


def _jwt_part(value: dict) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(value, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")


SENTINEL_JWT = ".".join((
    _jwt_part({"alg": "none", "typ": "JWT"}),
    _jwt_part({"key": SENTINEL_KEY, "exp": 4102444800}),
    "x",
))


def _cookie_value(headers) -> str | None:
    """Return the real ``token`` cookie from ASGI headers, never log it. The dashboard stores the
    sentinel it got from /v2/login as its own ``token`` cookie (path /ui, sent before the real
    one on /ui pages): a sentinel copy is skipped, it never stands for a session."""
    for name, raw_value in headers or ():
        if name.lower() != b"cookie":
            continue
        try:
            values = raw_value.decode("latin-1")
        except UnicodeDecodeError:
            continue
        for item in values.split(";"):
            key, separator, value = item.strip().partition("=")
            value = value.strip('"')
            if separator and key == TOKEN_COOKIE and (_jwt_claims(value) or {}).get("key") != SENTINEL_KEY:
                return value
    return None


def _jwt_claims(token: str | None) -> dict | None:
    """The (unverified) payload of a dashboard JWT; LiteLLM still validates the key itself."""
    if not isinstance(token, str):
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, UnicodeDecodeError, binascii.Error):
        return None
    return data if isinstance(data, dict) else None


def _session_key(token: str | None) -> str | None:
    """Extract LiteLLM's key claim; LiteLLM still validates that key itself."""
    data = _jwt_claims(token)
    key = data.get("key") if data else None
    if not isinstance(key, str) or not key or len(key) > 8192:
        return None
    try:
        key.encode("ascii")
    except UnicodeEncodeError:
        return None
    return key


def sentinel_for(token: str | None, *, expired: bool = False) -> str:
    """The JWT browser JavaScript gets for a real session JWT: its claims (role, user, login
    method, expiry: what the dashboard reads to build its menus) with only the key replaced, so
    the dashboard behaves as before while the key stays HttpOnly. Without readable claims, the
    fixed SENTINEL_JWT. ``expired`` dates it in the past: the dashboard's own expiry check then
    returns to the login page."""
    claims = _jwt_claims(token)
    if claims is None and not expired:
        return SENTINEL_JWT
    claims = {**(claims or {}), "key": SENTINEL_KEY}
    if expired:
        claims["exp"] = 1
    elif not isinstance(claims.get("exp"), (int, float)):
        claims["exp"] = 4102444800
    return ".".join((_jwt_part({"alg": "none", "typ": "JWT"}), _jwt_part(claims), "x"))


def _is_sentinel(authorization: str | None) -> bool:
    """The Authorization the dashboard derives from a sentinel: the sentinel key, or a JWT whose
    key claim is it (any session's sentinel: only the real cookie supplies the key)."""
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        return False
    value = authorization[len("Bearer "):]
    return value == SENTINEL_KEY or (_jwt_claims(value) or {}).get("key") == SENTINEL_KEY


async def _session_revoked(key: str) -> bool:
    """True only when LiteLLM's database no longer holds the dashboard session key (deleted,
    expired or blocked). False when it does, and whenever that cannot be told (master key, no
    database, a lookup error): LiteLLM still authenticates every API call itself."""
    proxy = sys.modules.get(PROXY_TARGET)
    if key == getattr(proxy, "master_key", None):
        return False
    db = getattr(getattr(proxy, "prisma_client", None), "db", None)
    if db is None:
        return False
    hashed = hashlib.sha256(key.encode()).hexdigest() if key.startswith("sk-") else key  # as LiteLLM stores it
    try:
        row = await db.litellm_verificationtoken.find_unique(where={"token": hashed})
    except Exception:  # noqa: BLE001 -- never block the dashboard on a lookup failure
        return False
    if row is None:
        return True
    expires = getattr(row, "expires", None)
    if isinstance(expires, datetime.datetime):
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=datetime.timezone.utc)
        if expires <= datetime.datetime.now(datetime.timezone.utc):
            return True
    return getattr(row, "blocked", None) is True


def _authorization(headers) -> tuple[int | None, str | None]:
    for index, (name, value) in enumerate(headers or ()):
        if name.lower() != b"authorization":
            continue
        try:
            return index, value.decode("latin-1")
        except UnicodeDecodeError:
            return index, None
    return None, None


def _scope_with_session_authorization(scope):
    key = _session_key(_cookie_value(scope.get("headers")))
    if key is None:
        return scope
    index, authorization = _authorization(scope.get("headers"))
    if not _is_sentinel(authorization):
        return scope
    headers = list(scope.get("headers") or ())
    if index is not None:
        del headers[index]
    headers.append((b"authorization", ("Bearer " + key).encode("ascii")))
    return {**scope, "headers": headers}


class UiTokenAuthMiddleware:
    """Supply LiteLLM the server-only session key for its normal auth check."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            scope = _scope_with_session_authorization(scope)
        await self.app(scope, receive, send)


def _harden_set_cookie(raw_value: bytes) -> tuple[bytes, bool]:
    """Force security attributes on LiteLLM's dashboard ``token`` cookie only."""
    try:
        text = raw_value.decode("latin-1")
    except UnicodeDecodeError:
        return raw_value, False
    parts = [part.strip() for part in text.split(";")]
    if not parts or parts[0].split("=", 1)[0].strip() != TOKEN_COOKIE:
        return raw_value, False
    kept = []
    for part in parts[1:]:
        lowered = part.lower()
        if lowered in ("secure", "httponly") or lowered.startswith("samesite="):
            continue
        kept.append(part)
    hardened = "; ".join([parts[0], *kept, "Secure", "HttpOnly", "SameSite=Lax"])
    return hardened.encode("latin-1"), True


def _harden_response_cookie(response) -> bool:
    matched = False
    for index, (name, value) in enumerate(response.raw_headers):
        if name.lower() != b"set-cookie":
            continue
        hardened, changed = _harden_set_cookie(value)
        if changed:
            response.raw_headers[index] = (name, hardened)
            matched = True
    return matched


def _replace_v2_token_body(response) -> bool:
    """Keep frontend compatibility without putting the real session JWT in JSON."""
    try:
        data = json.loads(response.body)
    except (TypeError, ValueError, UnicodeDecodeError):
        return False
    if not isinstance(data, dict) or not isinstance(data.get("token"), str):
        return False
    data["token"] = sentinel_for(data["token"])
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    response.body = body
    response.headers["content-length"] = str(len(body))
    return True


def _wrap_login_endpoint(route, *, v2: bool) -> None:
    """Modify LiteLLM's response before response compression/middleware runs."""
    if getattr(route, "_woow_ui_cookie_hardened", False):
        return
    original = route.endpoint

    async def hardened_endpoint(*args, **kwargs):
        response = await original(*args, **kwargs)
        if not hasattr(response, "raw_headers"):
            return response
        token_cookie = _harden_response_cookie(response)
        if v2 and token_cookie:
            _replace_v2_token_body(response)
        return response

    route.endpoint = hardened_endpoint
    route.dependant.call = hardened_endpoint
    # FastAPI's adapter installs the dependency AsyncExitStack in request.scope.
    # Starlette's adapter omits it, causing /login and /v2/login to return 500.
    from fastapi.routing import request_response

    route.app = request_response(route.get_route_handler())
    route._woow_ui_cookie_hardened = True


_SHIM_MARK = b"woow-ui-cookie-sentinel"
# Ends a gone session in the browser: the HttpOnly cookie (path /, browser JavaScript cannot
# clear it) and the sentinel copy the dashboard stored itself at login (path /ui).
_CLEAR_TOKEN_COOKIES = (
    b"token=; Path=/; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Secure; HttpOnly; SameSite=Lax",
    b"token=; Path=/ui; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Secure; SameSite=Lax",
)


def _has_sentinel_copy(headers) -> bool:
    """A ``token`` cookie holding a sentinel: what the dashboard stores itself after /v2/login."""
    for name, raw_value in headers or ():
        if name.lower() != b"cookie":
            continue
        for item in raw_value.decode("latin-1", "replace").split(";"):
            key, separator, value = item.strip().partition("=")
            if separator and key == TOKEN_COOKIE and (_jwt_claims(value.strip('"')) or {}).get("key") == SENTINEL_KEY:
                return True
    return False


def _shim_tag(sentinel: str) -> bytes:
    # The sentinel carries the session's claims but never its key (sentinel_for); no other
    # session material is in the page. The dashboard reads its token from document.cookie and
    # from sessionStorage, so both get the same sentinel; it may clear its own cookie copies.
    # A 401 telling the session key is gone (deleted, expired) reloads the page once a minute at
    # most, and the reload's revocation check logs it out.
    script = """<script data-woow-ui-cookie-sentinel>(function(){
const synthetic="token=%s";
try{sessionStorage.setItem("token",synthetic.slice(6));}catch(error){}
const descriptor=Object.getOwnPropertyDescriptor(Document.prototype,"cookie");
if(!descriptor||!descriptor.get||!descriptor.set)return;
Object.defineProperty(Document.prototype,"cookie",{configurable:true,enumerable:descriptor.enumerable,
get:function(){const parts=String(descriptor.get.call(this)||"").split(/;\\s*/).filter(function(part){return !/^token=/i.test(part);});parts.push(synthetic);return parts.filter(Boolean).join("; ");},
set:function(value){if(/^\\s*token=/i.test(String(value))&&!/^\\s*token=\\s*(;|$)/i.test(String(value)))return String(value);return descriptor.set.call(this,value);}});
const fetchOriginal=window.fetch;
if(typeof fetchOriginal!=="function")return;
window.fetch=function(){return fetchOriginal.apply(this,arguments).then(function(response){
if(response.status===401){response.clone().text().then(function(text){
if(!/token_not_found_in_db|Invalid proxy server token|expired_key/.test(text))return;
try{const mark="woowUiSessionReload",now=Date.now();if(now-Number(sessionStorage.getItem(mark)||0)<60000)return;sessionStorage.setItem(mark,String(now));}catch(error){return;}
location.reload();}).catch(function(){});}
return response;});};
})();</script>""" % sentinel
    return script.encode("utf-8")


class _DashboardCookieInjector:
    """Inject the nonsecret compatibility cookie before dashboard JS executes."""

    woow_ui_cookie_hardening = True
    _DROP_REQUEST_HEADERS = frozenset({b"if-none-match", b"if-modified-since", b"if-range", b"range"})

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") not in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return
        token = _cookie_value(scope.get("headers"))
        key = _session_key(token)
        has_session = key is not None
        # Only the dashboard's own sentinel copy is left (the real cookie expired or was cleared):
        # the session is over, whatever the copy's expiry says.
        stale_copy = not has_session and _has_sentinel_copy(scope.get("headers"))
        revoked = False
        scope = {
            **scope,
            "headers": [(name, value) for name, value in scope.get("headers", [])
                        if name.lower() not in self._DROP_REQUEST_HEADERS],
        }
        start = None
        chunks: list[bytes] = []

        async def send_wrapper(message):
            nonlocal start, revoked
            if message["type"] == "http.response.start":
                content_type = dict(message.get("headers") or []).get(b"content-type", b"")
                if message.get("status") == 200 and content_type.startswith(b"text/html"):
                    start = message
                    # One lookup per dashboard page load (not per asset).
                    revoked = has_session and await _session_revoked(key)
                    return
                await send(message)
                return
            if start is None or message["type"] != "http.response.body":
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = b"".join(chunks)
            ended = revoked or stale_copy
            if (has_session or ended) and _SHIM_MARK not in body:
                tag = _shim_tag(sentinel_for(token, expired=ended))
                body = body.replace(b"<head>", b"<head>" + tag, 1) if b"<head>" in body else tag + body
            dropped = {b"content-length", b"etag", b"last-modified", b"cache-control"}
            headers = [(name, value) for name, value in start.get("headers", []) if name.lower() not in dropped]
            # The page now carries this session's claims: never kept in a shared cache.
            headers += [(b"content-length", str(len(body)).encode()), (b"cache-control", b"private, no-store")]
            if ended:
                headers += [(b"set-cookie", value) for value in _CLEAR_TOKEN_COOKIES]
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, send_wrapper)


def _install_dashboard_injector(app) -> bool:
    from starlette.routing import Mount

    for route in app.router.routes:
        if isinstance(route, Mount) and route.path == "/ui":
            if not getattr(route.app, "woow_ui_cookie_hardening", False):
                route.app = _DashboardCookieInjector(route.app)
            return True
    return False


def install_on(module) -> bool:
    """Install the middleware, both login-route wrappers and dashboard shim once."""
    app = getattr(module, "app", None)
    if app is None:
        _log("WARNING proxy_server has no app; hardening NOT installed")
        return False
    if getattr(app.state, "woow_ui_cookie_hardening_installed", False):
        return True
    try:
        from fastapi.routing import APIRoute

        routes = [route for route in app.router.routes if isinstance(route, APIRoute)]
        legacy = [route for route in routes if route.path == "/login" and "POST" in (route.methods or set())]
        v2 = [route for route in routes if route.path == "/v2/login" and "POST" in (route.methods or set())]
        for route in legacy:
            _wrap_login_endpoint(route, v2=False)
        for route in v2:
            _wrap_login_endpoint(route, v2=True)
        if not v2:
            _log("WARNING /v2/login route missing; JSON token response was not hardened")
        if not _install_dashboard_injector(app):
            _log("WARNING dashboard /ui mount missing; sentinel UI shim was not installed")
        app.add_middleware(UiTokenAuthMiddleware)
        app.state.woow_ui_cookie_hardening_installed = True
        _log("dashboard token cookie is HttpOnly; UI receives a sentinel without the session key")
        return True
    except Exception as exc:  # noqa: BLE001 -- never prevent proxy startup
        _log(f"ERROR hardening NOT installed: {type(exc).__name__}")
        return False


_GUARD = threading.local()


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname != PROXY_TARGET or getattr(_GUARD, "busy", False):
            return None
        _GUARD.busy = True
        try:
            try:
                rest = sys.meta_path[sys.meta_path.index(self) + 1:]
            except ValueError:
                return None
            for finder in rest:
                if not hasattr(finder, "find_spec"):
                    continue
                spec = finder.find_spec(fullname, path, target)
                if spec is not None and spec.loader is not None:
                    break
            else:
                return None
        finally:
            _GUARD.busy = False
        original_exec = spec.loader.exec_module

        def exec_module(module):
            original_exec(module)
            try:
                install_on(module)
            except Exception as exc:  # noqa: BLE001 -- never prevent proxy startup
                _log(f"ERROR hardening NOT installed: {type(exc).__name__}")

        spec.loader.exec_module = exec_module
        return spec


def install_import_hook() -> None:
    if PROXY_TARGET in sys.modules:
        install_on(sys.modules[PROXY_TARGET])
        return
    if not any(isinstance(finder, _Finder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _Finder())
