"""WOOW — keep an exhausted ChatGPT account out of the rotation for a while (chart 0.3.14, PLG-14.S03).

When a ChatGPT subscription runs out of its weekly quota, the backend answers HTTP 429::

    {"error": {"type": "usage_limit_reached", "message": "The usage limit has been reached",
               "plan_type": "pro", "resets_at": <epoch>, "limit_window_minutes": 10080,
               "resets_in_seconds": <seconds>}}

and sends no ``Retry-After``. LiteLLM's router then cools the deployment down for its default
``cooldown_time`` (5 s), after which the exhausted account is tried again — about once every
6-9 s for the whole reset window (hours to days): a wasted upstream call, and with
``numRetries: 0`` (chart 0.3.6) a client-visible 429 for the request that happens to pick it
(sandbox measurement 2026-10-24, evidence/PLG-14-sandbox-429).

LiteLLM already lets the *response header* choose the cooldown (router.deployment_callback_on_failure:
deployment cooldown_time > Retry-After header > router cooldown_time). ``with_cooldown_hint`` adds
that header: for a 429 whose body says ``usage_limit_reached`` and that carries no ``retry-after``
of its own, ``retry-after = min(cap, max(61, resets_in_seconds))`` seconds.

* ``WOOW_USAGE_LIMIT_COOLDOWN_MAX_SECONDS`` = cap, default 900 (values
  ``litellm.usageLimitCooldownMaxSeconds``); 0 = off; above one day is cut to one day (the limit
  woow_upstream_errors applies when it forwards ``retry-after`` to the caller).
* The floor is 61, not 60: LiteLLM sleeps a ``Retry-After`` of 60 s or less in-request before a
  retry (utils._calculate_retry_after), a larger one falls back to its short exponential back-off.
  Only a cap below 61 (an operator's explicit choice) can still produce a sleep.
* ``resets_in_seconds`` missing / not a positive number: ``resets_at - now`` if that is usable,
  otherwise the floor (the account is exhausted, the reset time is just unknown).
* An upstream ``retry-after`` (any case) is never replaced; other 429s, other statuses and bodies
  that are not JSON are returned untouched; the input headers are never mutated.
* The hint travels like any upstream header: the router reads it for the cooldown and
  woow_upstream_errors forwards it to the caller as ``retry-after`` (a number of seconds, nothing
  else of the upstream body or the account).

Never raises: any problem returns the headers as they were.
"""

from __future__ import annotations

import json
import math
import os
import time

MAX_ENV = "WOOW_USAGE_LIMIT_COOLDOWN_MAX_SECONDS"
DEFAULT_MAX_SECONDS = 900
MIN_HINT_SECONDS = 61
HARD_MAX_SECONDS = 86400
ERROR_TYPE = "usage_limit_reached"


def max_seconds() -> int:
    """The cap in seconds (0 = off). Unset or malformed -> the default."""
    raw = os.environ.get(MAX_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_SECONDS
    if raw.isdigit():
        return min(int(raw), HARD_MAX_SECONDS)
    return DEFAULT_MAX_SECONDS


def _error(error_message) -> dict | None:
    if isinstance(error_message, (bytes, bytearray)):
        try:
            error_message = bytes(error_message).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(error_message, str):
        return None
    try:
        body = json.loads(error_message)
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else None


def _positive_number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def hint_seconds(status_code, error_message, now: float | None = None) -> int | None:
    """Seconds for the Retry-After hint, or None when this answer needs none."""
    cap = max_seconds()
    if cap <= 0 or status_code != 429:
        return None
    error = _error(error_message)
    if error is None or ERROR_TYPE not in (error.get("type"), error.get("code")):
        return None
    seconds = _positive_number(error.get("resets_in_seconds"))
    if seconds is None:
        at = _positive_number(error.get("resets_at"))
        if at is not None:
            seconds = _positive_number(at - (time.time() if now is None else now))
    wanted = math.ceil(seconds) if seconds is not None else MIN_HINT_SECONDS
    return min(cap, max(MIN_HINT_SECONDS, wanted))


def with_cooldown_hint(status_code, error_message, headers):
    """``headers`` plus ``retry-after`` when the answer is an exhausted-quota 429 without one."""
    try:
        seconds = hint_seconds(status_code, error_message)
        if seconds is None:
            return headers
        existing = dict(headers) if headers else {}
        if any(isinstance(k, str) and k.lower() == "retry-after" for k in existing):
            return headers
        existing["retry-after"] = str(seconds)
        return existing
    except Exception:  # noqa: BLE001 — LiteLLM's own error stands
        return headers
