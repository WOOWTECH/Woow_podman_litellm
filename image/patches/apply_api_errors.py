"""Fail-closed build patch for six LiteLLM 1.104.0 API error-contract defects (M4).

API-04.S05: client mock_response/mock_tool_calls get a 400 instead of being silently dropped.
API-04.S06: requests rejected by request validation (missing body parameter, unparsable JSON,
            mock parameters, tool_call_id mismatch) write no spend log and reach no provider.
API-07.S07: a usage chunk that arrives after the finish chunk adds no choices entry after it.
API-11.S02: a role=tool message must answer a tool_calls id of an earlier assistant message.
API-22.S01: OpenAI-compatible routes answer 404/405/422/500 as {"error": {...}} with
            code == HTTP status; other routes keep their error formats.
API-22.S02: /v1/completions without prompt is a 400; unhandled internal exceptions on
            OpenAI routes answer the generic 500 envelope without their text.

Runs last, after apply_login_throttle.py, on both the HA and the PaaS chains. Its targets are
outside the HA-only subscription policy patch (router.py), so both chains give the same input.
Three inputs are the reviewed apply_api_compat.py outputs. Validate every input, replacement
count, output hash, the helper and Python syntax before writing anything. One-shot: a second
application intentionally fails closed.
"""
import hashlib
import importlib.util
from pathlib import Path
import sys

HELPER = 'proxy/woow_api_errors.py'
HELPER_SHA256 = 'ceada742ff86d145bf40fd875866513aacac27d5f090c26937413479d57166ed'

# relative path: (reviewed input SHA-256, reviewed output SHA-256, edits)
PATCHES = {
    'litellm_core_utils/streaming_handler.py': (
        '1c31dc3841c051376ea40884f77118a41badea41102ddad076ba9ca9f89ffef6',
        '09013bd06249f0cc3d7310b849496c887959263ef2b0b1fee6e9404cf6355c5f',
        ((
            '                if hasattr(model_response, "usage") and model_response.usage is not None:\n'
            '                    return model_response\n'
            '                return\n',
            '                if hasattr(model_response, "usage") and model_response.usage is not None:\n'
            '                    # Woow API-07: a usage chunk after the finish chunk (e.g. OpenRouter) has no\n'
            '                    # delta. Keep its usage for cost tracking; no choice may follow the finish.\n'
            '                    self._record_usage_only_chunk(model_response=model_response)\n'
            '                return\n',
            1,
        ),),
    ),
    'proxy/litellm_pre_call_utils.py': (
        '5730d2612924ed53c1a2d2ddbbfdab6946d7035f9d695ff6d797f52774628055',
        'b70cd71c42f56de31746eb847826b518e476a1fd3894dcf2eeba01e722f05d7e',
        ((
            '    for _internal_key in _UNTRUSTED_ROOT_CONTROL_FIELDS:\n'
            '        if _allow_client_mock_response and _internal_key in _CLIENT_MOCK_CONTROL_FIELDS:\n',
            '    # Woow API-04: a mock request must be refused, not answered by the real provider.\n'
            '    from litellm.proxy.woow_api_errors import raise_if_client_mock_params_disallowed\n'
            '\n'
            '    raise_if_client_mock_params_disallowed(data, allowed=_allow_client_mock_response)\n'
            '    for _internal_key in _UNTRUSTED_ROOT_CONTROL_FIELDS:\n'
            '        if _allow_client_mock_response and _internal_key in _CLIENT_MOCK_CONTROL_FIELDS:\n',
            1,
        ),),
    ),
    'proxy/route_llm_request.py': (
        '0444eaf2cca0ac33cc0bab338f525c54058b7930568622e1703ca5a16a266bd4',
        '68f03c1bd61cda9f13efd0ca66c7a20fdf186624a8624a4b0a27a4cffc4d9d28',
        (
            (
                '    "aresponses": ("input",),\n',
                '    "aresponses": ("input",),\n'
                '    "atext_completion": ("prompt",),\n',
                1,
            ),
            (
                '            message=f"{route}: Missing required parameter: \'{param}\'.",\n'
                '            type="invalid_request_error",\n'
                '            param=param,\n'
                '            code=status.HTTP_400_BAD_REQUEST,\n'
                '        )\n',
                '            message=f"{route}: Missing required parameter: \'{param}\'.",\n'
                '            type="invalid_request_error",\n'
                '            param=param,\n'
                '            code=status.HTTP_400_BAD_REQUEST,\n'
                '        )\n'
                '        # Woow API-22: handlers that map status_code (e.g. /v1/completions) keep the 400.\n'
                '        self.status_code = status.HTTP_400_BAD_REQUEST\n',
                1,
            ),
        ),
    ),
    'proxy/common_request_processing.py': (
        '745e9f64a9cda4eb5167dd0ea02bc15d53dbee0a55ffb2152882dab7502a5f90',
        'adc20ff67119c85e3306a9786f79dc1513cb25e1fcd23d1171a48a8a5b1e021b',
        ((
            '                param="model",\n'
            '                code=status.HTTP_400_BAD_REQUEST,\n'
            '            )\n'
            '        self.data = await add_litellm_data_to_request(\n',
            '                param="model",\n'
            '                code=status.HTTP_400_BAD_REQUEST,\n'
            '            )\n'
            '        # Woow API-11/API-22: validate the body as sent, before hooks, logging setup or provider.\n'
            '        from litellm.proxy.woow_api_errors import validate_request_body\n'
            '\n'
            '        validate_request_body(route_type, self.data)\n'
            '        self.data = await add_litellm_data_to_request(\n',
            1,
        ),),
    ),
    'proxy/hooks/proxy_track_cost_callback.py': (
        '4dac983d1fd0881dea462b72603cfdb3ecdd91dbd3704e431642839894f7555f',
        'befbc7cf3a1b09f2bd6260543d21c21ac1dc487469dca281635765ae27e89ee3',
        ((
            '        request_route: Final = user_api_key_dict.request_route\n'
            '        if (\n'
            '            _ProxyDBLogger._should_track_errors_in_db() is False\n',
            '        # Woow API-04: a request refused by request validation never reached a provider.\n'
            '        from litellm.proxy.woow_api_errors import rejected_before_upstream\n'
            '\n'
            '        if rejected_before_upstream(original_exception):\n'
            '            return\n'
            '        request_route: Final = user_api_key_dict.request_route\n'
            '        if (\n'
            '            _ProxyDBLogger._should_track_errors_in_db() is False\n',
            1,
        ),),
    ),
    'proxy/auth/user_api_key_auth.py': (
        '129f2c8d91859bf4898e4cd5f068dc5f2404412da68ffe7495b26c362233270b',
        '3ad2c5ef479756d2c30ec076e691cfc213fde1911494407f893e52db5d0274a8',
        ((
            '    from litellm.proxy.proxy_server import proxy_logging_obj\n'
            '\n'
            '    try:\n'
            '        await proxy_logging_obj.post_call_failure_hook(  # pyright: ignore[reportUnknownMemberType]  # bare dict in sig\n'
            '            request_data={},  # mutable-ok: the failure hook seeds the call id and metadata onto this dict\n',
            '    from litellm.proxy.proxy_server import proxy_logging_obj\n'
            '    # Woow API-04: still logged to the failure hooks, but not as a spend log row.\n'
            '    from litellm.proxy.woow_api_errors import mark_rejected_before_upstream\n'
            '\n'
            '    mark_rejected_before_upstream(body_parse_exception)\n'
            '    try:\n'
            '        await proxy_logging_obj.post_call_failure_hook(  # pyright: ignore[reportUnknownMemberType]  # bare dict in sig\n'
            '            request_data={},  # mutable-ok: the failure hook seeds the call id and metadata onto this dict\n',
            1,
        ),),
    ),
    'proxy/proxy_server.py': (
        'a306974dd69f91c5f0443e1c80d4da084425db660d879606882c0205b7847cad',
        'ec011662127c97c5e4c3f7fad2508e2d179fb7bbb3d7f8f3940aed44f7803b5e',
        (
            (
                'async def openai_exception_handler(request: Request, exc: ProxyException):\n'
                '    # NOTE: DO NOT MODIFY THIS, its crucial to map to Openai exceptions\n'
                '    _log_model_access_denial(exc)\n',
                'async def openai_exception_handler(request: Request, exc: ProxyException):\n'
                '    # NOTE: DO NOT MODIFY THIS, its crucial to map to Openai exceptions\n'
                '    # Woow API-22: on OpenAI routes an unhandled internal exception keeps no text.\n'
                '    from litellm.proxy.woow_api_errors import sanitize_internal_error\n'
                '\n'
                '    exc = sanitize_internal_error(request, exc)\n'
                '    _log_model_access_denial(exc)\n',
                1,
            ),
            (
                '    _close_dangling_otel_server_span(request, 422, exc=exc)\n'
                '    return JSONResponse(\n'
                '        status_code=422,\n'
                '        content={"detail": jsonable_encoder(exc.errors())},\n'
                '    )\n',
                '    # Woow API-22: OpenAI routes answer validation errors in the OpenAI envelope.\n'
                '    from litellm.proxy.woow_api_errors import openai_validation_exception\n'
                '\n'
                '    openai_error = openai_validation_exception(request, exc.errors())\n'
                '    if openai_error is not None:\n'
                '        return await openai_exception_handler(request, openai_error)\n'
                '    _close_dangling_otel_server_span(request, 422, exc=exc)\n'
                '    return JSONResponse(\n'
                '        status_code=422,\n'
                '        content={"detail": jsonable_encoder(exc.errors())},\n'
                '    )\n',
                1,
            ),
            (
                '    _close_dangling_otel_server_span(request, 500, exc=exc)\n'
                '    return JSONResponse(\n'
                '        status_code=500,\n'
                '        content={\n'
                '            "error": {\n'
                '                "message": "Internal server error",\n'
                '                "type": "internal_server_error",\n'
                '            }\n'
                '        },\n'
                '    )\n'
                '\n'
                '\n'
                'router: Final = APIRouter()\n',
                '    _close_dangling_otel_server_span(request, 500, exc=exc)\n'
                '    # Woow API-22: same generic body; OpenAI routes also carry code "500".\n'
                '    from litellm.proxy.woow_api_errors import unhandled_error_body\n'
                '\n'
                '    return JSONResponse(status_code=500, content=unhandled_error_body(request))\n'
                '\n'
                '\n'
                'from starlette.exceptions import HTTPException as _WoowStarletteHTTPException  # noqa: E402\n'
                '\n'
                '\n'
                '@app.exception_handler(_WoowStarletteHTTPException)\n'
                'async def woow_openai_http_exception_handler(request: Request, exc: _WoowStarletteHTTPException):\n'
                '    # Woow API-22: HTTPExceptions on OpenAI routes (404 model, 405 method, unknown /v1\n'
                '    # path) use the OpenAI envelope; every other route keeps FastAPI\'s default body.\n'
                '    from fastapi.exception_handlers import http_exception_handler\n'
                '\n'
                '    from litellm.proxy.woow_api_errors import openai_http_exception\n'
                '\n'
                '    openai_error = openai_http_exception(request, exc)\n'
                '    if openai_error is None:\n'
                '        return await http_exception_handler(request, exc)\n'
                '    return await openai_exception_handler(request, openai_error)\n'
                '\n'
                '\n'
                'router: Final = APIRouter()\n',
                1,
            ),
        ),
    ),
}


def apply(root):
    root = Path(root)
    pending = []
    for name, (expected, result_hash, edits) in PATCHES.items():
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise SystemExit('Refusing missing or symlink target: ' + name)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise SystemExit('Refusing changed upstream source: ' + name)
        source = raw.decode('utf-8')
        for old, new, count in edits:
            if source.count(old) != count:
                raise SystemExit('Refusing changed patch context: ' + name)
            source = source.replace(old, new)
        result = source.encode('utf-8')
        if hashlib.sha256(result).hexdigest() != result_hash:
            raise SystemExit('Refusing unexpected patched source: ' + name)
        compile(source, str(path), 'exec')
        pending.append((path, result))
    helper_path = root / HELPER
    if helper_path.exists() or helper_path.is_symlink():
        raise SystemExit('Refusing existing helper: ' + HELPER)
    helper = Path(__file__).with_name('woow_api_errors.py').read_bytes()
    if hashlib.sha256(helper).hexdigest() != HELPER_SHA256:
        raise SystemExit('Refusing unexpected helper source: ' + HELPER)
    compile(helper.decode('utf-8'), str(helper_path), 'exec')
    pending.append((helper_path, helper))
    for path, result in pending:
        path.write_bytes(result)
        print(path.relative_to(root).as_posix() + ': ' + hashlib.sha256(result).hexdigest())


if __name__ == '__main__':
    if len(sys.argv) > 2:
        raise SystemExit('Usage: apply_api_errors.py [litellm-package-root]')
    root = Path(sys.argv[1]) if len(sys.argv) == 2 else Path(
        next(iter(importlib.util.find_spec('litellm').submodule_search_locations))
    )
    apply(root)
