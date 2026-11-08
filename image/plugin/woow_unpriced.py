"""WOOW — models without a price and spend budgets (AI-LLM-005-1 / AI-FIX-LLM-4).

LiteLLM records the cost of a call from a price table: the bundled model map, or the prices an admin
typed into the model (``input_cost_per_token`` / ``output_cost_per_token``). A model that is in neither
is billed as ``0`` — silently. A virtual key, team or user budget counts that spend, so such a model is
**outside every budget**: measured on this chart (AI-LLM-005, ``qa-noprice`` = ``openai/xiaomi/mimo-v2.6-flash``
without prices, key ``max_budget`` 0.0001 USD): 21 of 21 calls answered 200, ``spend`` stayed 0, the upstream
was paid about 0.00028 USD (2.8x the budget); once a price was added the same key was refused after 9 calls.
Anyone who can add a model (the admin) or uses a model somebody added unpriced (a member holding a budgeted
key) is affected.

This module is called by ``woow_chatgpt._patch_proxy_logging`` right after the ChatGPT account guard and the
reasoning floor, with the request body and the key (``UserAPIKeyAuth``) of the caller:

* the caller has NO budget (the master key, an admin key without ``max_budget``, a team/user without one):
  nothing happens — there is nothing to bypass;
* the caller has a budget (key ``max_budget``, per-model key budget, team/user/end-user/organization budget)
  and the requested model group has a deployment without a price:
    - mode ``warn`` (the default): the request goes through, the response carries
      ``x-woow-warning: model_unpriced_budget_not_enforced`` and the first such request per model group is
      logged (no key, no body);
    - mode ``reject``: ``400 invalid_request_error`` (``param: model``) that says what to fix;
    - mode ``off``: exactly as before (no header, no refusal).

A deployment counts as priced when any of these holds (so the check never fires for a model LiteLLM can
price):
1. the route (``model_info`` or ``litellm_params``) carries a price — ``input_cost_per_token``,
   ``output_cost_per_token`` or the per-second / per-character / per-pixel / per-image variants. A price of
   ``0`` is a price: that is how an admin says "free" (self-hosted model);
2. ``model_info.woow_unpriced_ok: true`` — the admin's explicit "this route is free / not budgeted" mark,
   for UIs that treat ``0`` as "empty";
3. LiteLLM's own price table prices the provider model (a positive price, or a table entry that sets the
   price to 0 on purpose; the all-``None`` placeholder the router registers for an unpriced deployment
   does not count);
4. the provider is ``chatgpt/`` (a flat subscription, not a per-token bill).

Settings (environment, read at every request; the chart maps it from values litellm.unpricedModelMode):
  WOOW_UNPRICED_MODEL_MODE   warn (default) | reject | off

Everything here is best effort: an unexpected error never fails a request, and ``warn`` never changes one.
"""

from __future__ import annotations

import contextvars
import os
import sys

PROXY_TARGET = "litellm.proxy.proxy_server"
WARNING = "model_unpriced_budget_not_enforced"

# Any of these on a route is "an admin priced it" (0 included).
_PRICE_KEYS = (
    "input_cost_per_token", "output_cost_per_token",
    "input_cost_per_second", "output_cost_per_second",
    "input_cost_per_character", "output_cost_per_character",
    "input_cost_per_pixel", "output_cost_per_pixel",
    "input_cost_per_image", "output_cost_per_image",
    "input_cost_per_audio_per_second", "input_cost_per_video_per_second",
    "input_cost_per_query",
)
# Providers whose bill is not per token (ChatGPT sign-in: the account's subscription).
_FLAT_RATE_PROVIDERS = ("chatgpt",)
# UserAPIKeyAuth fields whose positive number means "a budget applies to this caller".
_BUDGET_FIELDS = ("max_budget", "team_max_budget", "user_max_budget", "end_user_max_budget",
                  "organization_max_budget")
# …and whose non-empty dict does (per-model budgets).
_BUDGET_DICT_FIELDS = ("model_max_budget", "team_model_max_budget", "user_model_max_budget",
                       "end_user_model_max_budget")

# Set by the pre-call step for the same request's post-call step (same asyncio task).
_FLAGGED: contextvars.ContextVar = contextvars.ContextVar("woow_unpriced_flagged", default=False)
_LOGGED: set = set()


def _log(msg: str) -> None:
    print(f"woow-unpriced: {msg}", file=sys.stderr, flush=True)


def mode() -> str:
    value = (os.environ.get("WOOW_UNPRICED_MODEL_MODE") or "warn").strip().lower()
    if value in ("false", "no", "0", "disabled"):   # an unquoted `off` in a YAML values file is `false`
        return "off"
    return value if value in ("warn", "reject", "off") else "warn"


# ── does a budget apply to this caller? ───────────────────────────────────────
def _positive(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def has_budget(user) -> bool:
    """True when the key, its team/user/end-user/organization or a per-model budget caps spend."""
    if user is None:
        return False
    for field in _BUDGET_FIELDS:
        if _positive(getattr(user, field, None)):
            return True
    for field in _BUDGET_DICT_FIELDS:
        value = getattr(user, field, None)
        if isinstance(value, dict) and value:
            return True
    return False


# ── is a deployment priced? ───────────────────────────────────────────────────
def _has_price(source) -> bool:
    return isinstance(source, dict) and any(source.get(k) is not None for k in _PRICE_KEYS)


def _provider_and_model(params: dict) -> tuple[str | None, str]:
    model = str((params or {}).get("model") or "")
    provider = (params or {}).get("custom_llm_provider")
    if not provider and "/" in model:
        provider, model = model.split("/", 1)
    return (str(provider).lower() if provider else None), model


def is_priced(deployment: dict) -> bool:
    params = deployment.get("litellm_params") if isinstance(deployment.get("litellm_params"), dict) else {}
    info = deployment.get("model_info") if isinstance(deployment.get("model_info"), dict) else {}
    if _has_price(info) or _has_price(params):
        return True
    if info.get("woow_unpriced_ok") is True:
        return True
    provider, model = _provider_and_model(params)
    if provider in _FLAT_RATE_PROVIDERS:
        return True
    return _table_priced(provider, model, str(params.get("model") or ""))


def _positive_price(entry) -> bool:
    return isinstance(entry, dict) and any(
        isinstance(entry.get(k), (int, float)) and not isinstance(entry.get(k), bool) and entry[k] > 0 for k in _PRICE_KEYS)


def _table_priced(provider, model: str, full: str) -> bool:
    """Does LiteLLM's price table price this provider model?

    The router registers every deployment's model name in ``litellm.model_cost`` — an unpriced one as an
    entry whose prices are all ``None`` — and ``get_model_info`` then answers ``0`` for it (measured on
    1.104.0 in the proxy process, AI-FIX-LLM-4). So a ``0`` from ``get_model_info`` proves nothing; only
    a positive price does. A real table entry that is free on purpose (``…:free``) has the price keys set
    (``0``, not ``None``) in the raw table, which is what the first loop looks for."""
    import litellm

    table = getattr(litellm, "model_cost", None)
    candidates = [c for c in dict.fromkeys([full, f"{provider}/{model}" if provider else "", model]) if c]
    if isinstance(table, dict):
        for name in candidates:
            entry = table.get(name)
            if isinstance(entry, dict) and any(entry.get(k) is not None for k in _PRICE_KEYS):
                return True
    for name, prov in ((model, provider), (full, None)):
        if not name:
            continue
        try:
            found = litellm.get_model_info(model=name, custom_llm_provider=prov)
        except Exception:  # noqa: BLE001 — ModelNotMappedError and friends: not priced by LiteLLM
            continue
        if _positive_price(found):
            return True
    return False


def _deployments(model_group) -> list:
    router = getattr(sys.modules.get(PROXY_TARGET), "llm_router", None)
    if router is None or not isinstance(model_group, str):
        return []
    return [d for d in list(getattr(router, "model_list", None) or [])
            if isinstance(d, dict) and d.get("model_name") == model_group]


def unpriced_deployments(model_group) -> list:
    return [d for d in _deployments(model_group) if not is_priced(d)]


# ── hooks (called from woow_chatgpt) ──────────────────────────────────────────
def pre_call(user, data) -> None:
    """Warn about (or refuse) a budgeted caller using a model without a price."""
    _FLAGGED.set(False)
    if not isinstance(data, dict) or mode() == "off":
        return
    try:
        if not has_budget(user):
            return
        group = data.get("model")
        if not unpriced_deployments(group):
            return
    except Exception as exc:  # noqa: BLE001 — never break a request over this
        _log(f"WARNING unpriced-model check skipped: {type(exc).__name__}")
        return
    if mode() == "reject":
        from litellm.proxy._types import ProxyException

        raise ProxyException(
            message=(f"Model '{group}' has no price, so a spend budget cannot limit it: its calls would be "
                     "recorded as 0. Ask the gateway admin to set the model's input and output cost "
                     "(a price of 0 marks it free), or to set model_info.woow_unpriced_ok."),
            type="invalid_request_error", param="model", code=400)
    _FLAGGED.set(True)
    if group not in _LOGGED:
        _LOGGED.add(group)
        _log(f"WARNING model group '{group}' has no price: spend through budgeted keys is recorded as 0 "
             "(set its input/output cost, or model_info.woow_unpriced_ok to silence this)")


def post_call(response) -> None:
    """Add the warning header to a non-streaming response (hidden_params.additional_headers)."""
    try:
        if not _FLAGGED.get() or mode() != "warn":
            return
        hidden = getattr(response, "_hidden_params", None)
        if not isinstance(hidden, dict):
            return
        extra = hidden.get("additional_headers")
        if not isinstance(extra, dict):
            extra = {}
            hidden["additional_headers"] = extra
        existing = extra.get("x-woow-warning")
        extra["x-woow-warning"] = f"{existing}, {WARNING}" if existing else WARNING
    except Exception as exc:  # noqa: BLE001
        _log(f"WARNING unpriced-model header skipped: {type(exc).__name__}")
