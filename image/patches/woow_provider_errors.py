"""Woow client-safe provider errors and provider headers for pinned LiteLLM 1.104.0.

Installed as ``litellm/litellm_core_utils/woow_provider_errors.py`` by
``apply_upstream_guard.py``. Standard library only at import time, so the patched upstream
modules can import it lazily from any layer, and lean tests load it by path; the server-log
redaction also runs LiteLLM's ``secret_redaction.redact_string`` when that module is loaded.

OpenRouter error bodies carry the gateway's own OpenRouter account (``user_id``), the
upstream provider's raw answer (``error.metadata.raw``), BYOK flags, flagged input and
provider headers. LiteLLM copies that text into the client error, the streaming error frame
and the spend log, which internal users can read. Everything here rebuilds such a body as
``{"error":{"message":M,"code":C}}`` with a status-dependent ``M`` and replaces account,
organisation, project, request, generation and key identifiers. The original text only goes
to the server log: one ERROR record per scrubbed error (ERROR so that ``LITELLM_LOG=ERROR``
keeps it), secrets redacted first, at most 2000 characters; ``WOOW_PROVIDER_ERROR_LOG=off``
drops that record. It never reaches the client, the spend log or response headers.

An error is treated as OpenRouter's when the provider is ``openrouter`` or when its body has
OpenRouter's shape: a top-level ``user_id`` next to an ``error`` object (``openai/`` routes
pointed at OpenRouter). The body may be JSON or the Python ``repr`` the OpenAI SDK puts in
``Error code: 403 - {...}``. Any other provider's text is returned unchanged.

Every function is idempotent: a router or text-completion wrapper may scrub the same text
again without changing it.
"""
import ast
import contextvars
import json
import logging
import os
import re
import sys
from typing import Final
from urllib.parse import urlsplit

_LOG: Final = logging.getLogger("LiteLLM")

MSG_401: Final = (
    "The proxy's upstream credentials were rejected. Your API key is valid; contact the proxy administrator."
)
MSG_402: Final = "The upstream provider declined the request for a billing reason. Contact the proxy administrator."
MSG_403: Final = "The upstream provider refused the request. Contact the proxy administrator."
MSG_403_FLAGGED: Final = "The input was flagged by upstream moderation ({reasons})."
MSG_TIMEOUT: Final = "The upstream provider timed out."
MSG_429: Final = "The upstream provider is rate limiting this model. Retry later."
GENERIC_STREAM_MESSAGE: Final = "The upstream provider stream failed."

_FLAGGED_PREFIX: Final = MSG_403_FLAGGED.split("{", 1)[0]
_KEEP_MESSAGE: Final = frozenset({400, 404, 413, 422})
_NESTED_FOR: Final = frozenset({400, 413, 422})
_NESTED_LIMIT: Final = 300
_REASONS_LIMIT: Final = 5
_REASON_LIMIT: Final = 64
_LOG_LIMIT: Final = 2000  # characters of the redacted original kept in the server log
_PROVIDER_ERROR_MARK: Final = "Provider returned error"
_BODY_LIMIT: Final = 100_000  # longest body parsed as a Python literal (OpenAI SDK repr)
_HEALTH_ERROR_LIMIT: Final = 500
_HEALTH_HIDDEN_FIELDS: Final = frozenset({"raw_request_typed_dict"})

REDACTED: Final = "[redacted]"
REDACTED_URL: Final = "[redacted-url]"
_ID_RULES: Final = (
    (re.compile(r"https?://[^\s\"'<>]*openrouter\.ai[^\s\"'<>]*", re.I), REDACTED_URL),
    (re.compile(r"\buser_[A-Za-z0-9]{6,}"), REDACTED),
    (re.compile(r"\borg[-_][A-Za-z0-9]{6,}"), REDACTED),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), REDACTED),
    (re.compile(r"\bprojects/[0-9]{4,}"), "projects/" + REDACTED),
    (re.compile(r"\b(?:req|gen|msg|chatcmpl)[_-][A-Za-z0-9-]{6,}"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9-]{8,}"), REDACTED),
    (re.compile(r"\b[0-9a-f]{32,}\b"), REDACTED),
)

# Credentials the server log must never hold, applied after LiteLLM's own redact_string (which lean
# tests do not load): sk- keys, Bearer tokens, JWTs and api_key values.
_SECRET_RULES: Final = (
    (re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]+=*", re.I), "Bearer " + REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"""((?:x-)?api[_-]?key['"]?\s*[:=]\s*['"]?)[^\s,;&'"})\[\]{}>]+""", re.I), r"\g<1>" + REDACTED),
)

PROVIDER_ERROR_LOG_ENV: Final = "WOOW_PROVIDER_ERROR_LOG"
_PROVIDER_ERROR_LOG_MODES: Final = ("raw", "off")
# Text of the last record for the exception being mapped: exception_type() scrubs error_str, then
# its OpenAI-compatible mapping scrubs the same exception's message again.
_LOGGED: Final = contextvars.ContextVar("woow_provider_error_logged", default=None)
# Body stream_error_parts() rebuilt (and logged the original of); mapping it again is not a new error.
_EMITTED: Final = contextvars.ContextVar("woow_provider_error_emitted", default=None)

PROVIDER_HEADERS_ENV: Final = "WOOW_PROVIDER_HEADERS"
_PROVIDER_HEADER_PREFIX: Final = "llm_provider-"
_ALLOWED_PROVIDER_HEADER_PREFIXES: Final = ("x-ratelimit-",)
_ALLOWED_PROVIDER_HEADERS: Final = frozenset({"retry-after"})


def redact_ids(text: str) -> str:
    """Replace account, organisation, request, generation and key identifiers and OpenRouter URLs."""
    for pattern, replacement in _ID_RULES:
        text = pattern.sub(replacement, text)
    return text


def _status(status: object, code: object) -> int | None:
    for value in (status, code):
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and 100 <= value <= 599:
            return value
        if isinstance(value, str) and value.isdigit() and 100 <= int(value) <= 599:
            return int(value)
    return None


def _nested_message(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    try:
        nested = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(nested, dict):
        return None
    inner = nested.get("error")
    message = inner.get("message") if isinstance(inner, dict) else (inner if isinstance(inner, str) else nested.get("message"))
    if not isinstance(message, str) or not message.strip():
        return None
    return message.strip()[:_NESTED_LIMIT]


def _flagged(reasons: list) -> str:
    return MSG_403_FLAGGED.format(reasons=", ".join(str(reason)[:_REASON_LIMIT] for reason in reasons[:_REASONS_LIMIT]))


def _client_message(status: int | None, message: str, metadata: dict) -> str:
    if status == 401:
        return MSG_401
    if status == 402:
        return MSG_402
    if status == 403:
        if message.startswith(_FLAGGED_PREFIX):
            return message  # already scrubbed: the reasons are gone from the rebuilt body
        reasons = metadata.get("reasons")
        if isinstance(reasons, list) and reasons:
            return _flagged(reasons)  # never the flagged input itself
        return MSG_403
    if status in (408, 504):
        return MSG_TIMEOUT
    if status == 429:
        return MSG_429
    if status in _NESTED_FOR and message == _PROVIDER_ERROR_MARK:
        nested = _nested_message(metadata.get("raw"))
        if nested is not None:
            return message + ": " + nested
    return message  # 400/404/413/422, other 4xx and 5xx keep the provider's own message


def _body_parts(body: dict) -> tuple[str, object, dict]:
    error = body.get("error")
    if isinstance(error, dict):
        message, code, metadata = error.get("message"), error.get("code"), error.get("metadata")
    elif isinstance(error, str):
        message, code, metadata = error, body.get("code"), None
    else:
        message, code, metadata = body.get("message"), body.get("code"), None
    message = "" if message is None else message if isinstance(message, str) else json.dumps(message)
    code = code if isinstance(code, (int, str)) or code is None else None
    return message, code, metadata if isinstance(metadata, dict) else {}


def _object_end(text: str, start: int) -> int | None:
    """Index after the ``{...}`` opened at ``start`` (JSON or Python repr quoting), else None."""
    depth, quote, escaped = 0, "", False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _find_body(text: str) -> tuple[int, int, dict] | None:
    """The first ``{...}`` in ``text`` as (start, end, dict): JSON, else a Python literal."""
    start = text.find("{")
    if start < 0:
        return None
    try:
        body, end = json.JSONDecoder().raw_decode(text, start)
    except ValueError:
        end = _object_end(text, start)
        if end is None or end - start > _BODY_LIMIT:
            return None
        try:
            body = ast.literal_eval(text[start:end])
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            return None
    return (start, end, body) if isinstance(body, dict) else None


def _openrouter_shaped(body: dict) -> bool:
    """OpenRouter's error body: ``{"error": {...}, "user_id": ...}`` (never Anthropic's ``{"type": "error"}``)."""
    return "user_id" in body and isinstance(body.get("error"), dict) and body.get("type") != "error"


def _rebuild(text: str, status: object, found: tuple[int, int, dict] | None) -> str:
    if found is None:
        return redact_ids(text)
    start, end, body = found
    message, code, metadata = _body_parts(body)
    client = _client_message(_status(status, code), message, metadata)
    rebuilt = json.dumps({"error": {"message": redact_ids(client), "code": code}}, separators=(",", ":"), ensure_ascii=False)
    return redact_ids(text[:start]) + rebuilt + redact_ids(text[end:])


def provider_error_log_mode() -> str:
    """``WOOW_PROVIDER_ERROR_LOG``: ``raw`` (default) or ``off``; anything else logs an error and uses raw."""
    raw = os.environ.get(PROVIDER_ERROR_LOG_ENV)
    if raw is None or raw.strip() == "":
        return _PROVIDER_ERROR_LOG_MODES[0]
    value = raw.strip().lower()
    if value in _PROVIDER_ERROR_LOG_MODES:
        return value
    _LOG.error("Woow: invalid %s=%r; using %s", PROVIDER_ERROR_LOG_ENV, raw, _PROVIDER_ERROR_LOG_MODES[0])
    return _PROVIDER_ERROR_LOG_MODES[0]


def redact_secrets(text: str) -> str:
    """``text`` without credentials: LiteLLM's secret redaction when loaded, then ``_SECRET_RULES``.

    exception_mapping_utils imports ``secret_redaction`` at load time, so in the proxy it is
    always there; looking it up in ``sys.modules`` never imports LiteLLM (lean tests).
    """
    litellm_redaction = sys.modules.get("litellm.litellm_core_utils.secret_redaction")
    redact_string = getattr(litellm_redaction, "redact_string", None)
    if callable(redact_string):
        try:
            text = redact_string(text)
        except Exception:  # noqa: BLE001 - never let logging break error mapping; the local rules still apply
            pass
    for pattern, replacement in _SECRET_RULES:
        text = pattern.sub(replacement, text)
    return text


def server_log_text(raw: str) -> str:
    """What the server log keeps of an original provider error: secrets redacted, then truncated."""
    return redact_secrets(raw)[:_LOG_LIMIT]


def _log_scrubbed(provider: object, status: object, raw: str) -> None:
    """One ERROR record holding the original text, so LITELLM_LOG=ERROR keeps it (server log only)."""
    if provider_error_log_mode() == "off":
        return
    emitted = _EMITTED.get()
    if emitted is not None and emitted in raw:
        return  # a mid-stream error stream_error_parts() already logged, mapped again
    text = server_log_text(raw)
    if _LOGGED.get() == text:
        return  # the same exception, mapped a second time
    _LOGGED.set(text)
    _LOG.error("Woow upstream error scrubbed (provider=%s status=%s) raw=%s", provider, status, text)


def scrub_provider_error(provider: object, error_str: str, status: object = None) -> str:
    """Client-safe text of a provider error; other providers' text is returned unchanged.

    ``error_str`` is LiteLLM's (already secret-redacted) provider error text. OpenRouter
    errors, and OpenRouter-shaped bodies on other providers (``openai/`` routes pointed at
    OpenRouter, JSON or the OpenAI SDK's Python repr), are rebuilt; the original goes to the
    server log only. Each call starts a new error for the one-record-per-error log rule.
    """
    _LOGGED.set(None)
    return _scrub(provider, error_str, status)


def _scrub(provider: object, error_str: str, status: object) -> str:
    if not isinstance(error_str, str):
        return error_str
    found = _find_body(error_str)
    if provider != "openrouter" and (found is None or not _openrouter_shaped(found[2])):
        return error_str
    scrubbed = _rebuild(error_str, status, found)
    if scrubbed != error_str:
        _log_scrubbed(provider, status, error_str)
    return scrubbed


def scrub_compatible_message(provider: object, message: object, exception: object) -> object:
    """Client message for LiteLLM's OpenAI-compatible mapping (``openai/``, text completion, embeddings).

    That mapping shows either the SDK's whole ``Error code: N - {...}`` text or only the inner
    ``error.message``; ``user_id`` and ``metadata`` only appear in the exception's full text.
    When that full text is OpenRouter's, the status policy applies here too.
    """
    if not isinstance(message, str):
        return message
    status = getattr(exception, "status_code", None)
    inline = _find_body(message)
    if inline is not None and (provider == "openrouter" or _openrouter_shaped(inline[2])):
        return _scrub(provider, message, status)
    if provider == "openrouter":
        return _scrub(provider, message, status)
    for full in (getattr(exception, "message", None), str(exception)):
        found = _find_body(full) if isinstance(full, str) else None
        if found is None or not _openrouter_shaped(found[2]):
            continue
        body_message, code, metadata = _body_parts(found[2])
        client = redact_ids(_client_message(_status(status, code), body_message, metadata))
        if client != message:
            _log_scrubbed(provider, status, full)
        return client
    return message


def stream_error_parts(error_chunk: object) -> tuple[str, int]:
    """Message and HTTP status for an OpenRouter mid-stream ``{"error": {...}}`` chunk.

    Only ``message`` and ``code`` survive; ``metadata`` (raw provider text, provider headers)
    and ``user_id`` are dropped, and the original chunk goes to the server log only. A
    non-numeric code (``"server_error"``) maps to 500 and ``rate_limit*`` to 429 instead of
    crashing on ``int()``.
    """
    chunk = error_chunk if isinstance(error_chunk, dict) else {}
    message = chunk.get("message")
    code = chunk.get("code")
    if isinstance(code, bool) or not isinstance(code, (int, str)):
        code = None
    status = _status(None, code)
    if status is None or status < 400:
        status = 429 if isinstance(code, str) and code.lower().startswith("rate_limit") else 500
    text = GENERIC_STREAM_MESSAGE if message is None or message == "" else str(message)
    body = {"error": {"message": redact_ids(text), "code": code}}
    rebuilt = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    if set(chunk) - {"message", "code"} or body["error"]["message"] != message:
        _LOGGED.set(None)
        _log_scrubbed("openrouter", status, json.dumps(chunk, ensure_ascii=False, default=str))
        _EMITTED.set(rebuilt)
    return rebuilt, status


def client_safe_stream_message(message: object) -> str:
    """Second line of defence for SSE error events (Anthropic ``/v1/messages`` adapter).

    An OpenRouter-shaped body is rebuilt; any other provider's text keeps its own fields and
    only loses identifiers (``user_``/``org-`` ids, request ids, keys, OpenRouter URLs).
    """
    if not isinstance(message, str):
        message = "" if message is None else str(message)
    found = _find_body(message)
    if found is not None and _openrouter_shaped(found[2]):
        return _rebuild(message, None, found)
    return redact_ids(message)


def _health_error(error: str, hidden: list[str]) -> str:
    """First line of a health-check error, without the deployment's api_base, host or identifiers."""
    lines = error.strip().splitlines()
    line = lines[0] if lines else ""
    for value in hidden:
        host = urlsplit(value).hostname if "://" in value else None
        for secret in (value, value.rstrip("/"), host):
            if secret:
                line = line.replace(secret, REDACTED_URL)
    return redact_ids(line)[:_HEALTH_ERROR_LIMIT]


def health_endpoint_for_non_admin(endpoint: dict, admin_only: object) -> dict:
    """/health endpoint entry for a non-admin caller: no routing fields, raw request or stack trace."""
    drop = set(admin_only) | _HEALTH_HIDDEN_FIELDS
    hidden = [str(endpoint[key]) for key in sorted(set(admin_only)) if isinstance(endpoint.get(key), str) and endpoint[key]]
    out = {key: value for key, value in endpoint.items() if key not in drop}
    if isinstance(out.get("error"), str):
        out["error"] = _health_error(out["error"], hidden)
    return out


def provider_header_allowed(name: str) -> bool:
    """Forward only rate-limit and retry-after provider headers (``WOOW_PROVIDER_HEADERS=all`` restores all)."""
    if os.environ.get(PROVIDER_HEADERS_ENV, "allowlist").strip().lower() == "all":
        return True
    key = name.lower()
    if key.startswith(_PROVIDER_HEADER_PREFIX):
        key = key[len(_PROVIDER_HEADER_PREFIX):]
    return key in _ALLOWED_PROVIDER_HEADERS or key.startswith(_ALLOWED_PROVIDER_HEADER_PREFIXES)
