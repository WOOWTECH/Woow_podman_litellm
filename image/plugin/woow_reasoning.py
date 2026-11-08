"""WOOW — reasoning models and small output limits (AI-LLM-003-1 / AI-FIX-LLM-2).

A reasoning model spends part of ``max_tokens`` on hidden reasoning. With a small limit the whole
budget goes into reasoning and the reply is ``200``, ``content: ""``, ``finish_reason: "length"`` —
no error, no hint (qa-reasoning = gpt-6-luna, ``max_tokens=16``: 4 of 4 requests empty, 32+ fine).
This module is called by ``woow_chatgpt._patch_proxy_logging`` in front of LiteLLM's proxy
pre-call hook (where request values are still the caller's) and after its success hook:

* pre-call  — a request to a reasoning route whose output limit is below the floor gets
  (mode ``raise``, the default) the limit raised to the floor, or (mode ``reject``) a 400
  ``invalid_request_error`` that names the parameter and the minimum. The limit is only a cap, so
  replies that finish earlier cost the same; ``off`` leaves requests and responses exactly as
  they were (also no headers);
* post-call — a raised limit is announced with ``x-woow-max-tokens-raised: <old>-><new>``; a
  response that is empty with ``finish_reason: length`` anyway (a route this module did not
  recognise as reasoning) gets ``x-woow-warning: reasoning_exhausted_max_tokens``.

What counts as a reasoning route (a model group's deployments; any one is enough):
1. ``model_info.woow_min_output_tokens`` — an integer set by the admin: ``0`` = never touch this
   route, ``N`` = reasoning route with floor N (beats everything below, except that a request or
   route that turns reasoning off is still left alone);
2. ``model_info.supports_reasoning: true`` (not for Claude/Gemini families, where reasoning is
   opt-in), or an explicit request/route ``reasoning`` / ``reasoning_effort`` that is not off;
3. the provider model id matches a known always-reasoning family (OpenAI o-series, gpt-5+,
   DeepSeek R1/reasoner, QwQ, ``…:thinking``, Grok 4+, Magistral) and does not match a
   non-reasoning marker — extra patterns: ``WOOW_REASONING_MODEL_PATTERNS`` (comma separated
   regular expressions).
Requests that carry ``thinking`` (Anthropic style, the caller sizes ``budget_tokens`` itself),
or that switch reasoning off (``reasoning.enabled=false``, ``reasoning.effort=none``,
``reasoning_effort=none``), are never touched; so are routes that default reasoning off.

Settings (environment, read at every request; the chart maps them from values litellm.reasoning*):
  WOOW_REASONING_MIN_OUTPUT_TOKENS  floor, default 512; 0 turns the feature off
  WOOW_REASONING_MIN_OUTPUT_MODE    raise (default) | reject | off
  WOOW_REASONING_MODEL_PATTERNS     extra provider-model regexes

Everything here is best effort: an unexpected error never fails a request.
"""

from __future__ import annotations

import contextvars
import os
import re
import sys

PROXY_TARGET = "litellm.proxy.proxy_server"
DEFAULT_FLOOR = 512
OUTPUT_LIMIT_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")

# Always-reasoning families (matched on the provider model id, lower case). Conservative on
# purpose: a miss only means "not recognised", the admin can set model_info.woow_min_output_tokens.
_REASONING_PATTERNS = (
    r"(^|[/:._-])o[1-9][0-9]*($|[/:._-])",       # OpenAI o1 / o3 / o4-mini …
    r"(^|[/:._-])gpt-?[5-9]($|[/:._-]|\d)",       # gpt-5 …, gpt-6-luna, gpt-6-astra
    r"deepseek-(r1|reasoner)",
    r"(^|[/:._-])qwq($|[/:._-])",
    r"[:_-]thinking($|[/:._-])",
    r"(^|[/:._-])grok-[4-9]",
    r"magistral",
)
_NOT_REASONING_PATTERNS = (r"non-?reasoning", r"no-?think", r"chat-latest", r"-instant", r"-chat($|[/:._-])")
# Reasoning is opt-in for these: `supports_reasoning` alone says nothing about the default.
_OPT_IN_FAMILIES = re.compile(r"claude|gemini|anthropic/|vertex_ai/")

# Set by the pre-call step for the same request's post-call step (same asyncio task).
_RAISED: contextvars.ContextVar = contextvars.ContextVar("woow_reasoning_raised", default=None)


def _log(msg: str) -> None:
    print(f"woow-reasoning: {msg}", file=sys.stderr, flush=True)


# ── settings ──────────────────────────────────────────────────────────────────
def settings() -> tuple[str, int]:
    """(mode, default floor). mode is "off" when the feature is switched off."""
    mode = (os.environ.get("WOOW_REASONING_MIN_OUTPUT_MODE") or "raise").strip().lower()
    if mode not in ("raise", "reject", "off"):
        mode = "raise"
    raw = os.environ.get("WOOW_REASONING_MIN_OUTPUT_TOKENS")
    try:
        floor = int(raw) if raw not in (None, "") else DEFAULT_FLOOR
    except ValueError:
        floor = DEFAULT_FLOOR
    if floor <= 0:
        mode = "off"
    return mode, max(floor, 0)


def _extra_patterns() -> list:
    out = []
    for item in (os.environ.get("WOOW_REASONING_MODEL_PATTERNS") or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            out.append(re.compile(item, re.I))
        except re.error:
            _log(f"WARNING ignoring invalid WOOW_REASONING_MODEL_PATTERNS entry (len {len(item)})")
    return out


# ── reasoning on / off, as the request or the route says ──────────────────────
def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _reasoning_state(source) -> str | None:
    """"off" / "on" / None (silent) for a request body or a route's litellm_params. Looks at the
    OpenRouter-style ``reasoning`` object, ``reasoning_effort`` and the same inside ``extra_body``."""
    if not isinstance(source, dict):
        return None
    state = None
    for holder in (source, source.get("extra_body") if isinstance(source.get("extra_body"), dict) else None):
        if holder is None:
            continue
        reasoning = holder.get("reasoning")
        if isinstance(reasoning, dict):
            if reasoning.get("enabled") is False or str(reasoning.get("effort") or "").lower() == "none":
                return "off"
            if reasoning.get("enabled") is True or reasoning.get("effort") or _is_int(reasoning.get("max_tokens")):
                state = "on"
        effort = holder.get("reasoning_effort")
        if isinstance(effort, str) and effort:
            if effort.lower() == "none":
                return "off"
            state = "on"
    return state


def _model_id(params: dict) -> str:
    return str((params or {}).get("model") or "").lower()


def _deployment_floor(deployment: dict, request: dict, default_floor: int) -> int:
    """Floor this deployment asks for, 0 = leave the request alone."""
    params = deployment.get("litellm_params") if isinstance(deployment.get("litellm_params"), dict) else {}
    info = deployment.get("model_info") if isinstance(deployment.get("model_info"), dict) else {}
    if _reasoning_state(params) == "off":          # the route turns reasoning off by default
        return 0
    explicit = info.get("woow_min_output_tokens")
    if _is_int(explicit):
        return max(explicit, 0)
    model = _model_id(params)
    if _reasoning_state(params) == "on" or _reasoning_state(request) == "on":
        return default_floor
    if info.get("supports_reasoning") is True and not _OPT_IN_FAMILIES.search(model):
        return default_floor
    if any(re.search(p, model) for p in _NOT_REASONING_PATTERNS):
        return 0
    if any(re.search(p, model) for p in _REASONING_PATTERNS) or any(p.search(model) for p in _extra_patterns()):
        return default_floor
    return 0


def _deployments(model_group) -> list:
    router = getattr(sys.modules.get(PROXY_TARGET), "llm_router", None)
    if router is None or not isinstance(model_group, str):
        return []
    out = []
    for d in list(getattr(router, "model_list", None) or []):
        if isinstance(d, dict) and d.get("model_name") == model_group:
            out.append(d)
    return out


def decide(data: dict) -> tuple[int, list]:
    """(floor, [(field, value)]) — the floor that applies to this request and the output-limit
    fields of the body that are below it. floor 0 / empty list = nothing to do."""
    mode, default_floor = settings()
    if mode == "off" or not isinstance(data, dict):
        return 0, []
    if data.get("thinking") is not None or _reasoning_state(data) == "off":
        return 0, []
    deployments = _deployments(data.get("model"))
    if not deployments:
        return 0, []
    floor = max((_deployment_floor(d, data, default_floor) for d in deployments), default=0)
    if floor <= 0:
        return 0, []
    low = [(f, data[f]) for f in OUTPUT_LIMIT_FIELDS if _is_int(data.get(f)) and 0 < data[f] < floor]
    return floor, low


# ── hooks (called from woow_chatgpt) ──────────────────────────────────────────
def pre_call(data) -> None:
    """Raise (or refuse) a too-small output limit of a reasoning route; mutates ``data``."""
    _RAISED.set(None)
    if not isinstance(data, dict):
        return
    try:
        floor, low = decide(data)
    except Exception as exc:  # noqa: BLE001 — never break a request over this
        _log(f"WARNING reasoning floor skipped: {type(exc).__name__}")
        return
    if not low:
        return
    mode, _ = settings()
    field, value = low[0]
    if mode == "reject":
        from litellm.proxy._types import ProxyException

        raise ProxyException(
            message=(f"{field}={value} is too small for reasoning model '{data.get('model')}': its reasoning can use "
                     f"the whole limit and leave the reply empty. Send {field} >= {floor}, or turn reasoning off "
                     "for this request (reasoning.enabled=false or reasoning_effort=none)."),
            type="invalid_request_error", param=field, code=400)
    for f, _v in low:
        data[f] = floor
    _RAISED.set((value, floor))


def post_call(response) -> None:
    """Add the explanation headers to a non-streaming response (hidden_params.additional_headers)."""
    raised = _RAISED.get()
    try:
        if settings()[0] == "off":
            return
        headers = {}
        if raised:
            headers["x-woow-max-tokens-raised"] = f"{raised[0]}->{raised[1]}"
        elif _empty_and_truncated(response):
            headers["x-woow-warning"] = "reasoning_exhausted_max_tokens"
        if not headers:
            return
        hidden = getattr(response, "_hidden_params", None)
        if not isinstance(hidden, dict):
            return
        extra = hidden.get("additional_headers")
        if not isinstance(extra, dict):
            extra = {}
            hidden["additional_headers"] = extra
        extra.update(headers)
    except Exception as exc:  # noqa: BLE001
        _log(f"WARNING reasoning headers skipped: {type(exc).__name__}")


def _empty_and_truncated(response) -> bool:
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        return False
    for choice in choices:
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        if getattr(choice, "finish_reason", None) != "length" or (content not in (None, "")):
            return False
        if getattr(message, "tool_calls", None):
            return False
    return True
