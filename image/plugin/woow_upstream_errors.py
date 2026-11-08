"""WOOW — what a caller learns from a failed upstream call (chart 0.3.10, AUDITFIX litellm-retry-timeout).

LiteLLM 1.104.0 answers an upstream error with the upstream's status but renames every upstream
header to ``llm_provider-<name>``. OpenAI-compatible SDKs only read ``retry-after``,
``retry-after-ms`` and ``x-should-retry``, so an upstream 429 ``Retry-After: 60`` reached them as a
bare 429 and they guessed their own back-off (AI-FIX-LLM-1-1). And an upstream that does not answer
in ``request_timeout`` is answered as LiteLLM's ``litellm.Timeout`` = HTTP 408
``invalid_request_error``, which reads as "the caller was too slow".

``adjust(answer, original)`` runs on the proxy's answer (a ProxyException / HTTPException raised by
``ProxyBaseLLMRequestProcessing._handle_llm_api_exception``, wrapped by woow_chatgpt's import hook):

* for a retryable status (408, 409, 429, 5xx) it copies the upstream's ``retry-after`` (delay in
  seconds up to one day, or an HTTP date), ``retry-after-ms`` and ``x-should-retry`` (true/false)
  under their standard names. Malformed values are dropped; a value the gateway already set (the
  router cooldown's retry-after, the budget refusal's ``x-should-retry: false``) is never replaced;
  no other upstream header is promoted. ``WOOW_FORWARD_RETRY_AFTER=false`` switches this off.
* an upstream timeout (litellm.Timeout, 408) is answered with ``WOOW_UPSTREAM_TIMEOUT_STATUS``
  (default 504, ``error.type`` "timeout"); 408 restores LiteLLM's answer.

Never raises: any problem leaves LiteLLM's own answer as it was.
"""

from __future__ import annotations

import email.utils
import os
import re

FORWARD_ENV = "WOOW_FORWARD_RETRY_AFTER"
TIMEOUT_STATUS_ENV = "WOOW_UPSTREAM_TIMEOUT_STATUS"
DEFAULT_TIMEOUT_STATUS = 504
TIMEOUT_TYPE = "timeout"

# Statuses a later identical request can succeed with (the ones OpenAI's SDKs retry).
RETRYABLE_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
_PROVIDER_PREFIX = "llm_provider-"
_MAX_DELAY_S = 86400
_SECONDS = re.compile(r"\d{1,5}")
_MILLISECONDS = re.compile(r"\d{1,8}")
_HTTP_DATE = re.compile(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} GMT")


def forward_enabled() -> bool:
    return os.environ.get(FORWARD_ENV, "true").strip().lower() not in ("false", "0", "off", "no")


def timeout_status() -> int:
    raw = os.environ.get(TIMEOUT_STATUS_ENV, "").strip()
    if raw.isdigit() and 400 <= int(raw) <= 599:
        return int(raw)
    return DEFAULT_TIMEOUT_STATUS


def _status(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _retry_after(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if _SECONDS.fullmatch(value) and int(value) <= _MAX_DELAY_S:
        return value
    if _HTTP_DATE.fullmatch(value):
        try:
            email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        return value
    return None


def _retry_after_ms(value) -> str | None:
    if isinstance(value, str) and _MILLISECONDS.fullmatch(value.strip()) and int(value) <= _MAX_DELAY_S * 1000:
        return value.strip()
    return None


def _should_retry(value) -> str | None:
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower()
    return None


def _items(headers):
    try:
        return list(headers.items())
    except Exception:  # noqa: BLE001 — not a mapping
        return []


def upstream_headers(original, answer_headers=None) -> dict[str, str]:
    """The upstream's response headers (lower-case names) as far as LiteLLM kept them: on the
    exception (headers, response.headers, litellm_response_headers) or as llm_provider-* copies
    on the answer."""
    found: dict[str, str] = {}
    response = getattr(original, "response", None) if original is not None else None
    sources = [
        getattr(original, "headers", None) if original is not None else None,
        getattr(response, "headers", None) if response is not None else None,
        getattr(original, "litellm_response_headers", None) if original is not None else None,
        answer_headers,
    ]
    for source in sources:
        for key, value in _items(source or {}):
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            key = key.lower()
            if key.startswith(_PROVIDER_PREFIX):
                key = key[len(_PROVIDER_PREFIX):]
            found.setdefault(key, value)
    return found


def _is_timeout(original) -> bool:
    try:
        import litellm
        import openai
    except Exception:  # noqa: BLE001
        return False
    return isinstance(original, (litellm.Timeout, openai.APITimeoutError))


def _answer_status(answer) -> int | None:
    status = _status(getattr(answer, "code", None))
    return status if status is not None else _status(getattr(answer, "status_code", None))


def _map_timeout(answer, original) -> None:
    if not _is_timeout(original) or _answer_status(answer) != 408 or not hasattr(answer, "code"):
        return
    status = timeout_status()
    if status == 408:
        return
    answer.code = str(status)
    if _status(getattr(answer, "openai_code", None)) in (None, 408):
        answer.openai_code = str(status)
    answer.type = TIMEOUT_TYPE


def _forward_retry_hints(answer, original) -> None:
    if not forward_enabled() or _answer_status(answer) not in RETRYABLE_STATUSES:
        return
    headers = getattr(answer, "headers", None)
    if headers is None:
        headers = {}
        try:
            answer.headers = headers
        except Exception:  # noqa: BLE001
            return
    present = {k.lower() for k in headers if isinstance(k, str)}
    upstream = upstream_headers(original, headers)
    for name, check in (("retry-after", _retry_after), ("retry-after-ms", _retry_after_ms),
                        ("x-should-retry", _should_retry)):
        if name in present:
            continue
        value = check(upstream.get(name))
        if value is not None:
            headers[name] = value


def adjust(answer, original) -> None:
    """Fix up the proxy's answer to a failed request in place (see the module docstring)."""
    try:
        _map_timeout(answer, original)
        _forward_retry_hints(answer, original)
    except Exception as exc:  # noqa: BLE001 — LiteLLM's own answer stands
        import sys

        print(f"woow upstream errors: answer left unchanged: {exc!r}", file=sys.stderr)
