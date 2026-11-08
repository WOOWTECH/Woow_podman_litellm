"""WOOW PaaS litellm chart: run at start-up by every python process of the proxy
container (PYTHONPATH=/woow/py). Only installs lazy import hooks (request body cap on
the proxy app; ChatGPT sign-in guard on LiteLLM's Authenticator; the /woow/chatgpt page
on the proxy app); nothing of LiteLLM is imported here, and a failure never stops the
proxy from starting."""
import os
import sys

# LiteLLM's dashboard uses a JWT cookie as a browser-readable API key.  Keep
# the real cookie HttpOnly, while the chart's middleware gives the frontend a
# nonsecret sentinel and restores the session key only inside the proxy.  Its
# import hook is registered before the body-cap hook below: import hooks run in
# reverse registration order, so BodyLimitMiddleware is added last and remains
# the proxy's outermost middleware.
if os.environ.get("WOOW_UI_TOKEN_HTTPONLY", "true").lower() not in ("0", "false", "no", "off"):
    try:
        import woow_ui_cookie_hardening

        woow_ui_cookie_hardening.install_import_hook()
    except Exception as exc:  # noqa: BLE001 — start the proxy regardless
        print(f"woow sitecustomize: UI cookie hardening NOT installed: {exc!r}", file=sys.stderr)

# Request body cap (values litellm.maxRequestSizeMb → WOOW_MAX_REQUEST_SIZE_MB), independent of
# the ChatGPT plugin.  Registered after UI hardening so it is installed last
# on the proxy app and rejects oversized input before every downstream layer.
if os.environ.get("WOOW_MAX_REQUEST_SIZE_MB") is not None:
    try:
        import woow_body_limit

        woow_body_limit.install_import_hook()
    except Exception as exc:  # noqa: BLE001 — start the proxy regardless
        print(f"woow sitecustomize: request body cap NOT installed: {exc!r}", file=sys.stderr)

if os.environ.get("WOOW_CHATGPT_ENABLED") == "true":
    try:
        import woow_chatgpt

        woow_chatgpt.install_import_hook()
    except Exception as exc:  # noqa: BLE001 — start the proxy regardless
        print(f"woow sitecustomize: ChatGPT sign-in guard NOT installed: {exc!r}", file=sys.stderr)
