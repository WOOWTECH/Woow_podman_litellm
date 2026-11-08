"""Woow D1: client-visible auth and key errors never carry a virtual key's hash (its token id).

Installed as ``litellm/proxy/auth/woow_auth_errors.py`` by ``apply_upstream_guard.py``. Standard
library only (lean tests load this file by path).

LiteLLM 1.104.0 echoes the sha256 of the presented virtual key in some errors, e.g. a deleted or
unknown key: ``Received API Key = sk-...ppyA, Key Hash (Token) =<64 hex>``. Clients forward these
messages verbatim (agent replies, error headers, request dumps, backups), and the same text lands
in the spend log's ``error_information``. The hash is the key's database id: it works as the
``key`` argument of management endpoints, so it stays out of anything a client or an internal user
reads. The masked ``sk-...last4`` stays, so a caller can still tell which of its keys was refused.

The server log keeps the original text: callers scrub only after LiteLLM has logged the error.
Status code, error type, ``param``, headers and structured fields (such as a budget error's
``entity_id``, which spend logs store as ``error_budget_entity_id`` next to the row's own
``api_key`` hash) are not touched.
"""
import re
from typing import Final

REDACTED: Final = "[redacted]"
# Upstream's unknown-key message: drop the whole clause, the masked key before it already
# identifies the key for the caller.
_HASH_CLAUSE: Final = re.compile(r",\s*Key Hash \(Token\)\s*=\s*[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")
_HEX64: Final = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")


def scrub_key_hash(text):
    """``text`` without 64-hex values (key hashes); anything that is not a str is returned as is."""
    if not isinstance(text, str):
        return text
    return _HEX64.sub(REDACTED, _HASH_CLAUSE.sub("", text))


def _scrub_value(value):
    if isinstance(value, str):
        return scrub_key_hash(value)
    if isinstance(value, dict):
        return {k: _scrub_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(v) for v in value)
    return value


def scrub_auth_exception(exc):
    """Remove key hashes from ``exc``'s client-visible text, in place, and return ``exc``.

    Covers ``.message`` (ProxyException, LiteLLM exceptions), ``.detail`` (HTTPException, str or
    dict) and ``args`` (what ``str(exc)`` shows). The exception keeps its class and every other
    attribute, so callbacks, status mapping and spend-log fields other than the message behave as
    before.
    """
    if not isinstance(exc, BaseException):
        return exc
    for name in ("message", "detail"):
        try:
            value = getattr(exc, name, None)
        except Exception:  # noqa: BLE001  # a property that raises is left alone
            continue
        if isinstance(value, (str, dict, list)):
            scrubbed = _scrub_value(value)
            if scrubbed != value:
                try:
                    setattr(exc, name, scrubbed)
                except Exception:  # noqa: BLE001  # read-only attribute: args below still apply
                    pass
    if exc.args:
        scrubbed_args = _scrub_value(exc.args)
        if scrubbed_args != exc.args:
            exc.args = scrubbed_args
    return exc
