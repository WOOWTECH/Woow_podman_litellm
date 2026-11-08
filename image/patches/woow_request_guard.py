"""Woow output-token limits and budget admission for pinned LiteLLM 1.104.0.

Installed as ``litellm/proxy/woow_request_guard.py`` by ``apply_upstream_guard.py``. The pure
functions use the standard library only (lean tests load this file by path); LiteLLM's
``ProxyException`` and ``woow_api_errors.mark_rejected_before_upstream`` are imported lazily,
only when a request is refused.

- Output token limits (``max_tokens``, ``max_completion_tokens``, ``max_output_tokens``) must be
  null or an integer from 1 to 2**31-1 (an integral float such as 16.0 is accepted unchanged);
  anything else is an OpenAI-style 400 before any hook, spend log or provider call.
  ``WOOW_TOKEN_LIMIT_VALIDATION=enforce`` (default) | ``warn`` | ``off``.
- Budget reservations of reasoning models reserve at least R output tokens (the client's
  max_tokens does not bound hidden reasoning on OpenRouter) and a minimum input size (hidden
  provider prompt), unless the body explicitly disables reasoning (``reasoning.enabled: false``,
  ``reasoning.effort``/``reasoning_effort: none``, ``reasoning.max_tokens: 0`` or Anthropic's
  ``thinking.type: disabled``). R comes from ``model_info.woow_reasoning_output_reserve_tokens``,
  else ``WOOW_BUDGET_REASONING_RESERVE_TOKENS``, else 2048; the input floor from
  ``model_info.woow_min_input_tokens``, else ``WOOW_BUDGET_MIN_INPUT_TOKENS``, else 256.
- ``WOOW_BUDGET_ADMISSION=strict`` (default) refuses a request when the remaining budget cannot
  cover the part of it the client's max_tokens does not bound: the input plus, for a reasoning
  request, the reasoning reserve R (the estimate with max_tokens taken as 1). A request that
  fits that floor but not its full estimate is admitted with the reservation shrunk to the
  remaining budget, as upstream does. ``full`` refuses every estimate above the remaining
  budget (no max_tokens counts as 16384 tokens); ``resize`` restores upstream.
- ``WOOW_BUDGET_UNPRICED=allow`` (default) logs a budget-limited key using a model without a
  price; ``block_budgeted_keys`` refuses it with a 400.
"""
from collections.abc import Iterable, Mapping
import logging
import math
import os
from typing import Final

_LOG: Final = logging.getLogger("LiteLLM")

TOKEN_LIMIT_ENV: Final = "WOOW_TOKEN_LIMIT_VALIDATION"
RESERVE_ENV: Final = "WOOW_BUDGET_REASONING_RESERVE_TOKENS"
MIN_INPUT_ENV: Final = "WOOW_BUDGET_MIN_INPUT_TOKENS"
ADMISSION_ENV: Final = "WOOW_BUDGET_ADMISSION"
UNPRICED_ENV: Final = "WOOW_BUDGET_UNPRICED"

DEFAULT_REASONING_RESERVE: Final = 2048
DEFAULT_MIN_INPUT: Final = 256
TOKEN_LIMIT_MIN: Final = 1
TOKEN_LIMIT_MAX: Final = 2**31 - 1

# Output token limits per proxy route type. OpenAI Responses requires >= 16, but LiteLLM raises
# 1..15 to 16 itself (client warm-up probes), so only values below 1 are refused there too.
TOKEN_LIMIT_PARAMS: Final[Mapping[str, tuple[str, ...]]] = {
    "acompletion": ("max_tokens", "max_completion_tokens"),
    "atext_completion": ("max_tokens",),
    "aresponses": ("max_output_tokens",),
    "anthropic_messages": ("max_tokens",),
}
MODEL_OVERRIDE_KEYS: Final = ("woow_reasoning_output_reserve_tokens", "woow_min_input_tokens")


def _choice(env: str, allowed: tuple[str, ...]) -> str:
    raw = os.environ.get(env)
    if raw is None or raw.strip() == "":
        return allowed[0]
    value = raw.strip().lower()
    if value in allowed:
        return value
    _LOG.warning("Woow: invalid %s=%r; using %s", env, raw, allowed[0])
    return allowed[0]


def token_limit_mode() -> str:
    return _choice(TOKEN_LIMIT_ENV, ("enforce", "warn", "off"))


def budget_admission_mode() -> str:
    return _choice(ADMISSION_ENV, ("strict", "full", "resize"))


def unpriced_policy() -> str:
    return _choice(UNPRICED_ENV, ("allow", "block_budgeted_keys"))


def _kind(value: object) -> str:
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, float):
        return "a decimal number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, Mapping):
        return "an object"
    if isinstance(value, (list, tuple)):
        return "an array"
    return "an unsupported value"


def token_limit_problem(param: str, value: object) -> str | None:
    """OpenAI's own wording for an invalid output-token limit; None when the value is acceptable."""
    if value is None:
        return None
    number = value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        number = int(value)
    if isinstance(number, bool) or not isinstance(number, int):
        return f"Invalid type for '{param}': expected an integer, but got {_kind(value)} instead."
    if number < TOKEN_LIMIT_MIN:
        return (
            f"Invalid '{param}': integer below minimum value. "
            f"Expected a value >= {TOKEN_LIMIT_MIN}, but got {number} instead."
        )
    if number > TOKEN_LIMIT_MAX:
        return (
            f"Invalid '{param}': integer above maximum value. "
            f"Expected a value <= {TOKEN_LIMIT_MAX}, but got {number} instead."
        )
    return None


_ERROR_CLASS: list = []


def _request_guard_error(message: str, param: str) -> Exception:
    if not _ERROR_CLASS:
        from litellm.proxy._types import ProxyException

        class WoowRequestGuardError(ProxyException):
            """400 invalid_request_error raised before any provider call."""

        _ERROR_CLASS.append(WoowRequestGuardError)
    error = _ERROR_CLASS[0](message=message, type="invalid_request_error", param=param, code=400)
    error.status_code = 400  # handlers that map status_code (e.g. /v1/completions) keep the 400
    from litellm.proxy.woow_api_errors import mark_rejected_before_upstream

    mark_rejected_before_upstream(error)  # no spend log row: no provider was called
    return error


def validate_token_limits(route_type: str, data: Mapping[str, object]) -> None:
    """Refuse invalid output-token limits on the body exactly as the client sent it."""
    params = TOKEN_LIMIT_PARAMS.get(route_type, ())
    if not params:
        return
    mode = token_limit_mode()
    if mode == "off":
        return
    for param in params:
        problem = token_limit_problem(param, data.get(param))
        if problem is None:
            continue
        if mode == "warn":
            _LOG.warning(
                "Woow invalid output token limit forwarded (call_id=%s param=%s type=%s)",
                data.get("litellm_call_id"), param, type(data.get(param)).__name__,
            )
            continue
        raise _request_guard_error(problem, param)


def positive_token_count(value: object) -> int | None:
    """A requested output-token count usable for a budget estimate; bool and values < 1 are ignored."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 1 else None


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _setting(model_info: Mapping[str, object], key: str, env: str, default: int) -> int:
    configured = _non_negative_int(model_info.get(key))
    if configured is not None:
        return configured
    raw = os.environ.get(env)
    if raw is not None and raw.strip() != "":
        parsed = _non_negative_int(raw.strip())
        if parsed is not None:
            return parsed
        _LOG.warning("Woow: invalid %s=%r; using %s", env, raw, default)
    return default


def is_reasoning_model(model_info: Mapping[str, object]) -> bool:
    return model_info.get("supports_reasoning") is True or model_info.get("woow_max_tokens_enforced") is False


def reasoning_disabled(request_body: Mapping[str, object]) -> bool:
    """True when the body explicitly turns reasoning off (OpenRouter object, OpenAI effort, Anthropic thinking)."""
    reasoning = request_body.get("reasoning")
    if isinstance(reasoning, Mapping):
        budget = reasoning.get("max_tokens")
        if reasoning.get("enabled") is False or reasoning.get("effort") == "none":
            return True
        if isinstance(budget, int) and not isinstance(budget, bool) and budget == 0:
            return True
    thinking = request_body.get("thinking")
    if isinstance(thinking, Mapping) and thinking.get("type") == "disabled":
        return True
    return request_body.get("reasoning_effort") == "none"


def reserve_output_tokens(
    request_body: Mapping[str, object], model_info: Mapping[str, object], value: int | None, ceiling: int
) -> int | None:
    """Output tokens to reserve: at least min(R, ceiling) for reasoning models unless disabled."""
    if value is None or not is_reasoning_model(model_info) or reasoning_disabled(request_body):
        return value
    reserve = _setting(model_info, "woow_reasoning_output_reserve_tokens", RESERVE_ENV, DEFAULT_REASONING_RESERVE)
    return max(value, min(reserve, ceiling))


_OUTPUT_LIMIT_FIELDS: Final = ("max_completion_tokens", "max_tokens", "max_output_tokens")


def admission_floor_body(request_body: Mapping[str, object]) -> dict:
    """The request with every output-token limit set to 1: its estimate is what strict admission must fit."""
    floor = dict(request_body)
    for field in _OUTPUT_LIMIT_FIELDS:
        floor[field] = 1
    config = floor.get("inferenceConfig")
    if isinstance(config, Mapping):
        floor["inferenceConfig"] = {**config, "maxTokens": 1}
    return floor


def admission_floor_needed() -> bool:
    return budget_admission_mode() == "strict"


def admission_required_cost(estimate: float, floor: float | None, fail_closed: bool) -> float | None:
    """Cost the remaining budget must cover before admission; None admits by resizing (upstream)."""
    mode = budget_admission_mode()
    if fail_closed or mode == "full":
        return estimate
    if mode == "resize":
        return None
    return estimate if floor is None else min(floor, estimate)


def budget_estimate_detail(estimated_cost: float | None, remaining: float) -> str:
    """Refusal detail: the estimate that did not fit and what was left (never an entity id)."""
    if estimated_cost is None:
        return ""
    return f"Estimated request cost: {estimated_cost:.6g} exceeds the remaining budget: {max(remaining, 0.0):.6g}, "


def reserve_input_tokens(model_info: Mapping[str, object], counted: int) -> int:
    """Input tokens to reserve: reasoning models get a floor for the provider's hidden prompt."""
    if not is_reasoning_model(model_info):
        return counted
    return max(counted, _setting(model_info, "woow_min_input_tokens", MIN_INPUT_ENV, DEFAULT_MIN_INPUT))


def with_deployment_overrides(group_info: Mapping[str, object], deployments: Iterable[object]) -> dict:
    """Model-group cost info plus the deployments' Woow budget settings (largest value wins)."""
    merged = dict(group_info)
    for deployment in deployments or ():
        info = deployment.get("model_info") if isinstance(deployment, Mapping) else None
        if not isinstance(info, Mapping):
            continue
        for key in MODEL_OVERRIDE_KEYS:
            value = _non_negative_int(info.get(key))
            current = _non_negative_int(merged.get(key))
            if value is not None and (current is None or value > current):
                merged[key] = value
        if info.get("woow_max_tokens_enforced") is False:
            merged["woow_max_tokens_enforced"] = False
    return merged


def check_unpriced_budgeted_request(model: object) -> None:
    """A budget-limited key is about to use a model with no price: warn (default) or refuse."""
    group = ",".join(str(m) for m in model) if isinstance(model, (list, tuple)) else str(model)
    if unpriced_policy() == "allow":
        _LOG.warning("Woow event=unpriced_budgeted_request model=%s", group)
        return
    raise _request_guard_error(
        f"Model '{group}' has no price configured, so it cannot be used with a budget-limited key. "
        "Contact the proxy administrator.",
        "model",
    )
