"""Woow M4 batch 1 helpers for pinned LiteLLM 1.104.0 (client-facing errors, health, names).

Installed as ``litellm/litellm_core_utils/woow_client_guard.py`` by ``apply_m4_batch1.py``.
Standard library only at import time, so patched upstream modules can import it lazily from any
layer and lean tests load it by path.

R03  ``client_error_message()`` is the one mask every client-facing upstream error goes through
     (OpenAI JSON errors of every route, Anthropic /v1/messages errors, chat/completions and
     Responses SSE error frames, Responses WebSocket failures). It builds on the upstream guard's
     ``woow_provider_errors.redact_secrets`` (LiteLLM's own secret patterns plus Bearer, sk-, JWT
     and api_key rules) instead of a second rule set, and adds what that mask cannot know: the
     exact credential values the proxy itself holds (deployment and credential secrets, ChatGPT
     access tokens resolved for a request), internal hosts and addresses, and one wording for
     connection failures ("Connection error."). OpenRouter-specific rebuilding stays in the guard.
R15  ``health_endpoint_display()`` applies the same mask to every /health endpoint entry (admin
     callers too) before it is returned, cached or written to LiteLLM_HealthCheckTable: the error
     keeps its first line only (no stack trace) and ChatGPT endpoints show their api_base.
R11  ``restamp_responses_event_model()`` stamps the requested model group on Responses API
     stream events (``event.response.model``), as the non-streaming response already is.
R01  ``refuse_unsafe_*()`` refuse new credential names and model ids containing '%', '/', '#', '?'
     or '\\' or being '.' or '..', and model names containing '%'. The Home Assistant Ingress
     decodes request paths once more, and the dashboard puts names into URLs as typed: a browser
     cuts the URL at '#' or '?', turns '\\' into '/' and resolves '.' and '..'. Names that
     already exist are never refused: only creating one or renaming to one is.

Every function is idempotent: an error may pass several of these exits.
"""
from collections import OrderedDict
import ipaddress
import os
import re
import sys
from typing import Final

REDACTED: Final = "[redacted]"
REDACTED_HOST: Final = "[redacted-host]"
CONNECTION_ERROR: Final = "Connection error."

_GUARD_MODULE: Final = "litellm.litellm_core_utils.woow_provider_errors"
_LITELLM_REDACTION_MODULE: Final = "litellm.litellm_core_utils.secret_redaction"
_PROXY_SERVER_MODULE: Final = "litellm.proxy.proxy_server"
_CHATGPT_UTILS_MODULE: Final = "litellm.llms.chatgpt.common_utils"
CHATGPT_API_BASE_DEFAULT: Final = "https://chatgpt.com/backend-api/codex"

_MIN_SECRET: Final = 8  # shorter values are not masked by value (they would hit ordinary words)
_REMEMBER_LIMIT: Final = 256
_REMEMBERED: Final[OrderedDict] = OrderedDict()
_SECRET_KEYS: Final = frozenset({
    "api_key", "api_token", "access_token", "refresh_token", "id_token", "password", "client_secret",
    "azure_ad_token", "aws_access_key_id", "aws_secret_access_key", "aws_session_token",
    "vertex_credentials", "master_key",
})
_SECRET_SUFFIXES: Final = ("_api_key", "_secret", "_secret_key", "_token", "_password", "_credentials")
_ENV_PREFIX: Final = "os.environ/"
_HEALTH_ERROR_LIMIT: Final = 500
_HEALTH_ERROR_CUTS: Final = ("\nstack trace:", "\nstacktrace:", "Traceback (most recent call last):")

# Connection failures read the same on every route: no host, port, SSL context or errno detail.
_CONNECTION_RE: Final = re.compile(
    r"(?:Cannot connect to host|Connect call failed|All connection attempts failed|Connection refused|"
    r"Name or service not known|Temporary failure in name resolution|nodename nor servname provided|"
    r"No address associated with hostname|Network is unreachable|getaddrinfo failed|"
    r"(?:httpx\.|httpcore\.)?ConnectError\b)[^\n]*",
    re.I,
)
_CONNECTION_REPEAT_RE: Final = re.compile(r"Connection error\.(?:[\s:,;-]*Connection error\.)+")
_TRACEBACK: Final = "Traceback (most recent call last):"
_URL_HOST_RE: Final = re.compile(
    r"(?P<scheme>\b(?:https?|wss?|grpcs?)://)(?P<userinfo>[^\s/@'\"<>]*@)?"
    r"(?P<host>\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)"
)
_INTERNAL_SUFFIXES: Final = (
    ".local", ".localdomain", ".internal", ".lan", ".intra", ".corp", ".private", ".home.arpa", ".svc",
    ".cluster.local", ".ts.net",
)
_BARE_ADDRESS_RE: Final = re.compile(
    r"(?<![\w.:])(?:\d{1,3}(?:\.\d{1,3}){3}|\[?(?:::1|f[cd][0-9a-f]{2}(?::[0-9a-f]{0,4}){1,7}|fe80(?::[0-9a-f]{0,4}){1,7})\]?)(?![\w.])",
    re.I,
)
_CGNAT: Final = ipaddress.ip_network("100.64.0.0/10")


# ── secrets the proxy itself holds ───────────────────────────────────────────
def remember_secret(value: object) -> None:
    """Remember a credential resolved for a request (e.g. a ChatGPT access token), bounded."""
    if not isinstance(value, str):
        return
    value = value.strip()
    if len(value) < _MIN_SECRET:
        return
    _REMEMBERED[value] = None
    _REMEMBERED.move_to_end(value)
    while len(_REMEMBERED) > _REMEMBER_LIMIT:
        _REMEMBERED.popitem(last=False)


def _is_secret_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    key = key.lower()
    return key in _SECRET_KEYS or key.endswith(_SECRET_SUFFIXES)


def secret_values(params: object) -> list:
    """String secrets of a litellm_params / credential_values mapping (``os.environ/X`` resolved)."""
    if params is not None and not isinstance(params, dict) and hasattr(params, "model_dump"):
        try:
            params = params.model_dump()
        except Exception:  # noqa: BLE001 - masking must never break error handling
            return []
    if not isinstance(params, dict):
        return []
    out = []
    for key, value in params.items():
        if not _is_secret_key(key) or not isinstance(value, str):
            continue
        if value.startswith(_ENV_PREFIX):
            resolved = os.environ.get(value[len(_ENV_PREFIX):])
            if resolved:
                out.append(resolved)
            continue
        out.append(value)
    return [value.strip() for value in out if len(value.strip()) >= _MIN_SECRET]


def known_secrets(extra: object = ()) -> list:
    """Every credential value the proxy holds, longest first (so no value is masked piecemeal)."""
    values = set(_REMEMBERED)
    proxy = sys.modules.get(_PROXY_SERVER_MODULE)
    router = getattr(proxy, "llm_router", None) if proxy is not None else None
    for deployment in getattr(router, "model_list", None) or ():
        params = deployment.get("litellm_params") if isinstance(deployment, dict) else getattr(deployment, "litellm_params", None)
        values.update(secret_values(params))
    litellm_module = sys.modules.get("litellm")
    for credential in getattr(litellm_module, "credential_list", None) or ():
        values.update(secret_values(getattr(credential, "credential_values", None)))
    for value in extra or ():
        if isinstance(value, str) and len(value.strip()) >= _MIN_SECRET:
            values.add(value.strip())
    return sorted(values, key=len, reverse=True)


def _guard():
    """The upstream guard's helper (installed by apply_upstream_guard.py, verified by the installer)."""
    module = sys.modules.get(_GUARD_MODULE)
    if module is None:
        import importlib

        module = importlib.import_module(_GUARD_MODULE)
    return module


# Placeholders already in a text ([redacted], [redacted-url], [redacted-host]) and the values masked
# here are held as short bracketed tokens while the pattern rules run, then restored: LiteLLM's
# generic ``api_key: <value>`` rule would otherwise eat "api_key: [redacted" (8+ characters) and
# the guard's api_key rule the punctuation after it.
_PLACEHOLDER_RE: Final = re.compile(r"\[redacted(?:-[a-z]+)?\]")
_TOKEN_LIMIT: Final = 0xFF


class _Held:
    def __init__(self) -> None:
        self.kept = []

    def token(self, original: str) -> str:
        self.kept.append(original)
        return "[\ue000" + chr(0xE100 + len(self.kept) - 1) + "]"

    def hold(self, text: str, values: list) -> str:
        if len(_PLACEHOLDER_RE.findall(text)) + len(values) > _TOKEN_LIMIT:
            for value in values:
                text = text.replace(value, REDACTED)
            return text
        text = _PLACEHOLDER_RE.sub(lambda match: self.token(match.group(0)), text)
        for value in values:
            if value in text:
                text = text.replace(value, self.token(REDACTED))
        return text

    def restore(self, text: str) -> str:
        for index, original in enumerate(self.kept):
            text = text.replace("[\ue000" + chr(0xE100 + index) + "]", original)
        return text


def mask_secrets(text: str, extra: object = ()) -> str:
    """``text`` without the proxy's own credential values, then the upstream guard's secret rules."""
    held = _Held()
    return held.restore(_guard().redact_secrets(held.hold(text, known_secrets(extra))))


# ── internal hosts and connection failures ───────────────────────────────────
def _internal_address(text: str) -> bool:
    try:
        address = ipaddress.ip_address(text.strip("[]"))
    except ValueError:
        return False
    return bool(address.is_private or address.is_loopback or address.is_link_local or address.is_reserved
                or address.is_unspecified or (address.version == 4 and address in _CGNAT))


def _internal_host(host: str) -> bool:
    name = host.strip("[]").rstrip(".").lower()
    if not name:
        return False
    if _internal_address(name):
        return True
    if re.fullmatch(r"[0-9.]+", name):
        return False  # a public IPv4 literal
    return "." not in name or name.endswith(_INTERNAL_SUFFIXES)


def _url_host(match: "re.Match[str]") -> str:
    host = match.group("host")
    if not _internal_host(host):
        return match.group(0)
    return match.group("scheme") + REDACTED_HOST


def _bare_address(match: "re.Match[str]") -> str:
    return REDACTED_HOST if _internal_address(match.group(0)) else match.group(0)


def mask_internal(text: str) -> str:
    """No stack trace, internal hosts (single-label, .local/.internal/.svc/..., private, loopback,
    link-local and CGNAT addresses) or LiteLLM's own internal-detail patterns (paths, hosts)."""
    cut = text.find(_TRACEBACK)
    if cut != -1:
        text = text[:cut].rstrip()
    redaction = sys.modules.get(_LITELLM_REDACTION_MODULE)
    redact_internal_details = getattr(redaction, "redact_internal_details", None)
    if callable(redact_internal_details):
        try:
            text = redact_internal_details(text)
        except Exception:  # noqa: BLE001 - the local rules below still apply
            pass
    text = _URL_HOST_RE.sub(_url_host, text)
    return _BARE_ADDRESS_RE.sub(_bare_address, text)


def normalize_connection_error(text: str) -> str:
    """Connection failures (aiohttp, httpx, socket, DNS) all read ``Connection error.``."""
    text = _CONNECTION_RE.sub(CONNECTION_ERROR, text)
    return _CONNECTION_REPEAT_RE.sub(CONNECTION_ERROR, text)


def client_error_message(text: object, extra: object = ()) -> object:
    """The client-facing form of an error text; anything but a string is returned unchanged."""
    if not isinstance(text, str) or not text:
        return text
    held = _Held()
    text = held.hold(normalize_connection_error(text), known_secrets(extra))
    return held.restore(mask_internal(_guard().redact_secrets(text)))


def client_error_dict(error: object) -> object:
    """An OpenAI ``error`` object with its message masked (SSE error frames)."""
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return {**error, "message": client_error_message(error["message"])}
    return error


def scrub_proxy_exception(exc: object) -> object:
    """Mask a ProxyException's message in place (the global OpenAI error handler)."""
    message = getattr(exc, "message", None)
    if isinstance(message, str):
        masked = client_error_message(message)
        if masked != message:
            exc.message = masked  # type: ignore[attr-defined]
            exc.args = (masked,)  # type: ignore[attr-defined]
    return exc


# ── /health (R15) ────────────────────────────────────────────────────────────
def health_error_text(error: object, extra: object = ()) -> object:
    """A health-check error as shown and stored: first line, no stack trace, masked, bounded."""
    if not isinstance(error, str):
        return error
    for marker in _HEALTH_ERROR_CUTS:
        cut = error.find(marker)
        if cut != -1:
            error = error[:cut]
    lines = [line for line in error.strip().splitlines() if line.strip()]
    first = lines[0].strip() if lines else ""
    return client_error_message(first, extra)[:_HEALTH_ERROR_LIMIT]


def _mask_tree(value: object, extra: object) -> object:
    if isinstance(value, str):
        return mask_secrets(value, extra)
    if isinstance(value, dict):
        return {key: _mask_tree(item, extra) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_tree(item, extra) for item in value]
    return value


def _is_chatgpt(endpoint: dict) -> bool:
    model = endpoint.get("model")
    return endpoint.get("custom_llm_provider") == "chatgpt" or (isinstance(model, str) and model.startswith("chatgpt/"))


def chatgpt_api_base() -> str:
    """The ChatGPT backend LiteLLM's chatgpt provider calls when a deployment names none."""
    configured = os.environ.get("CHATGPT_API_BASE") or os.environ.get("OPENAI_CHATGPT_API_BASE")
    if configured:
        return configured
    utils = sys.modules.get(_CHATGPT_UTILS_MODULE)
    value = getattr(utils, "CHATGPT_API_BASE", None)
    return value if isinstance(value, str) and value else CHATGPT_API_BASE_DEFAULT


def health_endpoint_display(cleaned: object, endpoint_data: object, show_api_base: bool) -> object:
    """One /health endpoint entry: masked error and raw request, ChatGPT api_base filled in."""
    if not isinstance(cleaned, dict):
        return cleaned
    secrets = secret_values(endpoint_data)
    if isinstance(cleaned.get("error"), str):
        cleaned["error"] = health_error_text(cleaned["error"], secrets)
    if cleaned.get("raw_request_typed_dict") is not None:
        cleaned["raw_request_typed_dict"] = _mask_tree(cleaned["raw_request_typed_dict"], secrets)
    if show_api_base and not cleaned.get("api_base") and isinstance(endpoint_data, dict) and _is_chatgpt(endpoint_data):
        cleaned["api_base"] = chatgpt_api_base()
    return cleaned


# ── Responses stream model (R11) ─────────────────────────────────────────────
def restamp_responses_event_model(chunk: object, model: object) -> object:
    """A Responses API stream event whose ``response.model`` names ``model`` (a copy, never in place)."""
    if not isinstance(model, str) or not model:
        return chunk
    event_type = chunk.get("type") if isinstance(chunk, dict) else getattr(chunk, "type", None)
    event_type = getattr(event_type, "value", event_type)
    if not isinstance(event_type, str) or not event_type.startswith("response."):
        return chunk
    response = chunk.get("response") if isinstance(chunk, dict) else getattr(chunk, "response", None)
    if response is None:
        return chunk
    current = response.get("model") if isinstance(response, dict) else getattr(response, "model", None)
    if current is None or current == model:
        return chunk
    if isinstance(response, dict):
        restamped = {**response, "model": model}
    elif hasattr(response, "model_copy"):
        restamped = response.model_copy(update={"model": model})
    else:
        return chunk
    if isinstance(chunk, dict):
        return {**chunk, "response": restamped}
    if hasattr(chunk, "model_copy"):
        return chunk.model_copy(update={"response": restamped})
    return chunk


# ── names the HA Ingress or a browser cannot address (R01) ───────────────────
# PATH_UNSAFE: a name used as a URL path segment (/credentials/{name}, /model/{id}/update).
PATH_UNSAFE: Final = ("%", "/", "#", "?", "\\")
NAME_UNSAFE: Final = ("%",)
DOT_SEGMENTS: Final = (".", "..")


def unsafe_name_message(field: str, value: str, chars: tuple) -> str:
    quoted = [f"'{char}'" for char in chars]
    shown = quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + " or " + quoted[-1]
    shown_zh = "、".join(f"「{char}」" for char in chars)
    if "/" not in chars:
        return (
            f"Invalid {field} {value!r}: it must not contain {shown}. The Home Assistant sidebar (Ingress) "
            "decodes request paths once more, so such a name can reach a different resource. Any other "
            "characters are allowed (letters, digits, spaces, CJK, - _ . : @ ...). "
            f"{field} 不可包含{shown_zh}：Home Assistant 側邊欄（Ingress）會再解碼一次路徑，可能操作到別的資源；"
            "其他字元都可以使用（字母、數字、空白、中文、- _ . : @ 等）。"
        )
    return (
        f"Invalid {field} {value!r}: it must not contain {shown}, and must not be '.' or '..'. The dashboard "
        "puts names into URLs as typed (a browser cuts the URL at '#' or '?' and turns '\\' into '/'), and "
        "the Home Assistant sidebar (Ingress) decodes request paths once more, so such a name can reach a "
        "different resource. Any other characters are allowed (letters, digits, spaces, CJK, - _ . : @ ...). "
        f"{field} 不可包含{shown_zh}，也不可以只是「.」或「..」：後台會把名稱原樣放進網址（瀏覽器會在「#」或「?」"
        "截斷網址，並把「\\」換成「/」），Home Assistant 側邊欄（Ingress）也會再解碼一次路徑，可能操作到別的資源；"
        "其他字元都可以使用（字母、數字、空白、中文、- _ . : @ 等）。"
    )


def unsafe_name(value: object, chars: tuple, existing: object = None) -> bool:
    """True when ``value`` is a new name (not ``existing``) containing one of ``chars``; a path
    segment name (``chars`` holding '/') must not be '.' or '..' either."""
    if not isinstance(value, str) or value == existing:
        return False
    return any(char in value for char in chars) or ("/" in chars and value in DOT_SEGMENTS)


def _refuse(field: str, value: str, chars: tuple) -> None:
    from litellm.proxy._types import ProxyException

    raise ProxyException(
        message=unsafe_name_message(field, value, chars), type="invalid_request_error", param=field, code=400
    )


def refuse_unsafe_credential_name(new_name: object, existing: object = None) -> None:
    """POST /credentials (existing=None) and PATCH /credentials/{name} renames."""
    if unsafe_name(new_name, PATH_UNSAFE, existing):
        _refuse("credential_name", new_name, PATH_UNSAFE)  # type: ignore[arg-type]


def _field(obj: object, name: str) -> object:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def refuse_unsafe_new_model(model_params: object) -> None:
    """POST /model/new: model_name without '%', a client-chosen model id that is a safe path segment."""
    model_name = _field(model_params, "model_name")
    if unsafe_name(model_name, NAME_UNSAFE):
        _refuse("model_name", model_name, NAME_UNSAFE)  # type: ignore[arg-type]
    model_id = _field(_field(model_params, "model_info"), "id")
    if unsafe_name(model_id, PATH_UNSAFE):
        _refuse("model_info.id", model_id, PATH_UNSAFE)  # type: ignore[arg-type]


def refuse_unsafe_model_rename(model_params: object, router: object, model_id: object) -> None:
    """POST /model/update and PATCH /model/{id}/update: no rename (of a deployment the router
    knows) to a model_name with '%'; keeping the stored name is always allowed."""
    model_name = _field(model_params, "model_name")
    if not isinstance(model_name, str) or "%" not in model_name:
        return
    deployment = None
    get_deployment = getattr(router, "get_deployment", None)
    if callable(get_deployment) and isinstance(model_id, str):
        try:
            deployment = get_deployment(model_id=model_id)
        except Exception:  # noqa: BLE001 - lookup failures never block an update
            deployment = None
    if deployment is None:
        return  # the stored name is unknown here: never refuse what may be an existing name
    if unsafe_name(model_name, NAME_UNSAFE, _field(deployment, "model_name")):
        _refuse("model_name", model_name, NAME_UNSAFE)
