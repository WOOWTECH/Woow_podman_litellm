"""Woow request-validation and OpenAI error-envelope helpers for pinned LiteLLM 1.104.0.

Installed as ``litellm/proxy/woow_api_errors.py`` by ``apply_api_errors.py``; the patched
upstream modules import it lazily, inside the functions that use it.

- Client mock fields (``mock_response``/``mock_tool_calls``) are rejected unless the key or
  team metadata already allows them, instead of being silently stripped.
- Requests rejected while being validated, before any provider call, write no spend log.
- ``role: tool`` messages must answer a ``tool_calls`` id of an earlier assistant message.
- OpenAI-compatible routes answer every error as ``{"error": {...}}`` with ``code`` equal to
  the HTTP status; an unhandled internal exception never leaks its text there.
Other routes (management, UI, Anthropic ``/v1/messages``, Woow plugin) keep their formats.
"""
from collections.abc import Mapping, Sequence
from typing import Final

from fastapi import status
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import HTTPConnection

from litellm.proxy._types import ProxyException
from litellm.proxy.route_llm_request import (
    MockTestingParamsDisabledError,
    ProxyMissingRequiredParamError,
    raise_if_required_body_param_missing,
)

INTERNAL_ERROR_MESSAGE: Final = "Internal server error"
INTERNAL_ERROR_TYPE: Final = "internal_server_error"
CLIENT_MOCK_FIELDS: Final = ("mock_response", "mock_tool_calls")
_REJECTED_BEFORE_UPSTREAM: Final = "woow_rejected_before_upstream"

# OpenAI API resources, reached directly, under /v1 or /openai/v1, or through the Azure
# /engines/<model>/ and /openai/deployments/<model>/ forms. Anthropic (/v1/messages,
# /v1/skills) and LiteLLM management resources under /v1 are deliberately absent.
_OPENAI_RESOURCES: Final = frozenset(
    {
        "assistants", "audio", "batches", "chat", "completions", "containers", "embeddings",
        "evals", "files", "fine_tuning", "images", "models", "moderations", "realtime",
        "responses", "threads", "vector_stores", "videos",
    }
)


def _route(conn: HTTPConnection) -> str:
    from litellm.proxy.auth.auth_utils import get_request_route

    return get_request_route(conn)  # type: ignore[arg-type]  # reads only scope/url


def _resource(route: str) -> str | None:
    parts: Final = [p for p in route.split("/") if p]
    for prefix in (["openai", "v1"], ["v1"]):
        if parts[: len(prefix)] == prefix:
            return parts[len(prefix)] if len(parts) > len(prefix) else None
    if parts[:2] == ["openai", "deployments"] and len(parts) > 3:
        return parts[3]
    if parts[:1] == ["engines"] and len(parts) > 2:
        return parts[2]
    return parts[0] if parts else None


def is_openai_route(conn: HTTPConnection) -> bool:
    """True for OpenAI-compatible routes, and for /v1/* paths that matched no route at all."""
    if conn.scope.get("type") != "http":
        return False
    route: Final = _route(conn)
    if _resource(route) in _OPENAI_RESOURCES:
        return True
    return route.startswith("/v1/") and conn.scope.get("endpoint") is None


def mark_rejected_before_upstream(exc: BaseException) -> BaseException:
    setattr(exc, _REJECTED_BEFORE_UPSTREAM, True)
    return exc


def rejected_before_upstream(exc: object) -> bool:
    """Request-validation rejections: no provider was called, so no spend log row is written."""
    return isinstance(
        exc, (ProxyMissingRequiredParamError, MockTestingParamsDisabledError, ToolCallIdMismatchError)
    ) or getattr(exc, _REJECTED_BEFORE_UPSTREAM, False) is True


class ClientMockParamsDisabledError(MockTestingParamsDisabledError):
    def __init__(self, params: tuple[str, ...]):
        self.message = (
            f"Mock testing request params are disabled on this proxy: {', '.join(params)}. "
            "An admin can allow them for a key or team by setting its metadata "
            "`allow_client_mock_response: true`."
        )
        self.type = "invalid_request_error"
        self.param = params[0]
        StarletteHTTPException.__init__(
            self, status_code=status.HTTP_400_BAD_REQUEST, detail={"error": self.message}
        )


def raise_if_client_mock_params_disallowed(data: Mapping[str, object], *, allowed: bool) -> None:
    """Reject instead of silently dropping, so a mock request never returns a real 200."""
    if allowed:
        return
    present: Final = tuple(name for name in CLIENT_MOCK_FIELDS if name in data)
    if present:
        raise ClientMockParamsDisabledError(params=present)


class ToolCallIdMismatchError(ProxyException):
    def __init__(self, index: int):
        param: Final = f"messages.[{index}].tool_call_id"
        super().__init__(
            message=(
                f"Invalid parameter: {param} does not match the id of any tool_calls entry "
                "in a previous assistant message."
            ),
            type="invalid_request_error",
            param=param,
            code=status.HTTP_400_BAD_REQUEST,
        )
        self.status_code = status.HTTP_400_BAD_REQUEST


def raise_if_tool_call_ids_mismatch(messages: object) -> None:
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return
    answered: Final[set[str]] = set()
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            continue
        if message.get("role") == "assistant" and isinstance(message.get("tool_calls"), Sequence):
            answered.update(
                call["id"] for call in message["tool_calls"]
                if isinstance(call, Mapping) and isinstance(call.get("id"), str)
            )
        elif message.get("role") == "tool":
            tool_call_id = message.get("tool_call_id")
            if not isinstance(tool_call_id, str) or tool_call_id not in answered:
                raise ToolCallIdMismatchError(index)


def validate_request_body(route_type: str, data: Mapping[str, object]) -> None:
    """Checks on the body exactly as the client sent it, before any hook or provider call."""
    if route_type == "atext_completion":
        raise_if_required_body_param_missing(route_type=route_type, data=data)
    elif route_type == "acompletion":
        raise_if_tool_call_ids_mismatch(data.get("messages"))


def openai_http_exception(conn: HTTPConnection, exc: StarletteHTTPException) -> ProxyException | None:
    """OpenAI envelope for an HTTPException (404 model, 405 method, unknown /v1 path, ...)."""
    if not is_openai_route(conn):
        return None
    from litellm.proxy.common_request_processing import proxy_exception_from_http_exception

    return proxy_exception_from_http_exception(exc, dict(exc.headers or {}))


def openai_validation_exception(conn: HTTPConnection, errors: Sequence[Mapping[str, object]]) -> ProxyException | None:
    if not is_openai_route(conn):
        return None
    details: Final = "; ".join(
        ".".join(str(part) for part in error.get("loc", ())) + ": " + str(error.get("msg", "invalid"))  # type: ignore[union-attr]
        for error in errors
    )
    return ProxyException(
        message="Invalid request: " + (details or "validation failed"),
        type="invalid_request_error",
        param=None,
        code=422,
    )


def _is_unhandled_internal(exc: ProxyException) -> bool:
    if exc.code != str(status.HTTP_500_INTERNAL_SERVER_ERROR):
        return False
    origin: Final = exc.__cause__ or exc.__context__
    if origin is None or isinstance(origin, (ProxyException, StarletteHTTPException)):
        return False
    return getattr(origin, "status_code", None) is None


def sanitize_internal_error(conn: HTTPConnection, exc: ProxyException) -> ProxyException:
    """A 500 made from an exception without an HTTP status is an internal error: generic text only."""
    if not is_openai_route(conn) or not _is_unhandled_internal(exc):
        return exc
    return ProxyException(
        message=INTERNAL_ERROR_MESSAGE,
        type=INTERNAL_ERROR_TYPE,
        param=None,
        code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        headers=exc.headers,
    )


def unhandled_error_body(conn: HTTPConnection) -> dict[str, object]:
    error: Final[dict[str, object]] = {"message": INTERNAL_ERROR_MESSAGE, "type": INTERNAL_ERROR_TYPE}
    if is_openai_route(conn):
        error["code"] = str(status.HTTP_500_INTERNAL_SERVER_ERROR)
    return {"error": error}
