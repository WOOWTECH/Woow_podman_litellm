"""ChatGPT subscriptions for LiteLLM — sign-in page and API served by the LiteLLM proxy itself.

Several ChatGPT accounts per LiteLLM (see woow_chatgpt.py): the ``default`` account, plus one
per LiteLLM credential of provider "ChatGPT Subscription" (its ``api_key`` is the reference
``woow-chatgpt:<account>``). Same-named models on several accounts form one model group.

Adds to the proxy (same origin as /ui, same admin login):
  GET  /woow/chatgpt                     the page (accounts, sign-in, models)
  GET  /woow/chatgpt/ui-bridge.js        loaded by the dashboard (/ui): LLM Credentials ↔ accounts
  GET  /woow/chatgpt/api/accounts        every account: credentials, sign-in, models in LiteLLM
  POST /woow/chatgpt/api/accounts/resolve {"name"}: the account a credential name maps to
  POST /woow/chatgpt/api/accounts/bind   {"name"}: create the credential for a signed-in account
  POST /woow/chatgpt/api/accounts/remove {"account"}: its models, credentials and sign-in
  GET  /woow/chatgpt/api/status?account=
  POST /woow/chatgpt/api/start | poll    device-code login            (body {"account"})
  POST /woow/chatgpt/api/browser/start   browser login (PKCE)         (body {"account"})
  POST /woow/chatgpt/api/browser/finish  {"account", "input": <the localhost redirect URL or code>}
  POST /woow/chatgpt/api/disconnect      forget a sign-in (this LiteLLM only)
  GET  /woow/chatgpt/api/models?account= the account's models (live from ChatGPT) + what is added
  POST /woow/chatgpt/api/models          {"action": "add"|"remove", "account", "models": [...]|"visible"|"all",
                                           "naming": "shared"|"prefixed", "credential"?}
  POST /woow/chatgpt/api/test            {"account", "model"}: one tiny request on that account
Every /api route needs a LiteLLM proxy-admin key (the dashboard's session key from its `token`
cookie, the master key, or an admin virtual key) in `Authorization: Bearer …`.

Two ways in:
  * any LiteLLM (podman, Docker, a Home Assistant add-on, …): put woow_chatgpt.py and this file
    next to config.yaml and add
        litellm_settings:
          callbacks: ["woow_chatgpt_plugin.proxy_handler_instance"]
    The sign-ins are kept in LiteLLM's database when DATABASE_URL is set, otherwise in
    CHATGPT_TOKEN_DIR (default ~/.config/litellm/chatgpt) — make that directory persistent.
  * the WOOW PaaS chart: sitecustomize installs it when litellm.proxy.proxy_server is imported
    (no config key, so a callbacks list set in the admin UI is never overridden).
"""

# No `from __future__ import annotations`: FastAPI resolves route annotations (Request,
# dict) at runtime from this module, and they are imported inside install().
import asyncio
import os
import re
import sys
import threading
import time
from urllib.parse import quote

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import woow_chatgpt as core  # noqa: E402

PREFIX = "/woow/chatgpt"
CHATGPT_PROVIDER = "CHATGPT"  # what the dashboard stores in credential_info.custom_llm_provider
_COST_SYNC_LOCK = threading.Lock()


# ── helpers ───────────────────────────────────────────────────────────────────
def _self_base(request) -> str:
    """This proxy on loopback (model / credential writes go through LiteLLM's own endpoints, with
    the caller's key, so LiteLLM's permission checks and DB writes apply unchanged)."""
    port = os.environ.get("WOOW_PROXY_PORT")
    if not port:
        server = request.scope.get("server") or ("127.0.0.1", 4000)
        port = server[1] or 4000
    return f"http://127.0.0.1:{port}"


def _auth_headers(request) -> dict:
    value = request.headers.get("authorization")
    return {"Authorization": value} if value else {}


async def _self_call(request, method: str, path: str, body: dict | None = None, timeout: float = 30.0):
    import httpx

    async with httpx.AsyncClient(base_url=_self_base(request), timeout=timeout) as client:
        return await client.request(method, path, json=body, headers=_auth_headers(request))


def _credential_accounts() -> dict[str, str]:
    """{credential name: account} for every ChatGPT credential (core.credential_accounts)."""
    return core.credential_accounts()


def _find_name(name: str, names) -> str | None:
    """The stored credential name matching `name` exactly, else ignoring surrounding whitespace
    (a phone keyboard's trailing space: the dashboard cannot even delete such a credential, the
    browser's URL parser strips the space from /credentials/<name>)."""
    if name in names:
        return name
    for n in names:
        if n.strip() == name.strip():
            return n
    return None


def _all_credential_names() -> set[str]:
    import litellm

    return {c.credential_name for c in list(getattr(litellm, "credential_list", None) or [])}


def _deployments() -> list[dict]:
    """chatgpt/ deployments in the running router: {id, model_name, model, account, credential}."""
    try:
        from litellm.proxy import proxy_server
    except Exception:  # noqa: BLE001
        return []
    router = getattr(proxy_server, "llm_router", None)
    if router is None:
        return []
    creds = _credential_accounts()
    out = []
    for d in list(getattr(router, "model_list", None) or []):
        params = d.get("litellm_params") or {}
        model = str(params.get("model") or "")
        if not model.startswith("chatgpt/"):
            continue
        credential = params.get("litellm_credential_name")
        account = core.deployment_account(params, creds)
        out.append(
            {
                "id": (d.get("model_info") or {}).get("id"),
                "model_name": d.get("model_name"),
                "model": model,
                "account": account,
                "credential": credential,
            }
        )
    return out


def _account_of(params_or_query, default=core.DEFAULT_ACCOUNT) -> str:
    account = params_or_query.get("account") if params_or_query else None
    return account or default


def _prefix_for(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:40] or "chatgpt"


def sync_model_costs(account: str) -> list[str]:
    """Fetch an account's models and teach LiteLLM the ones its bundled map lacks."""
    with _COST_SYNC_LOCK:
        return core.register_model_costs(core.remote_models(account))


def _sync_accounts(accounts) -> None:
    for acct in accounts:
        if not core._has_tokens(core.account_record(acct)):
            continue
        try:
            added = sync_model_costs(acct)
            if added:
                core._log(f"taught LiteLLM {len(added)} ChatGPT model(s) its model map lacks: {', '.join(added)}")
        except Exception as exc:  # noqa: BLE001
            core._log(f"WARNING could not read the ChatGPT model list ({acct}): {type(exc).__name__}: {str(exc)[:200]}")


def _sync_model_costs_later(delay: float, account: str | None = None) -> None:
    def run():
        time.sleep(delay)
        _sync_accounts([account] if account else core.list_accounts())

    threading.Thread(target=run, name="woow-chatgpt-models", daemon=True).start()


def restore_then_sync() -> dict | None:
    """Proxy start (R14): first make this process use the newest durable sign-in of every account
    (core.restore — a newer database copy wins even when the token file cannot be written), then
    read the model lists. Reading a model list may refresh the token, so an account whose restore
    failed — or every account, when the database could not be read — is NOT synced: that could
    send an already-rotated refresh token to OpenAI and get the account's tokens revoked."""
    try:
        result = core.restore()
    except Exception as exc:  # noqa: BLE001
        core._log(f"WARNING ChatGPT sign-in check at proxy start failed ({type(exc).__name__}); model lists not read")
        return None
    if result.get("error") == "db_load_failed":
        core._log("WARNING ChatGPT sign-ins could not be read from the database at proxy start; model lists not read")
        return result
    skip = {f.get("account") for f in result.get("failed") or [] if f.get("error") == "restore_failed"}
    _sync_accounts([a for a in core.list_accounts() if a not in skip])
    return result


def _restore_then_sync_later(delay: float) -> None:
    def run():
        time.sleep(delay)
        restore_then_sync()

    threading.Thread(target=run, name="woow-chatgpt-models", daemon=True).start()


def accounts_view() -> list[dict]:
    creds = _credential_accounts()
    deployments = _deployments()
    names = list(dict.fromkeys(core.list_accounts() + sorted(set(creds.values()))))
    out = []
    for account in names:
        bound = sorted(n for n, a in creds.items() if a == account)
        label = "預設帳號" if account == core.DEFAULT_ACCOUNT else (bound[0] if bound else account)
        out.append(
            {
                "account": account,
                "label": label,
                "credentials": bound,
                **core.describe(core.account_record(account)),
                "models": sorted({d["model_name"] for d in deployments if d["account"] == account}),
            }
        )
    return out


# ── install ───────────────────────────────────────────────────────────────────
def install(app) -> None:
    if getattr(app.state, "woow_chatgpt_installed", False):
        return
    from fastapi import APIRouter, Body, Depends, HTTPException, Request
    from fastapi.responses import HTMLResponse
    from litellm.proxy.auth.user_api_key_auth import user_api_key_auth

    async def admin(user_api_key_dict=Depends(user_api_key_auth)):
        role = getattr(user_api_key_dict, "user_role", None)
        if str(getattr(role, "value", role)) != "proxy_admin":
            raise HTTPException(status_code=403, detail="ChatGPT subscription needs a LiteLLM proxy admin")
        return user_api_key_dict

    def account_param(value) -> str:
        account = value or core.DEFAULT_ACCOUNT
        if not core.valid_account(account):
            raise HTTPException(status_code=400, detail="account")
        return account

    router = APIRouter(prefix=PREFIX, include_in_schema=False)

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    async def page():
        # SAMEORIGIN: the LiteLLM dashboard embeds this page (ui-bridge.js); nobody else may.
        return HTMLResponse(
            PAGE_HTML,
            headers={
                "Cache-Control": "no-store",
                "X-Frame-Options": "SAMEORIGIN",
                "Content-Security-Policy": "frame-ancestors 'self'",
            },
        )

    @router.get("/ui-bridge.js")
    async def ui_bridge():
        from fastapi.responses import Response

        return Response(BRIDGE_JS, media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    @router.get("/api/accounts")
    async def accounts(_=Depends(admin)):
        return {"accounts": await asyncio.to_thread(accounts_view)}

    @router.post("/api/accounts/resolve")
    async def accounts_resolve(body: dict = Body(...), _=Depends(admin)):
        name = body.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise HTTPException(status_code=400, detail="name")
        creds = _credential_accounts()
        found = _find_name(name, creds)
        if found:
            name, account = found, creds[found]
        elif _find_name(name, _all_credential_names()):
            raise HTTPException(status_code=409, detail="這個名稱已被其他（非 ChatGPT）憑證使用")
        else:
            name = name.strip()
            account = core.account_for_name(name)
        return {"name": name, "account": account, "credential_exists": bool(found), **core.describe(core.account_record(account))}

    @router.post("/api/accounts/bind")
    async def accounts_bind(request: Request, body: dict = Body(...), _=Depends(admin)):
        """Create the LiteLLM credential for a signed-in account (the page's 「新增帳號」)."""
        name = body.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise HTTPException(status_code=400, detail="name")
        creds = _credential_accounts()
        found = _find_name(name, creds)
        if found:
            return {"name": found, "account": creds[found], "created": False}
        if _find_name(name, _all_credential_names()):
            raise HTTPException(status_code=409, detail="這個名稱已被其他（非 ChatGPT）憑證使用")
        name = name.strip()
        account = core.account_for_name(name)
        r = await _self_call(
            request,
            "POST",
            "/credentials",
            {
                "credential_name": name,
                "credential_values": {"api_key": core.account_ref(account), "api_base": core.CHATGPT_API_BASE},
                "credential_info": {"custom_llm_provider": CHATGPT_PROVIDER},
            },
        )
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=f"建立憑證失敗：{r.text[:200]}")
        return {"name": name, "account": account, "created": True}

    @router.post("/api/accounts/remove")
    async def accounts_remove(request: Request, body: dict = Body(...), _=Depends(admin)):
        """Remove an account: its models, its credentials and its sign-in. The default account
        keeps its models (only the sign-in is forgotten)."""
        import httpx

        account = account_param(body.get("account"))
        removed_models, removed_credentials, failed = [], [], []

        def incomplete():
            return {"ok": False, "account": account, "state": "error", "error": "dependent_delete_failed",
                    "removed_models": removed_models, "removed_credentials": removed_credentials, "failed": failed}
        if account != core.DEFAULT_ACCOUNT:
            for d in _deployments():
                if d["account"] != account:
                    continue
                if not d["id"]:
                    failed.append(d["model_name"])
                    continue
                try:
                    r = await _self_call(request, "POST", "/model/delete", {"id": d["id"]})
                except httpx.HTTPError:
                    failed.append(d["model_name"])
                else:
                    (removed_models if r.status_code == 200 else failed).append(d["model_name"])
            if failed:
                return incomplete()
            for name, acct in _credential_accounts().items():
                if acct == account:
                    try:
                        r = await _self_call(request, "DELETE", f"/credentials/{quote(name, safe='')}")
                    except httpx.HTTPError:
                        failed.append(name)
                    else:
                        (removed_credentials if r.status_code == 200 else failed).append(name)
            if failed:
                return incomplete()
        out = await asyncio.to_thread(core.disconnect, account)
        return {**out, "removed_models": removed_models, "removed_credentials": removed_credentials, "failed": failed}

    @router.get("/api/status")
    async def status(account: str = "", _=Depends(admin)):
        account = account_param(account)
        out = await asyncio.to_thread(core.sign_in_state, account)
        out["credentials"] = sorted(n for n, a in _credential_accounts().items() if a == account)
        out["models"] = [d for d in _deployments() if d["account"] == account]
        return out

    @router.post("/api/start")
    async def start(body: dict = Body(default={}), _=Depends(admin)):
        return await asyncio.to_thread(core.device_start, account_param(body.get("account")))

    @router.post("/api/poll")
    async def poll(body: dict = Body(default={}), _=Depends(admin)):
        account = account_param(body.get("account"))
        out = await asyncio.to_thread(core.device_poll, account)
        if out.get("state") == "connected":
            _sync_model_costs_later(0, account)
        return out

    @router.post("/api/browser/start")
    async def browser_start(body: dict = Body(default={}), _=Depends(admin)):
        return await asyncio.to_thread(core.browser_start, account_param(body.get("account")))

    @router.post("/api/browser/finish")
    async def browser_finish(body: dict = Body(...), _=Depends(admin)):
        account = account_param(body.get("account"))
        text = body.get("input")
        if not isinstance(text, str) or len(text) > 4096:
            raise HTTPException(status_code=400, detail="input")
        out = await asyncio.to_thread(core.browser_finish, text, account)
        if out.get("state") == "connected":
            _sync_model_costs_later(0, account)
        return out

    @router.post("/api/disconnect")
    async def disconnect(body: dict = Body(default={}), _=Depends(admin)):
        return await asyncio.to_thread(core.disconnect, account_param(body.get("account")))

    @router.get("/api/models")
    async def models(account: str = "", _=Depends(admin)):
        account = account_param(account)
        added = [d for d in _deployments() if d["account"] == account]
        have = {d["model"] for d in added}
        if not core._has_tokens(core.account_record(account)):
            return {"account": account, "connected": False, "models": [], "added": added}
        try:
            remote = await asyncio.to_thread(core.remote_models, account)
        except Exception as exc:  # noqa: BLE001
            return {"account": account, "connected": True, "error": f"{type(exc).__name__}: {str(exc)[:200]}", "models": [], "added": added}
        await asyncio.to_thread(core.register_model_costs, remote)
        for m in remote:
            m["added"] = m["model"] in have
        return {"account": account, "connected": True, "models": remote, "added": added}

    @router.post("/api/models")
    async def change_models(request: Request, body: dict = Body(...), _=Depends(admin)):
        account = account_param(body.get("account"))
        action = body.get("action")
        wanted = body.get("models")
        if action not in ("add", "remove"):
            raise HTTPException(status_code=400, detail="action must be add or remove")
        mine = [d for d in _deployments() if d["account"] == account]
        if action == "remove":
            if not isinstance(wanted, list) or not all(isinstance(n, str) for n in wanted):
                raise HTTPException(status_code=400, detail="models must be a list of names")
            aliases = {d["model_name"] for d in mine}
            exact_names = {n for n in wanted if n in aliases}
            # The model picker also sends backend ids for prefixed deployments. Keep
            # that behavior only when the requested name is not an exact alias.
            backend_names = {_as_model(n) for n in wanted if n not in aliases}
            removed, failed = [], []
            for d in mine:
                if (d["model_name"] in exact_names or d["model"] in backend_names) and d["id"]:
                    r = await _self_call(request, "POST", "/model/delete", {"id": d["id"]})
                    (removed if r.status_code == 200 else failed).append(d["model_name"])
            return {"removed": removed, "failed": failed}
        if not core._has_tokens(core.account_record(account)):
            raise HTTPException(status_code=409, detail="not_connected")
        try:
            remote = await asyncio.to_thread(core.remote_models, account)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"could not read the ChatGPT model list: {type(exc).__name__}")
        await asyncio.to_thread(core.register_model_costs, remote)
        by_model = {m["model"]: m for m in remote}
        if wanted in ("visible", "all"):
            targets = [m["model"] for m in remote if wanted == "all" or m["visible"]]
        elif isinstance(wanted, list):
            targets = [_as_model(n) for n in wanted if isinstance(n, str) and _valid_name(n)]
        else:
            raise HTTPException(status_code=400, detail='models must be a list, "visible" or "all"')
        creds = sorted(n for n, a in _credential_accounts().items() if a == account)
        credential = body.get("credential") if body.get("credential") in creds else (creds[0] if creds else None)
        naming = body.get("naming") if body.get("naming") in ("shared", "prefixed") else "shared"
        prefix = _prefix_for(credential or account)
        have = {(d["model_name"], d["model"]) for d in mine}
        added, kept, failed = [], [], []
        for model in dict.fromkeys(targets):
            model_name = model if naming == "shared" else f"{prefix}/{model.split('/', 1)[1]}"
            if (model_name, model) in have:
                kept.append(model_name)
                continue
            params = {"model": model}
            if account != core.DEFAULT_ACCOUNT:
                params["api_key"] = core.account_ref(account)
                if credential:
                    params["litellm_credential_name"] = credential
            info = by_model.get(model, {})
            r = await _self_call(
                request,
                "POST",
                "/model/new",
                {
                    "model_name": model_name,
                    "litellm_params": params,
                    "model_info": {
                        "mode": "responses",
                        "description": f"ChatGPT subscription（{credential or ('預設帳號' if account == core.DEFAULT_ACCOUNT else account)}）— {info.get('display_name') or model}",
                        **({"max_input_tokens": info["context_window"]} if isinstance(info.get("context_window"), int) else {}),
                    },
                },
                timeout=60.0,
            )
            (added if r.status_code == 200 else failed).append(model_name)
        return {"account": account, "added": added, "kept": kept, "failed": failed}

    @router.post("/api/test")
    async def test(body: dict = Body(...), _=Depends(admin)):
        """One tiny request on exactly this account (not through a model group, which may pick
        another account)."""
        import litellm

        account = account_param(body.get("account"))
        model = body.get("model")
        if not isinstance(model, str) or not _valid_name(model):
            raise HTTPException(status_code=400, detail="model")
        model = _as_model(model)
        await asyncio.to_thread(core.register_model_costs, [{"model": model}])
        started = time.time()
        try:
            resp = await litellm.acompletion(
                model=model,
                messages=[{"role": "user", "content": "Reply with the single word: OK"}],
                api_key=core.account_ref(account),
                timeout=120,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "account": account, "model": model, "seconds": round(time.time() - started, 2),
                    "status_code": getattr(exc, "status_code", None), "error": str(getattr(exc, "message", None) or exc)[:300]}
        text = ((resp.choices[0].message.content if resp.choices else "") or "").strip()
        return {"ok": True, "account": account, "model": model, "seconds": round(time.time() - started, 2), "reply": text[:40]}

    app.include_router(router)
    if os.environ.get("WOOW_CHATGPT_UI_BRIDGE", "true").lower() != "false":
        _inject_bridge_into_dashboard(app)
    app.state.woow_chatgpt_installed = True
    core._log(f"ChatGPT subscription page at {PREFIX} (multiple accounts)")
    _restore_then_sync_later(5)


# ── LiteLLM dashboard bridge ──────────────────────────────────────────────────
BRIDGE_MARK = f"{PREFIX}/ui-bridge.js".encode()
_SAFE_PREFIX = re.compile(r"(/[A-Za-z0-9._~-]+)*")


def external_prefix(scope, mount_path: str = "") -> str:
    """The path prefix the browser sees in front of this proxy: Home Assistant Ingress sends
    X-Ingress-Path (/api/hassio_ingress/<token>); a LiteLLM SERVER_ROOT_PATH shows up as the ASGI
    root_path; on PaaS both are empty. Inside a Starlette Mount the root_path also carries the
    mount's own path (/ui) — that part is not a prefix and is cut off. Anything not shaped like a
    plain path is ignored."""
    headers = dict(scope.get("headers") or [])
    root_path = (scope.get("root_path") or "").rstrip("/")
    if mount_path and root_path.endswith(mount_path):
        root_path = root_path[: -len(mount_path)]
    for value in (headers.get(b"x-ingress-path", b"").decode("latin-1"), root_path):
        value = value.rstrip("/")
        if value and _SAFE_PREFIX.fullmatch(value):
            return value
    return ""


def bridge_tag(scope) -> bytes:
    """Called inside the dashboard's /ui mount."""
    return f'<script src="{external_prefix(scope, "/ui")}{PREFIX}/ui-bridge.js" defer></script>'.encode()


class _DashboardScriptInjector:
    """Wraps the dashboard's StaticFiles mount (/ui): every full HTML page it serves gets one
    <script> tag for ui-bridge.js, which ties "LLM Credentials → Add Credential → ChatGPT
    Subscription" to the sign-in. ETag/Last-Modified are dropped so a browser never revalidates
    into a cached copy without the tag — and the request's conditional / range headers are
    dropped too: a browser that cached a dashboard page BEFORE this plugin existed keeps that
    page's ETag and sends If-None-Match / If-Modified-Since; StaticFiles would answer 304 and the
    browser would keep showing the old page without the bridge forever (seen in production
    10-09: no ChatGPT panel, "Failed to add credential")."""

    woow_bridge = True

    def __init__(self, app):
        self.app = app

    _DROP_REQUEST_HEADERS = frozenset({b"if-none-match", b"if-modified-since", b"if-range", b"range"})

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") not in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return
        scope = {
            **scope,
            "headers": [(k, v) for k, v in scope.get("headers", []) if k.lower() not in self._DROP_REQUEST_HEADERS],
        }
        start = None
        chunks: list[bytes] = []

        async def send_wrapper(message):
            nonlocal start
            if message["type"] == "http.response.start":
                content_type = dict(message.get("headers") or []).get(b"content-type", b"")
                if message.get("status") == 200 and content_type.startswith(b"text/html"):
                    start = message
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
            if BRIDGE_MARK not in body:
                tag = bridge_tag(scope)
                if b"</head>" in body:
                    body = body.replace(b"</head>", tag + b"</head>", 1)
                else:
                    body += tag
            drop = {b"content-length", b"etag", b"last-modified", b"cache-control"}
            headers = [(k, v) for k, v in start.get("headers", []) if k.lower() not in drop]
            # Keep a stricter policy set inside (the cookie hardening's per-session page is never stored).
            inner = next((v for k, v in start.get("headers", []) if k.lower() == b"cache-control"), b"")
            headers += [(b"content-length", str(len(body)).encode()),
                        (b"cache-control", inner if b"no-store" in inner else b"no-cache")]
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, send_wrapper)


def _inject_bridge_into_dashboard(app) -> None:
    from starlette.routing import Mount

    for route in app.router.routes:
        if isinstance(route, Mount) and route.path == "/ui" and not getattr(route.app, "woow_bridge", False):
            route.app = _DashboardScriptInjector(route.app)
            core._log("dashboard: Add Credential → ChatGPT Subscription now opens the sign-in")
            return
    core._log("WARNING dashboard mount /ui not found — ui-bridge.js not injected (the /woow/chatgpt page still works)")


def _valid_name(name: str) -> bool:
    return bool(re.fullmatch(r"(chatgpt/)?[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name))


def _as_model(name: str) -> str:
    return name if name.startswith("chatgpt/") else f"chatgpt/{name}"


PAGE_HTML = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ChatGPT 訂閱 · LiteLLM</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1f2328;--muted:#5d6670;--line:#e3e6ea;--accent:#2563eb;--accent-fg:#fff;--ok:#15803d;--warn:#b45309;--err:#b91c1c;--chip:#eef2ff;--code:#f1f5f9;--sel:#eff6ff}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--fg:#e6e8eb;--muted:#9aa3ad;--line:#2a2f38;--accent:#5b8cff;--accent-fg:#0b1020;--ok:#4ade80;--warn:#fbbf24;--err:#f87171;--chip:#1e2640;--code:#1d222b;--sel:#16213a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Segoe UI","Noto Sans TC","PingFang TC",sans-serif}
main{max-width:920px;margin:0 auto;padding:24px 16px 48px}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:16px}
h1{font-size:22px;margin:0}h2{font-size:17px;margin:0 0 12px}
a{color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin:14px 0}
.muted{color:var(--muted)}.small{font-size:13px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:7px 14px;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}
button.danger{color:var(--err)}button:disabled{opacity:.5;cursor:not-allowed}
input[type=password],input[type=text]{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);min-width:0;flex:1}
dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 16px;margin:0}dt{color:var(--muted)}dd{margin:0;word-break:break-all}
.badge{display:inline-block;padding:1px 9px;border-radius:999px;font-size:12px;border:1px solid currentColor;white-space:nowrap}
.ok{color:var(--ok)}.warn{color:var(--warn)}.err{color:var(--err)}
.code{font:600 30px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.08em;background:var(--code);border-radius:10px;padding:12px 18px;display:inline-block;margin:8px 0}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:13px}td code{background:var(--code);padding:1px 6px;border-radius:5px;font-size:13px}
.scroll{overflow-x:auto}
#msg{min-height:1.4em}
.hidden{display:none}
.acct{display:flex;justify-content:space-between;align-items:center;gap:10px;width:100%;text-align:left;padding:10px 12px;margin:6px 0;border-radius:10px}
.acct.sel{border-color:var(--accent);background:var(--sel)}
.acct .who{display:flex;flex-direction:column;min-width:0}.acct .who b{font-weight:600}.acct .who span{overflow:hidden;text-overflow:ellipsis}
body.embed main{padding:12px 12px 24px}body.embed header{display:none}body.embed .card{margin:10px 0;padding:14px}
#doneBar{position:sticky;bottom:0;background:var(--bg);padding:10px 0;border-top:1px solid var(--line);margin-top:12px}
</style>
</head>
<body>
<main>
<header>
  <div><h1>ChatGPT 訂閱</h1><div class="muted small">用一或多個 ChatGPT Plus／Pro 帳號的額度當作 LiteLLM 的 <code>chatgpt/</code> 模型</div></div>
  <a href="../../ui" id="backToUi" class="small">← 回 LiteLLM 後台</a>
</header>

<section class="card" id="authCard">
  <h2>管理員身分</h2>
  <p id="authInfo" class="muted small">檢查中…</p>
  <div class="row hidden" id="keyRow">
    <input type="password" id="keyInput" placeholder="LiteLLM master key 或管理員金鑰" autocomplete="off">
    <button id="keySave" class="primary">使用</button>
  </div>
</section>

<section class="card hidden" id="accountsCard">
  <h2>ChatGPT 帳號</h2>
  <p class="muted small">每個帳號對應 LiteLLM 的一個「ChatGPT Subscription」憑證（LLM Credentials）。多個帳號加入同名模型時，LiteLLM 會在帳號間自動分流，某個帳號額度用完會改走其他帳號。</p>
  <div id="accountList"></div>
  <div class="row" style="margin-top:8px"><button id="btnNew">＋ 新增 ChatGPT 帳號</button></div>
  <div id="newBox" class="hidden" style="margin-top:10px">
    <div class="row"><input type="text" id="newName" placeholder="名稱（會成為 LiteLLM 的憑證名稱，例如 chatgpt-pro-2）" maxlength="64" autocomplete="off"><button id="btnNewGo" class="primary">下一步：登入</button><button id="btnNewCancel">取消</button></div>
  </div>
</section>

<section class="card hidden" id="statusCard">
  <h2><span id="acctTitle"></span> <span id="stateBadge"></span></h2>
  <dl id="statusList"></dl>
  <div class="row" style="margin-top:14px">
    <button id="btnConnect" class="primary">連接 ChatGPT</button>
    <button id="btnDisconnect" class="danger hidden">中斷連接</button>
    <button id="btnRemoveAcct" class="danger hidden">移除這個帳號</button>
  </div>
</section>

<section class="card hidden" id="loginCard">
  <h2>登入 ChatGPT</h2>
  <div id="consent">
    <ul class="small">
      <li>需要 ChatGPT Plus 或 Pro。</li>
      <li>這是借用 Codex 的登入方式，不是 OpenAI 官方的第三方整合；用量算在該帳號的訂閱，帳號風險自行承擔。</li>
      <li>同一個 ChatGPT 帳號可以同時登入多個 LiteLLM，但每個 LiteLLM 要各自登入，不要把一台的登入檔複製到另一台（同一組 refresh token 只能用一次）；同一台 LiteLLM 內不要用兩個憑證連同一個帳號。訂閱額度由所有使用該帳號的地方共用。</li>
      <li>登入網址與代碼只能自己使用。</li>
    </ul>
    <label class="row small"><input type="checkbox" id="agree"> 我了解並同意</label>
    <p class="small" style="margin:12px 0 6px">選擇登入方式：</p>
    <div class="row">
      <button id="btnBrowser" class="primary" disabled>瀏覽器登入（建議）</button>
      <button id="btnStart" disabled>裝置代碼登入</button>
    </div>
    <p class="muted small">瀏覽器登入：在新分頁登入 ChatGPT，再把跳轉後的網址貼回來，不用改 ChatGPT 設定。<br>裝置代碼登入：輸入一組代碼即可，但要先在 ChatGPT「設定 → 安全性」開啟 Codex 的裝置代碼登入。</p>
  </div>
  <div id="browserBox" class="hidden">
    <ol class="small">
      <li><a id="authLink" target="_blank" rel="noopener noreferrer"><b>開啟 OpenAI 登入頁</b></a>（新分頁），登入<b>要連的那個</b> ChatGPT 帳號並按「繼續」。</li>
      <li>登入後瀏覽器會跳到 <code>http://localhost:1455/auth/callback?code=…</code>，顯示「無法連上這個網站」——這是正常的。</li>
      <li>複製網址列的<b>整段網址</b>，貼到下面，按「完成連結」。</li>
    </ol>
    <p class="muted small">要連第二個帳號時，若 OpenAI 直接用了已登入的帳號，請先在那個分頁登出 ChatGPT（或用無痕視窗）再開登入頁。</p>
    <div class="row"><input type="text" id="authInput" placeholder="http://localhost:1455/auth/callback?code=…" autocomplete="off" spellcheck="false"><button id="btnFinish" class="primary">完成連結</button></div>
    <p class="small"><a href="#" class="backToMethods">改用其他方式</a></p>
  </div>
  <div id="codeBox" class="hidden">
    <p>打開 <a id="verifyLink" target="_blank" rel="noopener noreferrer"></a>，輸入這組代碼並同意：</p>
    <div class="row"><span class="code" id="userCode"></span><button id="btnCopy">複製</button></div>
    <p class="muted small">等你登入中… <span id="countdown"></span>（這頁會自動偵測，不用重新整理）</p>
    <p class="small"><a href="#" class="backToMethods">改用其他方式</a></p>
  </div>
</section>

<section class="card hidden" id="modelsCard">
  <h2>模型</h2>
  <p class="muted small">清單直接向 ChatGPT 讀取，就是這個帳號現在能用的模型。</p>
  <div id="namingBox" class="small hidden" style="margin-bottom:8px">
    <b>模型名稱：</b>
    <label><input type="radio" name="naming" value="shared" checked> 共用名稱 <code>chatgpt/…</code>（和其他帳號的同名模型自動分流）</label>
    <label><input type="radio" name="naming" value="prefixed"> 加上前綴 <code id="prefixHint"></code>（只走這個帳號）</label>
  </div>
  <div class="row small" style="margin-bottom:8px">
    <label class="row"><input type="checkbox" id="showHidden"> 顯示隱藏模型</label>
    <button id="btnAll">全選</button><button id="btnNone">全不選</button>
    <button id="btnAdd" class="primary">加入選取</button><button id="btnRemove" class="danger">移除選取</button>
    <button id="btnReload">重新讀取</button>
  </div>
  <div class="scroll"><table><thead><tr><th></th><th>模型</th><th>說明</th><th>這個帳號</th><th></th></tr></thead><tbody id="modelRows"></tbody></table></div>
</section>

<p id="msg" class="small"></p>
<div id="doneBar" class="row hidden"><span class="muted small">完成後按「完成」回到 LiteLLM。</span><button id="btnDone" class="primary">完成</button></div>
</main>
<script>
(function(){
"use strict";
const $=id=>document.getElementById(id);
// Everything relative to where this page is served: "" on PaaS, /api/hassio_ingress/<token> in a
// Home Assistant sidebar (Ingress), or a LiteLLM SERVER_ROOT_PATH.
const BASE=location.pathname.replace(/\/woow\/chatgpt\/?$/,"");
const API=BASE+"/woow/chatgpt/api";
document.getElementById("backToUi").href=BASE+"/ui";
const QS=new URLSearchParams(location.search);
const EMBED=QS.has("embed") && window.parent!==window;
let key=null, pollTimer=null, countdownTimer=null, remote=[], accounts=[], cur=QS.get("account")||"default", curState=null, pendingName=null;
if(EMBED){ document.body.classList.add("embed"); $("doneBar").classList.remove("hidden"); }
function notify(type){ if(!EMBED) return; try{ window.parent.postMessage({source:"woow-chatgpt",type:type||"state",account:cur},location.origin); }catch(e){} }

function cookieKey(){
  const m=document.cookie.match(/(?:^|;\s*)token=([^;]+)/); if(!m) return null;
  try{ let p=m[1].split(".")[1].replace(/-/g,"+").replace(/_/g,"/"); while(p.length%4) p+="=";
       const j=JSON.parse(atob(p)); if(j.exp && j.exp*1000<Date.now()) return null; return j.key||null; }catch(e){ return null; }
}
// A key typed in here is kept in memory only (never sessionStorage): in a Home Assistant
// sidebar this page shares its origin with the HA frontend and everything else loaded there.
function storedKey(){ return null; }
function say(t,cls){ const e=$("msg"); e.textContent=t||""; e.className="small "+(cls||""); }
// Gateway answers carry no JSON (Cloudflare's 524 after ~125 s, its page for a 504, 502/503 while the pod restarts).
function gatewayError(s){ return (s===504||s===524)?"閘道逾時（HTTP "+s+"）：後端可能仍在處理，請稍候重新整理確認結果，不要重複送出。":(s===502||s===503||(s>=520&&s<=523))?"暫時連不上 LiteLLM（HTTP "+s+"）：服務可能正在重新啟動，請稍候重新整理再試。":null; }
let gatewayRefresh=null;
async function api(method,path,body){
  let r;
  try{ r=await fetch(API+path,{method,headers:Object.assign({"Authorization":"Bearer "+key},body?{"Content-Type":"application/json"}:{}),body:body?JSON.stringify(body):undefined}); }
  catch(e){ const err=new Error("連線中斷：請檢查網路後重新整理確認結果。"); err.status=0; throw err; }
  let j=null; try{ j=await r.json(); }catch(e){}
  if(!r.ok){ const d=j&&(j.detail||(j.error&&j.error.message)); const gw=gatewayError(r.status); const err=new Error(typeof d==="string"?d:(gw||("HTTP "+r.status))); err.status=r.status;
    if(gw&&!gatewayRefresh) gatewayRefresh=setTimeout(function(){ gatewayRefresh=null; refresh(); },15000);
    throw err; }
  return j;
}
const q=a=>"?account="+encodeURIComponent(a);
function fmtTime(sec){ if(!sec) return "—"; return new Date(sec*1000).toLocaleString("zh-TW",{hour12:false}); }
function row(dl,k,v){ const dt=document.createElement("dt"); dt.textContent=k; const dd=document.createElement("dd"); dd.textContent=v; dl.append(dt,dd); }
function plan(p){ return p?("ChatGPT "+p.charAt(0).toUpperCase()+p.slice(1)):""; }
function label(a){ return a.account==="default"?"預設帳號":(a.credentials&&a.credentials[0])||a.label||a.account; }

async function useKey(k,fromCookie){
  key=k;
  try{
    await loadAccounts();
    say("");
    $("authInfo").textContent=fromCookie?"已使用你在 LiteLLM 後台的管理員登入。":"已使用輸入的金鑰。";
    $("keyRow").classList.add("hidden");
    await select(cur); return true;
  }catch(e){
    // A gateway answer or a dropped connection says nothing about the login: keep the key and
    // try it again in 15 s (the refresh api() scheduled would run without a key).
    if(e.status===0||gatewayError(e.status)){
      say(e.message,"err");
      $("authInfo").textContent="暫時無法確認 LiteLLM 後台的登入，15 秒後自動重試。";
      if(gatewayRefresh) clearTimeout(gatewayRefresh);
      gatewayRefresh=setTimeout(function(){ gatewayRefresh=null; useKey(k,fromCookie); },15000);
      return true;
    }
    key=null;
    $("authInfo").textContent=(e.status===403?"這個身分不是 LiteLLM 管理員。":"尚未登入 LiteLLM 後台。")+" 請先到 /ui 用管理員登入，或在下方輸入 master key／管理員金鑰。";
    $("keyRow").classList.remove("hidden"); return false;
  }
}

async function loadAccounts(){
  const r=await api("GET","/accounts"); accounts=r.accounts||[];
  $("accountsCard").classList.remove("hidden");
  const box=$("accountList"); box.textContent="";
  const list=accounts.slice();
  if(pendingName && !list.some(a=>a.account===cur)) list.push({account:cur,label:pendingName,credentials:[pendingName],connected:false,models:[]});
  for(const a of list){
    if(a.account==="default" && cur!=="default" && !a.connected && !(a.models||[]).length && list.length>1) continue;   // hide an unused default
    const b=document.createElement("button"); b.className="acct"+(a.account===cur?" sel":"");
    const who=document.createElement("span"); who.className="who";
    const t=document.createElement("b"); t.textContent=label(a);
    const s=document.createElement("span"); s.className="muted small"; s.textContent=a.connected?((a.email||"")+(a.plan_type?" · "+plan(a.plan_type):"")):"尚未連接";
    who.append(t,s);
    const right=document.createElement("span"); right.className="row small";
    const n=document.createElement("span"); n.className="muted"; n.textContent=(a.models||[]).length?((a.models||[]).length+" 個模型"):"";
    const bd=document.createElement("span"); bd.className="badge "+(a.connected?"ok":"warn"); bd.textContent=a.connected?"已連接":"未連接";
    right.append(n,bd); b.append(who,right);
    b.onclick=()=>{ pendingName=null; select(a.account); };
    box.append(b);
  }
}

async function select(account){
  cur=account; remote=[]; clearTimeout(pollTimer); clearInterval(countdownTimer);
  $("loginCard").classList.add("hidden");
  try{ history.replaceState(null,"",location.pathname+"?"+(EMBED?"embed=1&":"")+"account="+encodeURIComponent(cur)); }catch(e){}
  document.querySelectorAll(".acct").forEach(b=>b.classList.remove("sel"));
  await loadAccounts();
  try{ render(await api("GET","/status"+q(cur))); }catch(e){ say(e.message,"err"); }
}

function render(s){
  curState=s; notify("state");
  $("statusCard").classList.remove("hidden");
  const a=accounts.find(x=>x.account===cur)||{account:cur,credentials:pendingName?[pendingName]:[]};
  const isDefault=cur==="default";
  $("acctTitle").textContent=isDefault?"預設帳號":(label(a));
  const dl=$("statusList"); dl.textContent="";
  const badge=$("stateBadge");
  if(!isDefault) row(dl,"LiteLLM 憑證",(s.credentials&&s.credentials.length)?s.credentials.join("、"):(pendingName?pendingName+"（連上後建立）":"（無）"));
  else row(dl,"用途","沒有指定憑證的 chatgpt/ 模型走這個帳號");
  if(s.connected){
    badge.innerHTML='<span class="badge ok">已連接</span>';
    row(dl,"ChatGPT 帳號",s.email||"—"); row(dl,"方案",plan(s.plan_type)||"—");
    row(dl,"訂閱有效至",s.subscription_active_until?new Date(s.subscription_active_until).toLocaleDateString("zh-TW"):"—");
    row(dl,"存取憑證有效至（會自動更新）",fmtTime(s.access_token_expires_at));
    row(dl,"保存位置",(s.persist_error || s.persisted===false)?"保存尚未完成，請先處理下方異常":(s.storage==="database"?"LiteLLM 資料庫（重啟不掉）":"檔案（請確認目錄是持久化的）"));
    $("btnConnect").textContent="重新登入"; $("btnDisconnect").classList.remove("hidden");
    $("modelsCard").classList.remove("hidden");
  }else{
    badge.innerHTML='<span class="badge warn">未連接</span>';
    row(dl,"狀態","尚未連接 ChatGPT");
    $("btnConnect").textContent="連接 ChatGPT"; $("btnDisconnect").classList.add("hidden");
    $("modelsCard").classList.add("hidden");
  }
  if(s.persist_error || s.persisted===false){
    badge.innerHTML='<span class="badge warn">登入保存異常</span>';
    row(dl,"保存狀態","登入資料尚未完整保存，請修復儲存空間或資料庫後重試。");
  }
  $("btnRemoveAcct").classList.toggle("hidden",isDefault||(!s.connected&&!(s.credentials||[]).length&&!(s.models||[]).length));
  if(s.models&&s.models.length) row(dl,"已加入 LiteLLM 的模型",[...new Set(s.models.map(m=>m.model_name))].join("、"));
  $("namingBox").classList.toggle("hidden",isDefault);
  $("prefixHint").textContent=((s.credentials&&s.credentials[0])||pendingName||cur).replace(/[^A-Za-z0-9._-]+/g,"-")+"/…";
  if(s.pending){ showCode(s.pending); }
  else if(!s.connected && pendingName){ showMethods(); }
  if(s.connected && !remote.length) loadModels();
}

function showMethods(){ clearTimeout(pollTimer); clearInterval(countdownTimer); $("loginCard").classList.remove("hidden"); $("consent").classList.remove("hidden"); $("codeBox").classList.add("hidden"); $("browserBox").classList.add("hidden"); $("agree").checked=false; $("btnStart").disabled=true; $("btnBrowser").disabled=true; }
function showCode(p){
  $("loginCard").classList.remove("hidden"); $("consent").classList.add("hidden"); $("browserBox").classList.add("hidden"); $("codeBox").classList.remove("hidden");
  $("userCode").textContent=p.user_code; const a=$("verifyLink"); a.href=p.verification_url; a.textContent=p.verification_url;
  clearInterval(countdownTimer);
  const tick=()=>{ const left=Math.max(0,Math.round(p.expires_at-Date.now()/1000)); $("countdown").textContent="剩 "+Math.floor(left/60)+" 分 "+String(left%60).padStart(2,"0")+" 秒"; if(!left) clearInterval(countdownTimer); };
  tick(); countdownTimer=setInterval(tick,1000);
  clearTimeout(pollTimer); schedulePoll(Math.max(5,p.interval||5));
}
function schedulePoll(sec){
  const acct=cur;
  pollTimer=setTimeout(async()=>{
    try{
      const r=await api("POST","/poll",{account:acct});
      if(acct!==cur) return;
      if(r.state==="pending"){ schedulePoll(Math.max(5,(r.pending&&r.pending.interval)||5)); return; }
      clearInterval(countdownTimer); $("loginCard").classList.add("hidden");
      if(r.state==="connected"){ await connected(); return; }
      if(r.state==="expired") say("代碼已過期，請重新取得。","warn");
      else if(r.state==="error") say(loginError(r),"err");
      refresh();
    }catch(e){ say("輪詢失敗："+e.message,"err"); schedulePoll(10); }
  },sec*1000);
}
async function connected(){
  if(pendingName){
    try{ await api("POST","/accounts/bind",{name:pendingName}); say("已連接，並建立 LiteLLM 憑證「"+pendingName+"」。接著在下方勾選要用的模型，按「加入選取」。","ok"); }
    catch(e){ say("已連接，但建立憑證失敗："+e.message,"warn"); }
    pendingName=null;
  } else say("已連接。接著在下方「模型」勾選要用的模型，按「加入選取」。","ok");
  remote=[]; await select(cur);
}
async function refresh(){ try{ await loadAccounts(); render(await api("GET","/status"+q(cur))); }catch(e){ say(e.message,"err"); } }

async function loadModels(){
  const tb=$("modelRows"); tb.innerHTML='<tr><td colspan="5" class="muted">讀取中…</td></tr>';
  try{
    const r=await api("GET","/models"+q(cur));
    if(r.error){ tb.innerHTML=""; say("讀不到模型清單："+r.error,"err"); return; }
    remote=r.models||[]; drawModels();
  }catch(e){ tb.innerHTML=""; say(e.message,"err"); }
}
function drawModels(){
  const tb=$("modelRows"); tb.textContent=""; const showHidden=$("showHidden").checked;
  for(const m of remote){
    if(!m.visible && !showHidden && !m.added) continue;
    const tr=document.createElement("tr");
    const c=document.createElement("td"); const cb=document.createElement("input"); cb.type="checkbox"; cb.value=m.model; cb.className="pick"; c.append(cb);
    const n=document.createElement("td"); const code=document.createElement("code"); code.textContent=m.model; const dn=document.createElement("div"); dn.className="muted small"; dn.textContent=m.display_name+(m.visible?"":"（隱藏）"); n.append(code,dn);
    const d=document.createElement("td"); d.className="small"; d.textContent=(m.description||"")+(m.context_window?("　上下文 "+Math.round(m.context_window/1000)+"K"):"");
    const st=document.createElement("td"); st.innerHTML=m.added?'<span class="badge ok">已加入</span>':'<span class="muted small">未加入</span>';
    const t=document.createElement("td"); const b=document.createElement("button"); b.textContent="測試"; b.onclick=()=>testModel(m.model,b); t.append(b);
    tr.append(c,n,d,st,t); tb.append(tr);
  }
  if(!tb.children.length) tb.innerHTML='<tr><td colspan="5" class="muted">沒有模型</td></tr>';
}
function picked(){ return [...document.querySelectorAll(".pick:checked")].map(e=>e.value); }
function naming(){ const r=document.querySelector("input[name=naming]:checked"); return r?r.value:"shared"; }
async function changeModels(action){
  const ms=picked(); if(!ms.length){ say("請先勾選模型。","warn"); return; }
  if(action==="remove" && !confirm("從 LiteLLM 移除這個帳號的 "+ms.length+" 個模型？")) return;
  say(action==="add"?"加入中…":"移除中…");
  try{ const r=await api("POST","/models",{action,account:cur,models:ms,naming:naming()});
       const done=action==="add"?r.added:r.removed;
       say((action==="add"?"已加入 ":"已移除 ")+(done||[]).length+" 個"+((r.kept||[]).length?("，"+r.kept.length+" 個原本就有"):"")+((r.failed||[]).length?("；失敗："+r.failed.join("、")):""),(r.failed||[]).length?"warn":"ok");
       remote=[]; await refresh(); loadModels();
  }catch(e){ say(e.message,"err"); }
}
async function testModel(model,btn){
  btn.disabled=true; btn.textContent="測試中…";
  try{ const r=await api("POST","/test",{account:cur,model}); say(r.ok?(model+" 正常（"+r.seconds+" 秒，回覆："+r.reply+"）"):(model+" 失敗："+r.error),r.ok?"ok":"err"); }
  catch(e){ say(e.message,"err"); }
  btn.disabled=false; btn.textContent="測試";
}

$("agree").onchange=e=>{ $("btnStart").disabled=!e.target.checked; $("btnBrowser").disabled=!e.target.checked; };
$("btnConnect").onclick=()=>showMethods();
document.querySelectorAll(".backToMethods").forEach(a=>a.onclick=e=>{ e.preventDefault(); showMethods(); });
$("btnBrowser").onclick=async()=>{ $("btnBrowser").disabled=true; say("準備登入網址…");
  try{ const r=await api("POST","/browser/start",{account:cur}); $("authLink").href=r.authorize_url; $("consent").classList.add("hidden"); $("codeBox").classList.add("hidden"); $("browserBox").classList.remove("hidden"); $("authInput").value=""; say(""); }
  catch(e){ say(e.message,"err"); } $("btnBrowser").disabled=false; };
const BROWSER_ERRORS={no_code:"貼上的內容裡沒有授權碼，請複製整段網址。",state_mismatch:"這個網址不是這次登入產生的，請按「開啟 OpenAI 登入頁」重新登入。",token_exchange_failed:"授權碼無效或已用過，請重新登入一次。",browser_login_expired:"登入已逾時，請重新開始。",no_browser_login:"沒有進行中的瀏覽器登入，請重新開始。"};
function loginError(r){
  if(r&&r.error==="duplicate_account"){ const o=accounts.find(x=>x.account===r.duplicate_of); return "這個 ChatGPT 帳號（"+(r.email||"")+"）已經連在「"+(o?label(o):r.duplicate_of)+"」了，同一個帳號不用連兩次。請改用另一個 ChatGPT 帳號登入（瀏覽器登入前先登出 ChatGPT 或用無痕視窗）。"; }
  return BROWSER_ERRORS[r&&r.error]||("連結失敗（"+((r&&r.error)||"")+"）");
}
$("btnFinish").onclick=async()=>{ const v=$("authInput").value.trim(); if(!v){ say("請貼上跳轉後的網址。","warn"); return; }
  $("btnFinish").disabled=true; say("連結中…");
  try{ const r=await api("POST","/browser/finish",{account:cur,input:v});
       if(r.state==="connected"){ $("loginCard").classList.add("hidden"); $("authInput").value=""; await connected(); }
       else say(loginError(r),"err"); }
  catch(e){ say(e.message,"err"); } $("btnFinish").disabled=false; };
$("btnStart").onclick=async()=>{ $("btnStart").disabled=true; say("向 OpenAI 取得代碼…");
  try{ const r=await api("POST","/start",{account:cur}); if(!r.ok){ say("取得代碼失敗（"+(r.error||"")+"），稍後再試。","err"); $("btnStart").disabled=false; return; } say(""); showCode(r.pending); }
  catch(e){ say(e.message,"err"); $("btnStart").disabled=false; } };
$("btnCopy").onclick=async()=>{ try{ await navigator.clipboard.writeText($("userCode").textContent); say("已複製代碼。","ok"); }catch(e){ say("無法自動複製，請選取代碼後手動複製。","err"); } };
$("btnDisconnect").onclick=async()=>{ if(!confirm("中斷後這台 LiteLLM 會忘記這個 ChatGPT 帳號的登入（模型與憑證保留；不會撤銷 OpenAI 那邊的授權）。確定？")) return;
  try{ const r=await api("POST","/disconnect",{account:cur}); if(!r.ok) throw new Error("中斷失敗，登入保留；請重試。"); remote=[]; say("已中斷連接。","ok"); refresh(); }catch(e){ say(e.message,"err"); } };
$("btnRemoveAcct").onclick=async()=>{ if(!confirm("移除這個帳號：刪除它在 LiteLLM 的模型與憑證，並忘記登入。確定？")) return;
  try{ const r=await api("POST","/accounts/remove",{account:cur}); if(!r.ok || (r.failed||[]).length) throw new Error("部分移除失敗，登入保留；請重試。"+(r.failed||[]).join("、")); say("已移除（模型 "+(r.removed_models||[]).length+" 個、憑證 "+(r.removed_credentials||[]).length+" 個）。","ok"); pendingName=null; await select("default"); }catch(e){ say(e.message,"err"); } };
$("btnNew").onclick=()=>{ $("newBox").classList.remove("hidden"); $("newName").focus(); };
$("btnNewCancel").onclick=()=>{ $("newBox").classList.add("hidden"); $("newName").value=""; };
$("btnNewGo").onclick=async()=>{ const name=$("newName").value.trim(); if(!name){ say("請輸入名稱。","warn"); return; }
  try{ const r=await api("POST","/accounts/resolve",{name}); $("newBox").classList.add("hidden"); $("newName").value="";
       pendingName=r.credential_exists?null:name; say(r.credential_exists?"這個名稱的憑證已存在，切換到它。":"請登入要連的 ChatGPT 帳號；連上後會建立 LiteLLM 憑證「"+name+"」。",""); await select(r.account); }
  catch(e){ say(e.message,"err"); } };
$("newName").addEventListener("keydown",e=>{ if(e.key==="Enter"){ e.preventDefault(); $("btnNewGo").click(); } });
$("showHidden").onchange=drawModels;
$("btnAll").onclick=()=>document.querySelectorAll(".pick").forEach(e=>e.checked=true);
$("btnNone").onclick=()=>document.querySelectorAll(".pick").forEach(e=>e.checked=false);
$("btnAdd").onclick=()=>changeModels("add");
$("btnRemove").onclick=()=>changeModels("remove");
$("btnReload").onclick=()=>{ remote=[]; loadModels(); };
$("btnDone").onclick=()=>notify("done");
$("keySave").onclick=async()=>{ const k=$("keyInput").value.trim(); if(!k) return; if(await useKey(k,false)){ $("keyInput").value=""; } };

(async()=>{ const c=cookieKey(); if(c && await useKey(c,true)) return; const s=storedKey(); if(s && await useKey(s,false)) return;
  if(!c&&!s){ $("authInfo").textContent="尚未登入 LiteLLM 後台。請先到 /ui 用管理員登入後再回來，或在下方輸入 master key／管理員金鑰。"; $("keyRow").classList.remove("hidden"); } })();
})();
</script>
</body>
</html>
"""


# ── LiteLLM `litellm_settings.callbacks` entry ────────────────────────────────
def _bootstrap_from_config() -> None:
    """Loaded by LiteLLM from config.yaml: put the sign-ins back from the database, patch the
    ChatGPT provider, and add the page to the running proxy app."""
    if os.environ.get("WOOW_CHATGPT_ENABLED") != "true":  # the WOOW chart restores in start-litellm.sh
        try:
            result = core.restore()
            core._log(f"restore: {result.get('state')} kept={result.get('kept')} restored={result.get('restored')}")
        except Exception as exc:  # noqa: BLE001
            core._log(f"WARNING restore failed: {type(exc).__name__}")
    core.install_import_hook(include_routes=False)
    from litellm.proxy import proxy_server

    install(proxy_server.app)


try:
    from litellm.integrations.custom_logger import CustomLogger as _Base
except Exception:  # noqa: BLE001 — imported outside LiteLLM (tests, the CLI)
    _Base = object


class WoowChatGPT(_Base):
    """Load the plugin and preserve server-configured ChatGPT account bindings."""

    # Run this security boundary even when the proxy requests content-enforcing hooks only.
    enforces_request_content = True

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        # Loaded through litellm_settings.callbacks (non-chart LiteLLM). The chart and the HA
        # add-on run the same check from core._patch_proxy_logging; running it twice is harmless.
        check_request(user_api_key_dict, data)
        return data


def _request_models(names, user_api_key_dict) -> set[str]:
    """Model group names a request may reach: what it names, plus key/team and router aliases."""
    models = {n for n in names if isinstance(n, str) and n}
    # Only authenticated key/team metadata may resolve public model aliases.
    for field in ("aliases", "team_model_aliases"):
        aliases = getattr(user_api_key_dict, field, None)
        if isinstance(aliases, dict):
            models.update(aliases[name] for name in tuple(models) if isinstance(aliases.get(name), str))
    # Router settings resolve global aliases after this hook. Use the same pinned resolver
    # for both string and {model, hidden} entry shapes.
    from litellm.router_utils.common_utils import resolve_model_group_alias

    proxy = sys.modules.get("litellm.proxy.proxy_server")
    router_aliases = getattr(getattr(proxy, "llm_router", None), "model_group_alias", None)
    for name in tuple(models):
        target = resolve_model_group_alias(router_aliases, name)
        if target:
            models.add(target)
    return models


def _fallback_targets(data) -> list:
    try:  # LiteLLM's own walk over fallbacks / context_window_fallbacks / … (nested shapes)
        from litellm.proxy.auth.auth_utils import iter_request_fallback_targets

        return list(iter_request_fallback_targets(data))
    except Exception:  # noqa: BLE001 — older LiteLLM: the plain top-level list
        value = data.get("fallbacks")
        return list(value) if isinstance(value, list) else []


def account_override_field(user_api_key_dict, data) -> str | None:
    """R02: the request field that would make a ChatGPT subscription model use another account
    than its deployment's, or None. Checked on the request itself and on each fallback target it
    names (LiteLLM hands the request's api_key to fallbacks too):
      * api_key "woow-chatgpt:<account>" — never valid in a request, whatever the model;
      * litellm_credential_name naming a ChatGPT credential — likewise;
      * when a ChatGPT model is reachable: any litellm_credential_name, custom_llm_provider, or an
        api_key that is not a real ChatGPT access token (any other string resolves to the default
        account). A real access token is the caller's own ChatGPT sign-in: it never reaches an
        account kept on this server, so it stays allowed (it was before; now stated);
      * when a ChatGPT model is reachable: Authorization or ChatGPT-Account-Id (any spelling) in
        extra_headers or headers — they would point the bound token at another account or swap
        the token (the provider refuses them too: core.check_account_headers).
    Other providers' request keys (bring-your-own key) are untouched."""
    scopes, names = [data], [data.get("model")]
    for target in _fallback_targets(data):
        if isinstance(target, dict):
            scopes.append(target)
            names.append(target.get("model"))
        elif isinstance(target, str):
            names.append(target)
    models = _request_models(names, user_api_key_dict)
    chatgpt_credentials = _credential_accounts()
    reaches_chatgpt = (
        any(name.startswith("chatgpt/") for name in models)
        or any(scope.get("custom_llm_provider") == "chatgpt" for scope in scopes)
        or any(d["model_name"] in models for d in _deployments())
    )
    for scope in scopes:
        key, credential = scope.get("api_key"), scope.get("litellm_credential_name")
        if isinstance(key, str) and key.strip().startswith(core.ACCOUNT_REF_PREFIX):
            return "api_key"
        if isinstance(credential, str) and _find_name(credential, chatgpt_credentials):
            return "litellm_credential_name"
        if reaches_chatgpt:
            if credential is not None:
                return "litellm_credential_name"
            if scope.get("custom_llm_provider") is not None:
                return "custom_llm_provider"
            if key is not None and not core._looks_like_access_token(key):
                return "api_key"
            for field in ("extra_headers", "headers"):
                value = scope.get(field)
                if isinstance(value, dict) and any(str(name).lower() in core.ACCOUNT_HEADERS for name in value):
                    return field
    return None


def check_request(user_api_key_dict, data) -> None:
    """Refuse a request that tries to pick the ChatGPT account (400, OpenAI error envelope). Runs
    before routing (core._patch_proxy_logging), where request values are still apart from the
    deployment's; core.check_deployment_binding backs it up at the provider."""
    if not isinstance(data, dict):
        return
    try:
        field = account_override_field(user_api_key_dict, data)
    except Exception as exc:  # noqa: BLE001 — never break unrelated requests; the provider check still holds
        core._log(f"WARNING ChatGPT account guard skipped: {type(exc).__name__}")
        return
    if field is None:
        return
    from litellm.proxy._types import ProxyException

    refusal = ProxyException(message=core.ACCOUNT_OVERRIDE_MESSAGE, type="invalid_request_error", param=field, code=400)
    refusal.status_code = 400  # /v1/completions re-wraps an error with its status_code (else 500)
    raise refusal


if "litellm.proxy.proxy_server" in sys.modules:
    _bootstrap_from_config()
proxy_handler_instance = WoowChatGPT()


# Served at /woow/chatgpt/ui-bridge.js and loaded by every dashboard page (/ui). Ties the
# dashboard's "LLM Credentials" to ChatGPT accounts — one credential of provider "ChatGPT
# Subscription" per account, as many as wanted:
#  * Add Credential + "ChatGPT Subscription": a panel to link a NEW ChatGPT account (default) or
#    reuse a signed-in account that has no credential yet (e.g. the default account). Pressing
#    "Add Credential" for a new account opens a sub-view INSIDE the dialog (an overlay outside it
#    would count as an outside click and close the dialog), like pi's login: pick
#    "瀏覽器登入" (PKCE; paste the localhost redirect URL back, no ChatGPT setting needed) or
#    "裝置代碼登入", then tick the account's models and how to name them; the credential is saved
#    by the dashboard's own submit, the models right after it;
#  * ChatGPT has no API key: the credential's api_key becomes the account reference
#    "woow-chatgpt:<account>" (LiteLLM refuses an empty credential — the "Failed to add
#    credential" toast), api_base the ChatGPT endpoint;
#  * Edit Credential shows the account and can re-sign it in; deleting a ChatGPT credential also
#    removes that account's models and sign-in (unless another credential still uses it);
#  * credential rows of provider ChatGPT Subscription get a status badge that opens the full
#    /woow/chatgpt page for that account in an overlay.
# Pure DOM + fetch; LiteLLM's dashboard code is not modified. If the dashboard changes shape the
# bridge simply finds nothing — /woow/chatgpt keeps working on its own.
BRIDGE_JS = r"""(function(){
"use strict";
if(window.__woowChatgptBridge) return; window.__woowChatgptBridge=true;
// The proxy may sit under a path prefix (Home Assistant Ingress /api/hassio_ingress/<token>, a
// LiteLLM SERVER_ROOT_PATH): take it from this script's own URL, which the injector built.
var BASE=(function(){
  var me=document.currentScript, src=me&&me.src;
  if(src){ try{ return new URL(src,location.href).pathname.replace(/\/woow\/chatgpt\/ui-bridge\.js$/,""); }catch(e){} }
  return location.pathname.replace(/\/ui(\/.*)?$/,"");
})();
var PAGE=BASE+"/woow/chatgpt", API=PAGE+"/api", REF="woow-chatgpt:";
var CHATGPT_API_BASE="https://chatgpt.com/backend-api/codex";
var origFetch=window.fetch.bind(window);
var accounts=null, byCred={}, loadedAt=0, loading=null;

function key(){
  var m=document.cookie.match(/(?:^|;\s*)token=([^;]+)/); if(!m) return null;
  try{ var p=m[1].split(".")[1].replace(/-/g,"+").replace(/_/g,"/"); while(p.length%4) p+="=";
       var j=JSON.parse(atob(p)); if(j.exp && j.exp*1000<Date.now()) return null; return j.key||null; }catch(e){ return null; }
}
// Gateway answers carry no JSON (Cloudflare's 524 after ~125 s, its page for a 504, 502/503 while the pod restarts).
function gatewayError(s){ return (s===504||s===524)?"閘道逾時（HTTP "+s+"）：後端可能仍在處理，請稍候重新整理確認結果，不要重複送出。":(s===502||s===503||(s>=520&&s<=523))?"暫時連不上 LiteLLM（HTTP "+s+"）：服務可能正在重新啟動，請稍候重新整理再試。":null; }
function api(method,path,body){
  var k=key(); if(!k) return Promise.reject(new Error("請重新登入 LiteLLM 後台（找不到管理員登入）。"));
  var h={Authorization:"Bearer "+k}; if(body) h["Content-Type"]="application/json";
  return origFetch(API+path,{method:method,headers:h,body:body?JSON.stringify(body):undefined}).then(function(r){
    return r.json().catch(function(){ return {}; }).then(function(j){
      if(!r.ok){ var d=j&&j.detail; var err=new Error(typeof d==="string"?d:(gatewayError(r.status)||("HTTP "+r.status))); err.status=r.status; throw err; } return j; });
  }, function(){ var err=new Error("連線中斷：請檢查網路後重新整理確認結果。"); err.status=0; throw err; });
}
function isChatgpt(v){ return /chatgpt/i.test(String(v||"")); }
function loadAccounts(force){
  if(!force && accounts && Date.now()-loadedAt<15000) return Promise.resolve(accounts);
  if(loading) return loading;
  loading=api("GET","/accounts").then(function(r){
    accounts=r.accounts||[]; byCred={};
    accounts.forEach(function(a){ (a.credentials||[]).forEach(function(n){ byCred[n]=a; }); });
    loadedAt=Date.now(); loading=null; paintAll(); return accounts;
  }).catch(function(){ loading=null; return accounts||[]; });
  return loading;
}
function acct(id){ return (accounts||[]).find(function(a){ return a.account===id; })||null; }
function planName(a){ return a&&a.plan_type? "ChatGPT "+a.plan_type.charAt(0).toUpperCase()+a.plan_type.slice(1):""; }
function who(a){ return a&&a.connected?((a.email||"ChatGPT 帳號")+(planName(a)?" · "+planName(a):"")):"尚未連接"; }
function label(a){ return a.account==="default"?"預設帳號":((a.credentials&&a.credentials[0])||a.account); }
function el(tag,attrs,html){ var e=document.createElement(tag); for(var k in (attrs||{})) e.setAttribute(k,attrs[k]); if(html!=null) e.innerHTML=html; return e; }
function txt(s){ return String(s==null?"":s).replace(/[&<>"']/g,function(c){ return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]; }); }
function toast(msg){ var t=el("div",{"class":"woow-cg-toast"}); t.textContent=msg; document.body.appendChild(t); setTimeout(function(){ t.remove(); },5000); }

window.addEventListener("message",function(e){
  if(e.origin!==location.origin || !e.data || e.data.source!=="woow-chatgpt") return;
  if(e.data.type==="done"){ document.querySelectorAll(".woow-cg-overlay").forEach(function(o){ o.remove(); }); }
  loadAccounts(true);
});

// ── styles ──
var css=document.createElement("style");
css.textContent=[
".woow-cg-panel{border:1px solid rgba(127,127,127,.35);border-radius:10px;padding:10px 14px;margin:0 0 16px;font-size:13px;line-height:1.55}",
".woow-cg-sub{font-size:13px;line-height:1.6}.woow-cg-sub h3{font-size:15px;font-weight:600;margin:0 0 10px}",
".woow-cg-sub ul,.woow-cg-sub ol{margin:6px 0 10px;padding-left:20px}.woow-cg-sub li{margin:3px 0}",
".woow-cg-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px}",
".woow-cg-btn{font:inherit;font-size:13px;border-radius:6px;padding:7px 14px;cursor:pointer;border:1px solid #2563eb;background:#2563eb;color:#fff}",
".woow-cg-btn.secondary{background:transparent;color:inherit;border-color:rgba(127,127,127,.5)}.woow-cg-btn:disabled{opacity:.5;cursor:not-allowed}",
".woow-cg-method{display:block;width:100%;text-align:left;margin-top:8px;padding:12px 14px;border-radius:8px;border:1px solid rgba(127,127,127,.4);background:transparent;color:inherit;cursor:pointer;font:inherit}",
".woow-cg-method:hover:not(:disabled){border-color:#2563eb}.woow-cg-method:disabled{opacity:.5;cursor:not-allowed}.woow-cg-method b{display:block;font-size:14px}",
".woow-cg-muted{opacity:.7;font-size:12px}.woow-cg-ok{color:#15803d}.woow-cg-warn{color:#b45309}.woow-cg-err{color:#b91c1c}",
".woow-cg-input,.woow-cg-select{flex:1;min-width:0;font:inherit;padding:7px 10px;border:1px solid rgba(127,127,127,.5);border-radius:6px;background:transparent;color:inherit}",
".woow-cg-select option{color:#111}",
".woow-cg-code{font:600 26px/1.2 ui-monospace,Menlo,monospace;letter-spacing:.08em;padding:8px 14px;border-radius:8px;background:rgba(127,127,127,.12)}",
".woow-cg-models{max-height:240px;overflow:auto;border:1px solid rgba(127,127,127,.3);border-radius:8px;padding:6px 10px;margin-top:8px}",
".woow-cg-models label,.woow-cg-naming label{display:flex;gap:8px;align-items:flex-start;padding:3px 0}.woow-cg-models code{font-size:12px}",
".woow-cg-badge{font-size:12px;margin-left:8px;padding:1px 8px;border-radius:999px;border:1px solid currentColor;cursor:pointer;background:transparent;white-space:nowrap;max-width:240px;overflow:hidden;text-overflow:ellipsis}",
".woow-cg-overlay{position:fixed;inset:0;z-index:2147483000;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:16px}",
".woow-cg-sheet{background:#fff;color:#111;border-radius:12px;width:min(880px,100%);height:min(90vh,920px);display:flex;flex-direction:column;overflow:hidden}",
"@media (max-width:640px){.woow-cg-model-content{padding-left:12px!important;padding-right:12px!important}.woow-cg-model-frame{margin-left:8px!important;margin-right:8px!important}",
".woow-cg-model-tabbar{flex-wrap:wrap!important}.woow-cg-model-tabscroll{flex:1 0 100%!important;width:100%;min-width:0;mask-image:none!important}",
".woow-cg-model-tablist{display:flex!important;flex-wrap:wrap!important;width:100%!important;min-width:0}.woow-cg-model-tablist>[role=tab]{flex:0 1 auto!important;min-width:0;max-width:100%;height:auto!important;white-space:normal!important;overflow-wrap:anywhere}}",
"@media (prefers-color-scheme:dark){.woow-cg-sheet{background:#171a21;color:#e6e8eb}}",
".woow-cg-sheet header{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-bottom:1px solid rgba(127,127,127,.3);font-weight:600}",
".woow-cg-sheet iframe{flex:1;border:0;width:100%}",
".woow-cg-toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:2147483001;background:#111;color:#fff;padding:10px 16px;border-radius:8px;font-size:13px;max-width:90vw}",
// LiteLLM 1.104.0: keep the native sidebar toggle/state and table scroll container.
// The media query uses the dashboard iframe's width, not the outer HA viewport.
"@media (max-width:767px){",
".woow-cg-dashboard > [data-slot=sidebar][data-collapsed=false]{position:fixed;inset:0 auto 0 0;z-index:40}",
".woow-cg-dashboard > [data-slot=sidebar][data-collapsed=false] + div{margin-left:72px}",
".woow-cg-models-page{padding:8px}",
".woow-cg-credentials{min-width:0}",
".woow-cg-credentials > div:first-child{flex-wrap:wrap}",
".woow-cg-credentials > div:first-child > p{flex:1 0 100%;min-width:0}",
".woow-cg-credentials [data-slot=table-container]{max-width:100%;overflow-x:auto;overscroll-behavior-x:contain}",
"}"
].join("\n");
(document.head||document.documentElement).appendChild(css);

// Mark only the verified gateway shell and Models / Credentials DOM. These hooks
// survive React tab changes via paintAll; no synthetic clicks or auth/API changes.
function ensureResponsiveLayout(){
  var sidebar=document.querySelector("aside[data-slot=sidebar][data-collapsed]");
  if(sidebar && sidebar.nextElementSibling && sidebar.nextElementSibling.querySelector("main")){
    sidebar.parentElement.classList.add("woow-cg-dashboard");
  }
  document.querySelectorAll("main > .mx-4 > .p-8 > [data-slot=tabs]").forEach(function(tabs){
    var credentialsTab=Array.prototype.find.call(tabs.querySelectorAll("[role=tab]"),function(t){ return t.textContent.trim()==="LLM Credentials"; });
    if(!credentialsTab) return;
    tabs.parentElement.classList.add("woow-cg-models-page");
    tabs.querySelectorAll("[role=tabpanel] > div.mx-auto.w-full").forEach(function(panel){
      var header=panel.firstElementChild, description=header && header.querySelector(":scope > p");
      if(description && description.textContent.trim()==="Configured credentials for different AI providers. Add and manage your API credentials."){
        panel.classList.add("woow-cg-credentials");
      }
    });
  });
}

// ── dialogs ──
function credentialDialogs(){
  return Array.prototype.filter.call(document.querySelectorAll("[role=dialog]"),function(d){
    var t=d.querySelector("[data-slot=dialog-title],h2"); var s=t?t.textContent.trim():"";
    return s==="Add New Credential"||s==="Edit Credential";
  });
}
function isEdit(dialog){ var t=dialog.querySelector("[data-slot=dialog-title],h2"); return !!t && t.textContent.trim()==="Edit Credential"; }
function chatgptSelected(dialog){ var i=dialog.querySelector("#custom_llm_provider"); return !!(i && isChatgpt(i.value)); }
function credName(dialog){ var i=dialog.querySelector("#credential_name"); return i?i.value.trim():""; }
function credRaw(dialog){ var i=dialog.querySelector("#credential_name"); return i?i.value:""; }
function credOf(name){ if(byCred[name]) return byCred[name]; var t=String(name||"").trim(); for(var k in byCred){ if(k.trim()===t) return byCred[k]; } return null; }
function choice(dialog){ var s=dialog.querySelector(".woow-cg-choice"); return s?s.value:"__new__"; }

function paintPanel(dialog,panel){
  var st=panel.querySelector(".woow-cg-status");
  if(isEdit(dialog)){
    var a=credOf(credRaw(dialog));
    st.textContent=a?("連結的 ChatGPT 帳號："+who(a)):"（這個憑證還沒有連結 ChatGPT 帳號）";
    st.className="woow-cg-status "+(a&&a.connected?"woow-cg-ok":"woow-cg-warn");
    return;
  }
  var sel=panel.querySelector(".woow-cg-choice");
  if(sel && accounts){
    var free=accounts.filter(function(a){ return a.connected && !(a.credentials||[]).length; });
    var sig=free.map(function(a){ return a.account+":"+a.email; }).join("|");
    if(sel.dataset.sig!==sig){
      var keep=sel.value; sel.innerHTML='<option value="__new__">登入一個新的 ChatGPT 帳號（建議）</option>';
      free.forEach(function(a){ var o=el("option",{value:a.account}); o.textContent="使用已連結的帳號："+label(a)+"（"+who(a)+"）"; sel.appendChild(o); });
      sel.dataset.sig=sig; if(Array.prototype.some.call(sel.options,function(o){ return o.value===keep; })) sel.value=keep;
    }
  }
  var c=choice(dialog);
  if(c==="__new__"){ st.textContent="按「Add Credential」後選擇登入方式，登入這個憑證要連的 ChatGPT 帳號。"; st.className="woow-cg-status woow-cg-muted"; }
  else { var a2=acct(c); st.textContent="這個憑證會使用："+(a2?who(a2):c); st.className="woow-cg-status woow-cg-ok"; }
}
function ensurePanel(dialog){
  var form=dialog.querySelector("form"), panel=dialog.querySelector(".woow-cg-panel");
  if(!chatgptSelected(dialog)){ if(panel) panel.remove(); return; }
  if(dialog.querySelector(".woow-cg-sub")) return;
  if(panel){ paintPanel(dialog,panel); loadAccounts(false); return; }
  if(!form || !form.lastElementChild) return;
  if(isEdit(dialog)){
    panel=el("div",{"class":"woow-cg-panel"},
      '<div><b>ChatGPT 訂閱</b>：每個 ChatGPT Subscription 憑證連結一個 ChatGPT 帳號。</div>'+
      '<div class="woow-cg-row"><span class="woow-cg-status"></span></div>'+
      '<div class="woow-cg-row"><button type="button" class="woow-cg-btn secondary woow-cg-relogin">重新登入這個帳號</button><button type="button" class="woow-cg-btn secondary woow-cg-manage">管理模型</button></div>');
    panel.querySelector(".woow-cg-relogin").addEventListener("click",function(ev){ ev.preventDefault(); ev.stopPropagation();
      var name=credRaw(dialog), a=credOf(name);
      (a?Promise.resolve({account:a.account}):api("POST","/accounts/resolve",{name:name})).then(function(r){ openSub(dialog,r.account,name,false); })
        .catch(function(err){ var st=panel.querySelector(".woow-cg-status"); st.textContent=err.message; st.className="woow-cg-status woow-cg-err"; }); });
    panel.querySelector(".woow-cg-manage").addEventListener("click",function(ev){ ev.preventDefault(); ev.stopPropagation(); var a=credOf(credRaw(dialog)); openOverlay(a?a.account:"default"); });
  } else {
    panel=el("div",{"class":"woow-cg-panel"},
      '<div><b>ChatGPT 訂閱不需要 API Key</b>。每個 ChatGPT Subscription 憑證連結一個 ChatGPT（Plus／Pro）帳號，<b>可以建立多個</b>；不同帳號加入同名模型時，LiteLLM 會在帳號間自動分流。</div>'+
      '<div class="woow-cg-row"><select class="woow-cg-select woow-cg-choice"><option value="__new__">登入一個新的 ChatGPT 帳號（建議）</option></select></div>'+
      '<div class="woow-cg-row"><span class="woow-cg-status"></span></div>');
    panel.querySelector(".woow-cg-choice").addEventListener("change",function(){ paintPanel(dialog,panel); });
  }
  form.insertBefore(panel, form.lastElementChild);
  paintPanel(dialog,panel); loadAccounts(false);
}

// ── the sub-view: choose a login method → sign in → pick models ──
var timers=[];
function clearTimers(){ timers.forEach(function(t){ clearTimeout(t); clearInterval(t); }); timers=[]; }
function openSub(dialog,account,name,submitAfter){
  var form=dialog.querySelector("form"); if(!form) return;
  closeSub(dialog);
  var sub=el("div",{"class":"woow-cg-sub"}); sub.dataset.account=account; sub.dataset.name=name; sub.dataset.submitAfter=submitAfter?"1":"";
  form.style.display="none"; form.parentNode.insertBefore(sub, form);
  showMethods(dialog,sub);
}
function closeSub(dialog){
  clearTimers();
  var sub=dialog.querySelector(".woow-cg-sub"), form=dialog.querySelector("form");
  if(sub) sub.remove(); if(form) form.style.display="";
}
function footer(dialog,sub,extra){
  var row=el("div",{"class":"woow-cg-row"}); if(extra) row.appendChild(extra);
  var back=el("button",{type:"button","class":"woow-cg-btn secondary"},"返回");
  back.onclick=function(e){ e.preventDefault(); closeSub(dialog); };
  row.appendChild(back); sub.appendChild(row);
}
function otherBtn(dialog,sub){ var b=el("button",{type:"button","class":"woow-cg-btn secondary"},"改用其他方式"); b.onclick=function(e){ e.preventDefault(); showMethods(dialog,sub); }; return b; }
function say(sub,msg,cls){ var m=sub.querySelector(".woow-cg-msg"); if(!m){ m=el("p",{"class":"woow-cg-msg"}); sub.appendChild(m); } m.textContent=msg||""; m.className="woow-cg-msg "+(cls||""); }

function showMethods(dialog,sub){
  clearTimers();
  var account=sub.dataset.account, name=sub.dataset.name;
  sub.innerHTML='<h3>連結 ChatGPT 帳號：'+txt(name)+'</h3>'+
    '<div class="woow-cg-status-line woow-cg-muted"></div>'+
    '<ul class="woow-cg-muted"><li>需要 ChatGPT Plus 或 Pro。</li><li>這是借用 Codex 的登入方式，不是 OpenAI 官方的第三方整合；用量算在該帳號的訂閱，帳號風險自行承擔。</li><li>同一個 ChatGPT 帳號可以同時登入多個 LiteLLM，但每個 LiteLLM 要各自登入，不要把一台的登入檔複製到另一台（同一組 refresh token 只能用一次）；同一台 LiteLLM 內不要用兩個憑證連同一個帳號。訂閱額度由所有使用該帳號的地方共用。</li><li>登入網址與代碼只能自己使用。</li></ul>'+
    '<label class="woow-cg-row" style="margin-top:4px"><input type="checkbox" class="woow-cg-agree"> 我了解並同意</label>'+
    '<p style="margin:12px 0 0"><b>選擇登入方式</b></p>'+
    '<button type="button" class="woow-cg-method woow-cg-m-browser" disabled><b>瀏覽器登入（建議）</b><span class="woow-cg-muted">在新分頁登入 ChatGPT，再把跳轉後的網址貼回來。不用改 ChatGPT 設定。</span></button>'+
    '<button type="button" class="woow-cg-method woow-cg-m-device" disabled><b>裝置代碼登入</b><span class="woow-cg-muted">輸入一組代碼即可；要先在 ChatGPT「設定 → 安全性」開啟 Codex 的裝置代碼登入。</span></button>';
  var line=sub.querySelector(".woow-cg-status-line");
  loadAccounts(true).then(function(){
    var a=acct(account);
    if(a&&a.connected){
      line.textContent="這個憑證已連結："+who(a)+"。重新登入可以換成別的帳號。";
      var keep=el("button",{type:"button","class":"woow-cg-btn"},"沿用目前的連結"); keep.onclick=function(e){ e.preventDefault(); showModels(dialog,sub); };
      line.appendChild(document.createElement("br")); line.appendChild(keep);
    }
  });
  var agree=sub.querySelector(".woow-cg-agree"), mb=sub.querySelector(".woow-cg-m-browser"), md=sub.querySelector(".woow-cg-m-device");
  agree.onchange=function(){ mb.disabled=md.disabled=!agree.checked; };
  mb.onclick=function(e){ e.preventDefault(); showBrowser(dialog,sub); };
  md.onclick=function(e){ e.preventDefault(); showDevice(dialog,sub); };
  footer(dialog,sub);
}

var BROWSER_ERRORS={no_code:"貼上的內容裡沒有授權碼，請複製整段網址。",state_mismatch:"這個網址不是這次登入產生的，請重新開啟登入頁。",token_exchange_failed:"授權碼無效或已用過，請重新登入一次。",browser_login_expired:"登入已逾時，請重新開始。",no_browser_login:"沒有進行中的瀏覽器登入，請重新開始。"};
function loginError(r){
  if(r&&r.error==="duplicate_account"){ var o=acct(r.duplicate_of); return "這個 ChatGPT 帳號（"+(r.email||"")+"）已經連在「"+(o?label(o):r.duplicate_of)+"」了，同一個帳號不用連兩次。請改用另一個 ChatGPT 帳號登入（瀏覽器登入前先登出 ChatGPT 或用無痕視窗）。"; }
  return BROWSER_ERRORS[r&&r.error]||("連結失敗（"+((r&&r.error)||"")+"）");
}
function showBrowser(dialog,sub){
  clearTimers();
  var account=sub.dataset.account;
  sub.innerHTML='<h3>瀏覽器登入</h3><p class="woow-cg-muted">準備登入網址…</p>';
  api("POST","/browser/start",{account:account}).then(function(r){
    sub.innerHTML='<h3>瀏覽器登入：'+txt(sub.dataset.name)+'</h3>'+
      '<ol><li><a class="woow-cg-auth" target="_blank" rel="noopener noreferrer"><b>開啟 OpenAI 登入頁</b></a>（新分頁），登入<b>要連的那個</b> ChatGPT 帳號並按「繼續」。</li>'+
      '<li>登入後瀏覽器會跳到 <code>http://localhost:1455/auth/callback?code=…</code>，顯示「無法連上這個網站」——這是正常的。</li>'+
      '<li>複製網址列的<b>整段網址</b>，貼到下面，按「完成連結」。</li></ol>'+
      '<p class="woow-cg-muted">連第二個以上的帳號時，若 OpenAI 直接用了已登入的帳號，請先在那個分頁登出 ChatGPT（或用無痕視窗）再開登入頁。</p>'+
      '<div class="woow-cg-row"><input class="woow-cg-input" placeholder="http://localhost:1455/auth/callback?code=…" autocomplete="off" spellcheck="false"><button type="button" class="woow-cg-btn woow-cg-finish">完成連結</button></div>';
    sub.querySelector(".woow-cg-auth").href=r.authorize_url;
    var inp=sub.querySelector(".woow-cg-input"), fin=sub.querySelector(".woow-cg-finish");
    inp.addEventListener("keydown",function(e){ if(e.key==="Enter"){ e.preventDefault(); fin.click(); } });
    fin.onclick=function(e){ e.preventDefault(); var v=inp.value.trim(); if(!v){ say(sub,"請貼上跳轉後的網址。","woow-cg-warn"); return; }
      fin.disabled=true; say(sub,"連結中…");
      api("POST","/browser/finish",{account:account,input:v}).then(function(r){
        if(r.state==="connected"){ showModels(dialog,sub); }
        else { fin.disabled=false; say(sub,loginError(r),"woow-cg-err"); }
      }).catch(function(err){ fin.disabled=false; say(sub,err.message,"woow-cg-err"); }); };
    footer(dialog,sub,otherBtn(dialog,sub));
  }).catch(function(err){ say(sub,err.message,"woow-cg-err"); footer(dialog,sub,otherBtn(dialog,sub)); });
}

function showDevice(dialog,sub){
  clearTimers();
  var account=sub.dataset.account;
  sub.innerHTML='<h3>裝置代碼登入</h3><p class="woow-cg-muted">向 OpenAI 取得代碼…</p>';
  api("POST","/start",{account:account}).then(function(r){
    if(!r.ok||!r.pending){ throw new Error("取得代碼失敗（"+(r.error||"")+"），稍後再試或改用瀏覽器登入。"); }
    var p=r.pending;
    sub.innerHTML='<h3>裝置代碼登入：'+txt(sub.dataset.name)+'</h3>'+
      '<p>打開 <a class="woow-cg-verify" target="_blank" rel="noopener noreferrer"></a>，用<b>要連的那個</b> ChatGPT 帳號輸入這組代碼並同意：</p>'+
      '<div class="woow-cg-row"><span class="woow-cg-code"></span><button type="button" class="woow-cg-btn secondary woow-cg-copy">複製</button></div>'+
      '<p class="woow-cg-muted">等你登入中… <span class="woow-cg-left"></span>（會自動偵測）。若 OpenAI 顯示不允許裝置代碼，請到 ChatGPT「設定 → 安全性」開啟，或改用瀏覽器登入。</p>';
    var a=sub.querySelector(".woow-cg-verify"); a.href=p.verification_url; a.textContent=p.verification_url;
    sub.querySelector(".woow-cg-code").textContent=p.user_code;
    sub.querySelector(".woow-cg-copy").onclick=async function(e){ e.preventDefault(); try{ await navigator.clipboard.writeText(p.user_code); toast("已複製代碼。"); }catch(x){ toast("無法自動複製，請選取代碼後手動複製。"); } };
    var left=sub.querySelector(".woow-cg-left");
    var tick=function(){ var s=Math.max(0,Math.round(p.expires_at-Date.now()/1000)); left.textContent="剩 "+Math.floor(s/60)+" 分 "+String(s%60).padStart(2,"0")+" 秒"; };
    tick(); timers.push(setInterval(tick,1000));
    var poll=function(){ api("POST","/poll",{account:account}).then(function(q){
      if(q.state==="pending"){ timers.push(setTimeout(poll,Math.max(5,(q.pending&&q.pending.interval)||5)*1000)); return; }
      if(q.state==="connected"){ showModels(dialog,sub); return; }
      say(sub,q.state==="expired"?"代碼已過期，請重新開始。":loginError(q),"woow-cg-err");
    }).catch(function(){ timers.push(setTimeout(poll,10000)); }); };
    timers.push(setTimeout(poll,Math.max(5,p.interval||5)*1000));
    footer(dialog,sub,otherBtn(dialog,sub));
  }).catch(function(err){ sub.innerHTML='<h3>裝置代碼登入</h3>'; say(sub,err.message,"woow-cg-err"); footer(dialog,sub,otherBtn(dialog,sub)); });
}

function showModels(dialog,sub){
  clearTimers();
  var account=sub.dataset.account, name=sub.dataset.name, submitAfter=sub.dataset.submitAfter==="1";
  var prefix=name.replace(/[^A-Za-z0-9._-]+/g,"-").replace(/^-+|-+$/g,"").slice(0,40)||"chatgpt";
  sub.innerHTML='<h3>已連結：'+txt(name)+'</h3><p class="woow-cg-ok woow-cg-who"></p>'+
    '<p><b>要加入哪些模型？</b> <span class="woow-cg-muted">清單直接讀自這個帳號。</span></p>'+
    '<div class="woow-cg-naming"><label><input type="radio" name="woow-cg-naming" value="shared" checked> 共用名稱 <code>chatgpt/…</code>（和其他帳號的同名模型自動分流）</label>'+
    '<label><input type="radio" name="woow-cg-naming" value="prefixed"> 加上前綴 <code>'+txt(prefix)+'/…</code>（只走這個帳號）</label></div>'+
    '<div class="woow-cg-row" style="margin-top:6px"><label><input type="checkbox" class="woow-cg-hidden"> 顯示隱藏模型</label><button type="button" class="woow-cg-btn secondary woow-cg-all">全選</button><button type="button" class="woow-cg-btn secondary woow-cg-none">全不選</button></div>'+
    '<div class="woow-cg-models"><span class="woow-cg-muted">讀取中…</span></div>';
  loadAccounts(true).then(function(){ sub.querySelector(".woow-cg-who").textContent=who(acct(account)); });
  var box=sub.querySelector(".woow-cg-models"), remote=[];
  function draw(){
    var show=sub.querySelector(".woow-cg-hidden").checked; box.innerHTML="";
    remote.forEach(function(m){
      if(!m.visible && !show && !m.added) return;
      var l=el("label",null,'<input type="checkbox" value="'+txt(m.model)+'"'+(m.added||m.visible?" checked":"")+(m.added?" disabled":"")+'><span><code>'+txt(m.model)+'</code> <span class="woow-cg-muted">'+txt(m.display_name)+(m.visible?"":"（隱藏）")+(m.added?" · 已加入":"")+'</span></span>');
      box.appendChild(l);
    });
    if(!box.children.length) box.innerHTML='<span class="woow-cg-muted">沒有可加入的模型</span>';
  }
  api("GET","/models?account="+encodeURIComponent(account)).then(function(r){ if(r.error) throw new Error("讀不到模型清單："+r.error); remote=r.models||[]; draw(); })
    .catch(function(err){ box.innerHTML=""; say(sub,err.message,"woow-cg-err"); });
  sub.querySelector(".woow-cg-hidden").onchange=draw;
  sub.querySelector(".woow-cg-all").onclick=function(e){ e.preventDefault(); box.querySelectorAll("input:not(:disabled)").forEach(function(c){ c.checked=true; }); };
  sub.querySelector(".woow-cg-none").onclick=function(e){ e.preventDefault(); box.querySelectorAll("input:not(:disabled)").forEach(function(c){ c.checked=false; }); };
  var go=el("button",{type:"button","class":"woow-cg-btn"},submitAfter?"建立憑證並加入選取的模型":"加入選取的模型");
  go.onclick=function(e){ e.preventDefault();
    var picked=Array.prototype.map.call(box.querySelectorAll("input:checked:not(:disabled)"),function(c){ return c.value; });
    var naming=(sub.querySelector("input[name=woow-cg-naming]:checked")||{}).value||"shared";
    if(submitAfter){
      dialog.dataset.woowAccount=account; dialog.dataset.woowModels=JSON.stringify(picked); dialog.dataset.woowNaming=naming;
      finish(dialog,true); return;
    }
    go.disabled=true; say(sub,picked.length?"加入模型中…":"");
    (picked.length?api("POST","/models",{action:"add",account:account,credential:name,models:picked,naming:naming}):Promise.resolve({failed:[]})).then(function(r){
      if(r.failed&&r.failed.length) toast("有模型加入失敗："+r.failed.join("、"));
      return loadAccounts(true);
    }).then(function(){ finish(dialog,false); }).catch(function(err){ go.disabled=false; say(sub,err.message,"woow-cg-err"); });
  };
  footer(dialog,sub,go);
}

function finish(dialog,submit){
  var form=dialog.querySelector("form");
  closeSub(dialog); paintAll();
  if(submit && form){ form.dataset.woowBypass="1"; if(form.requestSubmit) form.requestSubmit(); else form.dispatchEvent(new Event("submit",{bubbles:true,cancelable:true})); }
}

// "Add Credential" pressed with provider ChatGPT Subscription
window.addEventListener("submit",function(e){
  var form=e.target; if(!form || form.tagName!=="FORM") return;
  var dialog=form.closest("[role=dialog]"); if(!dialog || credentialDialogs().indexOf(dialog)<0 || !chatgptSelected(dialog)) return;
  if(form.dataset.woowBypass==="1"){ delete form.dataset.woowBypass; return; }
  if(isEdit(dialog)) return;                          // edit: saved as is (the account reference is kept)
  var name=credName(dialog);
  if(!name) return;                                   // let the dashboard show "Credential name is required"
  var c=choice(dialog);
  if(c!=="__new__"){ dialog.dataset.woowAccount=c; delete dialog.dataset.woowModels; return; }   // reuse a signed-in account
  e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
  api("POST","/accounts/resolve",{name:name}).then(function(r){
    if(r.credential_exists){ toast("已有同名的 ChatGPT 憑證「"+name+"」，請換個名稱。"); return; }
    openSub(dialog,r.account,name,true);
  }).catch(function(err){ toast(err.message); });
},true);

// ── credential rows ──
function openOverlay(account){
  if(document.querySelector(".woow-cg-overlay")) return;
  var o=el("div",{"class":"woow-cg-overlay"}), sh=el("div",{"class":"woow-cg-sheet"}), h=el("header",null,"ChatGPT 訂閱");
  var x=el("button",{type:"button","class":"woow-cg-btn secondary"},"關閉"); x.onclick=function(){ o.remove(); loadAccounts(true); };
  h.appendChild(x); var f=el("iframe",{src:PAGE+"?embed=1&account="+encodeURIComponent(account||"default"),title:"ChatGPT 訂閱"});
  sh.appendChild(h); sh.appendChild(f); o.appendChild(sh);
  o.addEventListener("click",function(e){ if(e.target===o){ o.remove(); loadAccounts(true); } });
  document.body.appendChild(o);
}
function ensureBadges(){
  Array.prototype.forEach.call(document.querySelectorAll("[role=tabpanel],main"),function(p){
    if(!/Configured credentials for different AI providers/.test(p.textContent||"")) return;
    p.querySelectorAll("tr").forEach(function(tr){
      if(tr.closest("[role=dialog]")) return;
      var cells=tr.querySelectorAll("td"); if(!cells.length) return;
      var cell=Array.prototype.find.call(cells,function(td){ return /ChatGPT Subscription/.test(td.textContent||""); });
      if(!cell) return;
      var name=(cells[0].textContent||"").trim();
      var b=cell.querySelector(".woow-cg-badge");
      if(!b){ b=el("button",{type:"button","class":"woow-cg-badge"}); b.addEventListener("click",function(ev){ ev.preventDefault(); ev.stopPropagation(); var a=credOf(b.dataset.name); openOverlay(a?a.account:"default"); });
              var sp=cell.querySelector("span"); (sp&&sp.parentElement||cell).appendChild(b); loadAccounts(false); }
      b.dataset.name=name;
      var a=credOf(name);
      b.textContent=!accounts?"ChatGPT 連結":(a?(a.connected?("已連結 · "+(a.email||"")):"未連結 · 連結"):"未連結");
      b.title=a?who(a):"";
      b.className="woow-cg-badge "+(a&&a.connected?"woow-cg-ok":"woow-cg-warn");
    });
  });
}

// "Delete Credential?" asks to type the name; a name with a leading/trailing space (phone keyboard)
// can never be matched by eye. Remember which row's menu was opened (its data-testid carries the
// exact name) and offer to fill the name in.
var lastActionName=null;
function rememberRow(e){ var b=e.target&&e.target.closest&&e.target.closest('[data-testid^="credential-actions-"]'); if(b) lastActionName=b.getAttribute("data-testid").slice("credential-actions-".length); }
document.addEventListener("pointerdown",rememberRow,true); document.addEventListener("click",rememberRow,true);
function ensureDeleteHint(){
  Array.prototype.forEach.call(document.querySelectorAll("[role=dialog],[role=alertdialog]"),function(d){
    var t=d.querySelector("[data-slot=dialog-title],h2"); if(!t || t.textContent.trim()!=="Delete Credential?" || d.querySelector(".woow-cg-delhint")) return;
    var name=lastActionName, inp=d.querySelector("input");
    if(!name || name===name.trim() || !inp) return;
    var shown=name.replace(/ /g,"␣");
    var hint=el("div",{"class":"woow-cg-panel woow-cg-delhint"},"這個憑證的名稱前後有空白（<code>"+txt(shown)+"</code>，␣＝空白），要連空白一起輸入才能刪除。 ");
    var btn=el("button",{type:"button","class":"woow-cg-btn secondary"},"幫我填入名稱");
    btn.addEventListener("click",function(ev){ ev.preventDefault(); ev.stopPropagation();
      var setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set; setter.call(inp,name);
      inp.dispatchEvent(new Event("input",{bubbles:true})); inp.focus(); });
    hint.appendChild(btn);
    var host=inp.closest("[data-slot=input-group]")||inp; host.parentNode.insertBefore(hint,host);
  });
}
// The Models tab strip shares a flex row with a fixed refresh control. At phone
// widths that control can consume the whole row, leaving a zero-width scroller.
// Keep the actual tabs and their handlers; only this known tab group gets mobile CSS.
function ensureModelTabs(){
  Array.prototype.forEach.call(document.querySelectorAll('[role=tab]'),function(tab){
    if(tab.textContent.trim()!=="LLM Credentials") return;
    var list=tab.closest('[role=tablist]'), port=list&&list.parentElement, bar=port&&port.parentElement;
    if(!bar || getComputedStyle(bar).display!=="flex" || !/auto|scroll/.test(getComputedStyle(port).overflowX)) return;
    list.classList.add("woow-cg-model-tablist"); port.classList.add("woow-cg-model-tabscroll"); bar.classList.add("woow-cg-model-tabbar");
    var content=bar.closest('.p-8'), frame=content&&content.parentElement;
    if(content) content.classList.add("woow-cg-model-content");
    if(frame && frame.classList.contains('mx-4')) frame.classList.add("woow-cg-model-frame");
  });
}
function paintAll(){ ensureModelTabs(); ensureResponsiveLayout(); credentialDialogs().forEach(ensurePanel); ensureBadges(); ensureDeleteHint(); }
var queued=false;
function schedule(){ if(queued) return; queued=true; requestAnimationFrame(function(){ queued=false; paintAll(); }); }
new MutationObserver(schedule).observe(document.documentElement,{childList:true,subtree:true});
setInterval(paintAll,800);   // the provider input's value changes without DOM mutations

// ── saving / deleting ChatGPT credentials ──
function openDialog(){ var d=credentialDialogs(); return d.length?d[d.length-1]:null; }
// A name with leading/trailing whitespace (a phone keyboard's trailing space) breaks the
// dashboard: the browser's URL parser strips it from /credentials/<name>, so such a credential
// could never be edited or deleted (404). Percent-encode that last path segment; and trim the
// names of new credentials so it cannot happen again.
function keepSpaces(url){
  var cut=url.search(/[?#]/), base=cut<0?url:url.slice(0,cut), rest=cut<0?"":url.slice(cut);
  var i=base.lastIndexOf("/"), seg=base.slice(i+1), dec;
  try{ dec=decodeURIComponent(seg); }catch(e){ return {url:url,name:seg}; }
  if(!/^\s|\s$/.test(dec)) return {url:url,name:dec};
  return {url:base.slice(0,i+1)+encodeURIComponent(dec)+rest,name:dec};
}
window.fetch=function(input,init){
  try{
    var url=typeof input==="string"?input:(input&&input.url)||"";
    var method=String((init&&init.method)||(input&&input.method)||"GET").toUpperCase();
    var path=new URL(url,location.href).pathname;
    var m=path.match(/\/credentials(?:\/([^/]+))?\/?$/);
    var named=null;
    if(typeof input==="string" && /\/credentials\/[^?#]/.test(input)){ named=keepSpaces(input); input=named.url; }
    if(m && !/\/by_(name|model)\//.test(path)){
      var body=null; try{ body=init&&typeof init.body==="string"?JSON.parse(init.body):null; }catch(x){}
      if(method==="POST" && !m[1] && body && typeof body.credential_name==="string" && body.credential_name!==body.credential_name.trim()){
        body.credential_name=body.credential_name.trim(); init=Object.assign({},init,{body:JSON.stringify(body)});
      }
      var name=body&&body.credential_name||(named?named.name:(m[1]?decodeURIComponent(m[1]):""));
      if(method==="POST" && !m[1] && body && body.credential_info && isChatgpt(body.credential_info.custom_llm_provider)){
        var dialog=openDialog(), account=dialog&&dialog.dataset.woowAccount, models=dialog&&dialog.dataset.woowModels, naming=dialog&&dialog.dataset.woowNaming;
        var pick=account?Promise.resolve(account):api("POST","/accounts/resolve",{name:name}).then(function(r){ return r.account; });
        return pick.then(function(acctId){
          body.credential_values=Object.assign({},body.credential_values||{}, {api_key:REF+acctId, api_base:CHATGPT_API_BASE});
          return origFetch(input,Object.assign({},init,{body:JSON.stringify(body)})).then(function(resp){
            if(resp.ok && models){ var list=JSON.parse(models);
              if(list.length) api("POST","/models",{action:"add",account:acctId,credential:name,models:list,naming:naming||"shared"})
                .then(function(r){ toast("已加入 "+((r.added||[]).length)+" 個模型"+((r.failed||[]).length?("；失敗："+r.failed.join("、")):"")+"。"); loadAccounts(true); })
                .catch(function(err){ toast("加入模型失敗："+err.message); });
            }
            if(dialog){ delete dialog.dataset.woowAccount; delete dialog.dataset.woowModels; delete dialog.dataset.woowNaming; }
            loadAccounts(true); return resp;
          });
        });
      }
      if((method==="PATCH"||method==="PUT") && m[1] && body && body.credential_info && isChatgpt(body.credential_info.custom_llm_provider)){
        return loadAccounts(true).then(function(){
          var a=credOf(name);
          var vals=body.credential_values||{};
          if(a && !String(vals.api_key||"").startsWith(REF)) body.credential_values=Object.assign({},vals,{api_key:REF+a.account, api_base:CHATGPT_API_BASE});
          return origFetch(input,Object.assign({},init,{body:JSON.stringify(body)}));
        });
      }
      if(method==="DELETE" && m[1] && credOf(name)){
        var gone=credOf(name);
        return origFetch(input,init).then(function(resp){
          if(resp.ok && gone.account!=="default" && (gone.credentials||[]).length<=1){
            api("POST","/accounts/remove",{account:gone.account}).then(function(r){ if(!r.ok || (r.failed||[]).length) throw new Error("部分移除失敗，登入保留；請重試。"); toast("已一併移除這個 ChatGPT 帳號的登入與 "+((r.removed_models||[]).length)+" 個模型。"); loadAccounts(true); })
              .catch(function(){ loadAccounts(true); });
          } else loadAccounts(true);
          return resp;
        });
      }
    }
  }catch(e){}
  return origFetch(input,init);
};

loadAccounts(false);
})();
"""
