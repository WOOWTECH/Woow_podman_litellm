"""WOOW PaaS litellm chart: request body cap in front of the whole LiteLLM proxy app.

Why (commercial acceptance AI-LLM-013-2): LiteLLM 1.104.0 reads AND json-parses the complete
request body before it checks the API key (user_api_key_auth -> _read_request_body). One 101 MB
body with an invalid key cost the proxy ~600 MB; three at once peaked at 2078 MiB of the 2 GiB
limit. Upstream's own cap (general_settings.max_request_size_mb, RequestSizeLimitMiddleware) only
runs for enterprise-licensed proxies (premium_user) -- the chart never sets LITELLM_LICENSE, so the
setting alone is a no-op.

What: an ASGI middleware added OUTSIDE every LiteLLM middleware when litellm.proxy.proxy_server
has been imported:
  * a declared Content-Length over the cap -> 413 at once, not one body byte read;
  * no Content-Length (chunked) -> the bytes are counted while the app reads them; past the cap
    the read fails and the client gets 413 even if LiteLLM swallows the error (its body reader
    returns {} on any exception) -- the app's own answer is replaced;
  * the 413 is OpenAI-shaped JSON and closes the connection (the unread rest of the body must
    not be taken for the next request).
WOOW_MAX_REQUEST_SIZE_MB (values litellm.maxRequestSizeMb): MiB, decimals allowed; 0 or less =
off; unset / unreadable = 32. Installed by sitecustomize; a failure never stops the proxy.
"""
from __future__ import annotations

import importlib.abc
import json
import math
import os
import sys
import threading

ENV = "WOOW_MAX_REQUEST_SIZE_MB"
DEFAULT_MB = 32
PROXY_TARGET = "litellm.proxy.proxy_server"


def _log(msg: str) -> None:
    print(f"woow body-limit: {msg}", file=sys.stderr, flush=True)


def limit_from_env() -> int | None:
    """The cap in bytes, or None when switched off."""
    raw = (os.environ.get(ENV) or "").strip()
    if not raw:
        mb = float(DEFAULT_MB)
    else:
        try:
            mb = float(raw)
        except ValueError:
            mb = float("nan")
        if not math.isfinite(mb):
            _log(f"WARNING {ENV}={raw!r} is not a number; using {DEFAULT_MB}")
            mb = float(DEFAULT_MB)
    if mb <= 0:  # off
        return None
    return int(mb * 1024 * 1024)


class _TooLarge(Exception):
    pass


def _mb(n: int) -> str:
    v = n / (1024 * 1024)
    return f"{v:g} MB"


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = int(max_bytes)

    async def _reject(self, send) -> None:
        body = json.dumps({"error": {
            "message": f"Request body too large. The limit is {_mb(self.max_bytes)}.",
            "type": "invalid_request_error", "param": None, "code": "request_too_large"}},
            separators=(",", ":")).encode()
        await send({"type": "http.response.start", "status": 413, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body, "more_body": False})

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        limit = self.max_bytes
        for name, value in scope.get("headers") or ():
            if name.lower() == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    continue
                if declared > limit:
                    return await self._reject(send)
        state = {"bytes": 0, "over": False, "started": False, "replaced": False}

        async def limited_receive():
            if state["over"]:
                raise _TooLarge()
            message = await receive()
            if message.get("type") == "http.request":
                state["bytes"] += len(message.get("body") or b"")
                if state["bytes"] > limit:
                    state["over"] = True
                    raise _TooLarge()
            return message

        async def guarded_send(message):
            kind = message.get("type")
            if state["over"] and not state["started"]:
                # The app answered after the body read failed (LiteLLM turns it into {} and
                # then a 400/401): the client gets the 413 instead, exactly once.
                if kind == "http.response.start":
                    state["started"] = state["replaced"] = True
                    await self._reject(send)
                return
            if state["replaced"]:
                return
            if kind == "http.response.start":
                state["started"] = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _TooLarge:
            if state["started"]:
                raise
            state["started"] = state["replaced"] = True
            await self._reject(send)
            return
        if state["over"] and not state["started"]:
            state["started"] = state["replaced"] = True
            await self._reject(send)


def install_on(module) -> bool:
    """Add the cap to the proxy app of an imported litellm.proxy.proxy_server (once)."""
    limit = limit_from_env()
    app = getattr(module, "app", None)
    if limit is None or app is None:
        _log("off" if limit is None else "WARNING proxy_server has no app; cap NOT installed")
        return False
    if any(getattr(m, "cls", None) is BodyLimitMiddleware for m in app.user_middleware):
        return True
    app.add_middleware(BodyLimitMiddleware, max_bytes=limit)  # last added = outermost
    _log(f"request bodies over {_mb(limit)} are refused with 413 before LiteLLM reads them")
    return True


_GUARD = threading.local()


class _Finder(importlib.abc.MetaPathFinder):
    """Runs install_on() right after proxy_server executes. Delegates only to finders AFTER
    itself and refuses re-entry, so it chains with woow_chatgpt's finder in either order and
    every hook runs exactly once."""

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
            except Exception as exc:  # noqa: BLE001 -- never stop LiteLLM from starting
                _log(f"ERROR cap NOT installed: {exc!r}")

        spec.loader.exec_module = exec_module
        return spec


def install_import_hook() -> None:
    if PROXY_TARGET in sys.modules:
        install_on(sys.modules[PROXY_TARGET])
        return
    if not any(isinstance(f, _Finder) for f in sys.meta_path):
        sys.meta_path.insert(0, _Finder())
