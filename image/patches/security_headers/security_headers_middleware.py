"""
Adds anti-framing / content-type security headers to every HTTP response.

X-Frame-Options and Content-Security-Policy: frame-ancestors 'none' stop the
admin UI and login pages from being embedded cross-origin (clickjacking).
X-Content-Type-Options: nosniff stops MIME sniffing.

Strict-Transport-Security is opt-in via LITELLM_ENABLE_HSTS because it only
makes sense over HTTPS and would lock browsers out of plain-http deployments.

Existing framing/HSTS/Referrer-Policy values and explicit CSP directives are
preserved. HTML gains missing object/base restrictions without introducing
script-src/default-src (Next hydration and inline code remain unaffected).
Credential routes, authenticated responses and Set-Cookie responses always
use no-store, superseding any accidentally public route/CDN cache policy.
"""

import os
import re
from typing import Final

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

STATIC_SECURITY_HEADERS: Final = (
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", "frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)
HSTS_HEADER: Final = ("Strict-Transport-Security", "max-age=31536000; includeSubDomains")


def _hsts_enabled() -> bool:
    return os.getenv("LITELLM_ENABLE_HSTS", "false").strip().lower() == "true"


# These routes can mint, reflect or expose credentials even without a Bearer
# request (login, errors, redirects, OAuth and UI session responses included).
_SENSITIVE_PATHS = (
    "/key", "/v2/key", "/credentials", "/user", "/invitation",
    "/login", "/v2/login", "/v3/login", "/logout", "/sso",
    "/token", "/authorize",
)


def _credential_response(scope: Scope, headers: MutableHeaders) -> bool:
    path = scope.get("path", "")
    root = scope.get("root_path", "").rstrip("/")
    if root and (path == root or path.startswith(root + "/")):
        path = path[len(root):]
    return (
        bool(Headers(scope=scope).get("authorization"))
        or "set-cookie" in headers
        or any(path == prefix or path.startswith(prefix + "/") for prefix in _SENSITIVE_PATHS)
    )


def _html_policy_defaults(headers: MutableHeaders) -> None:
    if headers.get("content-type", "").split(";", 1)[0].strip().lower() not in (
        "text/html", "application/xhtml+xml",
    ):
        return
    # Multiple CSP policies are enforced together. Keep every route policy
    # byte-for-byte, including nonce, script and intentional framing rules.
    # Add only directives that no existing enforcing policy defines.
    directives = {
        part.strip().split(None, 1)[0].lower()
        for policy in headers.getlist("content-security-policy")
        for part in re.split(r"[;,]", policy)
        if part.strip()
    }
    missing = [name + " 'none'" for name in ("object-src", "base-uri") if name not in directives]
    if missing:
        headers.append("Content-Security-Policy", "; ".join(missing))


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers: Final = MutableHeaders(scope=message)
                applied: Final = (*STATIC_SECURITY_HEADERS, HSTS_HEADER) if _hsts_enabled() else STATIC_SECURITY_HEADERS
                for name, value in applied:
                    headers.setdefault(name, value)
                _html_policy_defaults(headers)
                if _credential_response(scope, headers):
                    # Explicitly supersede any public/max-age on secret-bearing
                    # responses, including CDN-specific overrides if supplied.
                    headers["Cache-Control"] = "no-store"
                    headers["Pragma"] = "no-cache"
                    headers["Expires"] = "0"
                    for name in ("CDN-Cache-Control", "Cloudflare-CDN-Cache-Control", "Surrogate-Control"):
                        if name in headers:
                            headers[name] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_security_headers)
