"""Fail-closed build patch: M4 batch 1 (R01, R03, R04, R11, R15) on pinned LiteLLM 1.104.0.

R04  A proxy_admin_viewer gets 403 "user not allowed to access this OpenAI routes" on every
     inference route (chat, completions, embeddings, responses, messages, audio, images, ...)
     instead of being billed; its GET/HEAD/OPTIONS reads (and every management read) stay 200.
     Upstream's viewer check sits after the branch that passes all LLM routes.
R03  Every client-facing upstream error goes through one mask (woow_client_guard.client_error_message,
     built on the upstream guard's redact_secrets): the OpenAI error handler of every route
     (/v1/completions included), Anthropic /v1/messages errors (JSON and SSE), chat/completions and
     Responses SSE error frames, Responses WebSocket failures. The proxy's own credential values
     (deployment and credential secrets, ChatGPT access tokens resolved for a request), Bearer,
     sk- and JWT tokens and internal hosts are removed; connection failures read "Connection error.".
R15  /health entries (admin callers too), the background health cache, LiteLLM_HealthCheckTable rows
     and /health/latest|history get the same mask, the error keeps its first line (no stack trace)
     and ChatGPT endpoints show their api_base.
R11  Responses API stream events carry the requested model group in response.model, like the
     non-streaming response (HA relay/* models are priced by that name).
R01  New credential names and model ids that a browser or the HA Ingress cannot address as a path
     segment (containing '%', '/', '#', '?' or '\\', or being '.' or '..'), and model names
     containing '%', are refused with 400 (create and rename only; stored names keep working).

Runs last, after apply_upstream_guard.py, on both the HA and the PaaS chains. Five inputs are the
reviewed outputs of earlier installers (proxy/proxy_server.py and proxy/common_request_processing.py
of the API errors / upstream guard chain, the upstream guard's health endpoints and Anthropic
streaming iterator); the others are pristine LiteLLM 1.104.0 files. router.py (HA-only
subscription policy) is not a target, so both chains give the same inputs. Validate every input,
replacement count, output hash, the helper, the upstream guard helper it builds on and Python
syntax before writing anything. One-shot: a second application intentionally fails closed.
"""
import hashlib
import importlib.util
from pathlib import Path
import sys

# installed path: (source file next to this installer, reviewed SHA-256)
HELPERS = {
    'litellm_core_utils/woow_client_guard.py': (
        'woow_client_guard.py',
        '6acec556b988052ede00e2d237c87562414245d803786a4f861c52ec85762c6e',
    ),
}

# Installed by an earlier installer and imported by the helper: must be exactly the reviewed file.
DEPENDENCIES = {
    'litellm_core_utils/woow_provider_errors.py': '3aecbff2ca670d7c4bed4293433a20e391e5c07da9e0d57b211b2f11026ab585',
}

_IMPORT = '        from litellm.litellm_core_utils.woow_client_guard import {}\n\n'

# relative path: (reviewed input SHA-256, reviewed output SHA-256, edits)
PATCHES = {
    'proxy/auth/route_checks.py': (
        'e159ec69303ba8654062e368ee79376cc5427d9b7999d13942ab8eacae703422',
        'c828cde99fdc0fadedc6be0588cefe8d909345b10819c0d50888741232106417',
        ((
            '        elif RouteChecks.is_llm_api_route(route=route):\n'
            '            pass\n',
            '        elif RouteChecks.is_llm_api_route(route=route):\n'
            '            # Woow R04: a view-only admin reads (GET/HEAD/OPTIONS) but never runs billable\n'
            '            # inference; the viewer branch below is unreachable for LLM routes upstream.\n'
            '            if _user_role == LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY.value and RouteChecks._get_request_method(\n'
            '                request=request\n'
            '            ) not in ("GET", "HEAD", "OPTIONS"):\n'
            '                RouteChecks._check_proxy_admin_viewer_access(\n'
            '                    route=route,\n'
            '                    _user_role=_user_role,\n'
            '                    request_data=request_data,\n'
            '                    request=request,\n'
            '                )\n',
            1,
        ),),
    ),
    'proxy/proxy_server.py': (
        'ec011662127c97c5e4c3f7fad2508e2d179fb7bbb3d7f8f3940aed44f7803b5e',
        '2d036ba2913d0ecad2d43b2d56781c1638cfc81270cf84c591db253895722cde',
        (
            (
                '    exc = sanitize_internal_error(request, exc)\n'
                '    _log_model_access_denial(exc)\n',
                '    exc = sanitize_internal_error(request, exc)\n'
                '    # Woow R03: every client-facing error loses credentials, internal hosts and raw\n'
                '    # connection detail (/v1/completions, embeddings, responses, ... and management routes).\n'
                '    from litellm.litellm_core_utils.woow_client_guard import scrub_proxy_exception\n'
                '\n'
                '    exc = scrub_proxy_exception(exc)\n'
                '    _log_model_access_denial(exc)\n',
                1,
            ),
            (
                '        error_returned: Final = json.dumps({"error": proxy_exception.to_dict()})\n',
                '        # Woow R03: chat/completions and text completion SSE error frames use the same mask.\n'
                + _IMPORT.format('client_error_dict') +
                '        error_returned: Final = json.dumps({"error": client_error_dict(proxy_exception.to_dict())})\n',
                2,
            ),
            (
                '    downstream_model: Final = chunk.get("model") if isinstance(chunk, dict) else getattr(chunk, "model", None)\n'
                '    if downstream_model == target_model:\n',
                '    # Woow R11: Responses API events carry the model on event.response (response.created,\n'
                '    # in_progress, completed, ...): stamp it like the non-streaming response does.\n'
                '    from litellm.litellm_core_utils.woow_client_guard import restamp_responses_event_model\n'
                '\n'
                '    chunk = restamp_responses_event_model(chunk, target_model)\n'
                '    downstream_model: Final = chunk.get("model") if isinstance(chunk, dict) else getattr(chunk, "model", None)\n'
                '    if downstream_model == target_model:\n',
                1,
            ),
        ),
    ),
    'proxy/common_request_processing.py': (
        '2184dccb1c24728a1e6ad249cfe3e1d9c8331f8d2c1bb5c93adce7e15939a117',
        '2c7f9e0fe459df8bae59bb3f565153c08d90ee03f8475b3e8dd883369eb45de9',
        ((
            '            stream_error_status: Final = error_status_code(e, status.HTTP_500_INTERNAL_SERVER_ERROR)\n'
            '            proxy_exception: Final = ProxyException(\n'
            '                message=redact_internal_details_from_client_message(getattr(e, "message", str(e))),\n',
            '            stream_error_status: Final = error_status_code(e, status.HTTP_500_INTERNAL_SERVER_ERROR)\n'
            '            # Woow R03: SSE error frames of the shared streaming generator use the same mask.\n'
            '            from litellm.litellm_core_utils.woow_client_guard import client_error_message\n'
            '\n'
            '            proxy_exception: Final = ProxyException(\n'
            '                message=client_error_message(redact_internal_details_from_client_message(getattr(e, "message", str(e)))),\n',
            1,
        ),),
    ),
    'proxy/common_utils/responses_stream_errors.py': (
        '9a029357d771bb45d7e139de7e4864579c33bdf3ada622861dd257616622b12f',
        'd050ac10b1043608bc646618ef65ca5ec68fdb4e29a3811bd1eb9c64635236e6',
        (
            (
                '        original: Final = _original_failure(exception)\n'
                '        details: Final = _failure_details(original)\n',
                '        original: Final = _original_failure(exception)\n'
                '        details: Final = _failure_details(original)\n'
                '        # Woow R03: the response.failed event of a Responses stream uses the same mask.\n'
                + _IMPORT.format('client_error_message'),
                1,
            ),
            (
                '                            "message": redact_internal_details_from_client_message(details.message or str(original)),\n',
                '                            "message": client_error_message(\n'
                '                                redact_internal_details_from_client_message(details.message or str(original))\n'
                '                            ),\n',
                1,
            ),
        ),
    ),
    'proxy/response_api_endpoints/endpoints.py': (
        'a47693f778012379f7792d1dddfd19aef45c8306ea97e2221bcb6dc4405bae65',
        'a0bb715514004ace29d9a1bcbf158c8dbf77a5d4f791f33be30ab1f95c5247d9',
        ((
            '    return json.dumps({"type": "error", "status": status, "error": {"type": error_type, "message": str(failure)}})\n',
            '    # Woow R03: Responses WebSocket failures use the same mask.\n'
            '    from litellm.litellm_core_utils.woow_client_guard import client_error_message\n'
            '\n'
            '    message: Final = client_error_message(str(failure))\n'
            '    return json.dumps({"type": "error", "status": status, "error": {"type": error_type, "message": message}})\n',
            1,
        ),),
    ),
    'anthropic_interface/exceptions/exception_mapping_utils.py': (
        'c8254454f2fe5c9992007aae3acebfc989796010c5ca9e6bc4dd0e4e8479e47e',
        '71a2b99c8b8c29acecfb2782dffcd63eab24db99be857d2f88a0d8ed1ad9d4c5',
        ((
            '        # Try to parse as JSON once\n'
            '        parsed: dict | None = safe_json_loads(raw_message)\n',
            '        # Woow R03: /v1/messages errors (JSON body and SSE error event) use the same mask.\n'
            + _IMPORT.format('client_error_message') +
            '        raw_message = client_error_message(raw_message)\n'
            '        # Try to parse as JSON once\n'
            '        parsed: dict | None = safe_json_loads(raw_message)\n',
            1,
        ),),
    ),
    'llms/chatgpt/common_utils.py': (
        '90e7ecbc8802ad1ec1d3b287145857699ccd2e7671f97f10c264f617b558f4aa',
        'e5667b0298a811bc1c9ad295c6524284dd1b18ae99d420c3a76c5eb6adee2692',
        ((
            '    originator: Final = get_chatgpt_originator()\n'
            '    user_agent: Final = get_chatgpt_user_agent(originator)\n',
            '    # Woow R03: a resolved access token is masked by value in every client-facing error.\n'
            '    from litellm.litellm_core_utils.woow_client_guard import remember_secret\n'
            '\n'
            '    remember_secret(access_token)\n'
            '    originator: Final = get_chatgpt_originator()\n'
            '    user_agent: Final = get_chatgpt_user_agent(originator)\n',
            1,
        ),),
    ),
    'proxy/health_check.py': (
        '2e3160d3332332f60c4ddcb9500740a4b1bdd923d299e445c72808578397873d',
        '4256fee29dbbb383e9faed8c1a91afc774fc91bba0a7354c159c363689ec6599',
        ((
            '    displayed: Final = HEALTH_DISPLAY_PARAMS if details is not False else MINIMAL_DISPLAY_PARAMS\n'
            '    return {k: v for k, v in endpoint_data.items() if k in displayed}\n',
            '    displayed: Final = HEALTH_DISPLAY_PARAMS if details is not False else MINIMAL_DISPLAY_PARAMS\n'
            '    # Woow R15: every caller (admins too), the health cache and LiteLLM_HealthCheckTable get\n'
            '    # a masked first-line error and raw request; ChatGPT endpoints show their api_base.\n'
            '    from litellm.litellm_core_utils.woow_client_guard import health_endpoint_display\n'
            '\n'
            '    return health_endpoint_display(\n'
            '        {k: v for k, v in endpoint_data.items() if k in displayed}, endpoint_data, "api_base" in displayed\n'
            '    )\n',
            1,
        ),),
    ),
    'proxy/health_endpoints/_health_endpoints.py': (
        '5ff405b436493823c8ca6276f58a52405f151e658da3b75472f86cb4f4a01432',
        '6f027c864258d94fbef1e65dc409d625ab8d6fe8e85bcffdb90ccdf639ac14df',
        (
            (
                '    """Convert health check database record to dictionary format"""\n',
                '    """Convert health check database record to dictionary format"""\n'
                '    # Woow R15: /health/latest and /health/history mask rows written before the mask existed.\n'
                '    from litellm.litellm_core_utils.woow_client_guard import health_error_text\n'
                '\n',
                1,
            ),
            (
                '        "error_message": check.error_message,\n',
                '        "error_message": health_error_text(check.error_message),\n',
                1,
            ),
        ),
    ),
    'proxy/credential_endpoints/endpoints.py': (
        'bff9241d37e9715cfbd64891dc198a30ecedc13a94f2335e9bdc9f4cf1d52b68',
        'e6a09fae16fe3dfa1bc550835380232bbd76025c276f8b4e5c0b68e3d9b33458',
        (
            (
                '    Stores credential in DB.\n'
                '    Reloads credentials in memory.\n'
                '    """\n'
                '    from litellm.proxy.proxy_server import prisma_client\n',
                '    Stores credential in DB.\n'
                '    Reloads credentials in memory.\n'
                '    """\n'
                '    from litellm.proxy.proxy_server import prisma_client\n'
                '    # Woow R01: no new name a browser or the HA Ingress cannot address (woow_client_guard.PATH_UNSAFE).\n'
                '    from litellm.litellm_core_utils.woow_client_guard import refuse_unsafe_credential_name\n'
                '\n'
                '    refuse_unsafe_credential_name(credential.credential_name)\n',
                1,
            ),
            (
                '    llm_router: Annotated[litellm.Router | None, Depends(get_llm_router)] = None,\n'
                '):\n'
                '    """\n'
                '    [BETA] endpoint. This might change unexpectedly.\n'
                '    """\n'
                '    from litellm.proxy.proxy_server import prisma_client\n',
                '    llm_router: Annotated[litellm.Router | None, Depends(get_llm_router)] = None,\n'
                '):\n'
                '    """\n'
                '    [BETA] endpoint. This might change unexpectedly.\n'
                '    """\n'
                '    from litellm.proxy.proxy_server import prisma_client\n'
                '    # Woow R01: no rename to such a name (woow_client_guard.PATH_UNSAFE); keeping the stored name is allowed.\n'
                '    from litellm.litellm_core_utils.woow_client_guard import refuse_unsafe_credential_name\n'
                '\n'
                '    refuse_unsafe_credential_name(credential.credential_name, existing=credential_name)\n',
                1,
            ),
        ),
    ),
    'proxy/management_endpoints/model_management_endpoints.py': (
        'e9262533fe0fc73d676bdfd9e41f7c03d0478727ebf23298c878ee101e8a5e5e',
        'ecaf62b5bd9e631d35a0eaeb56fdb9ac0c563f42fc4726610ef79953da366e53',
        (
            (
                '        proxy_logging_obj,\n'
                '        store_model_in_db,\n'
                '    )\n'
                '\n'
                '    try:\n',
                '        proxy_logging_obj,\n'
                '        store_model_in_db,\n'
                '    )\n'
                '    # Woow R01: no model_name with \'%\', no client-chosen model id that is not a safe path segment.\n'
                '    from litellm.litellm_core_utils.woow_client_guard import refuse_unsafe_new_model\n'
                '\n'
                '    refuse_unsafe_new_model(model_params)\n'
                '\n'
                '    try:\n',
                1,
            ),
            (
                '        _model_id = _model_info.id\n'
                '        if _model_id is None:\n'
                '            raise Exception("model_info.id not provided")\n',
                '        _model_id = _model_info.id\n'
                '        if _model_id is None:\n'
                '            raise Exception("model_info.id not provided")\n'
                '        # Woow R01: no rename to a model_name with \'%\'; keeping the stored name is allowed.\n'
                + _IMPORT.format('refuse_unsafe_model_rename') +
                '        refuse_unsafe_model_rename(model_params, llm_router, _model_id)\n',
                1,
            ),
            (
                '        # Verify model exists and is stored in DB\n'
                '        if not store_model_in_db:\n',
                '        # Woow R01: no rename to a model_name with \'%\'; keeping the stored name is allowed.\n'
                + _IMPORT.format('refuse_unsafe_model_rename') +
                '        refuse_unsafe_model_rename(patch_data, llm_router, model_id)\n'
                '        # Verify model exists and is stored in DB\n'
                '        if not store_model_in_db:\n',
                1,
            ),
        ),
    ),
    'llms/anthropic/experimental_pass_through/adapters/streaming_iterator.py': (
        'ca7fdb570f91657c486df34ca72f0e9e7153ce5adae12c27df92b7730dbf9689',
        'b610d4cd89a50de57115f070ac1950d2775807781a7569b12951b4cdd9c481b1',
        ((
            '    return 500, str(exc) or "Upstream stream ended before completion"\n',
            '    # Woow R03: a non-provider mid-stream failure keeps no internal detail either.\n'
            '    from litellm.litellm_core_utils.woow_client_guard import client_error_message\n'
            '\n'
            '    return 500, client_error_message(str(exc)) or "Upstream stream ended before completion"\n',
            1,
        ),),
    ),
}


def apply(root):
    root = Path(root)
    for name, expected in DEPENDENCIES.items():
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise SystemExit('Refusing missing or symlink dependency: ' + name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise SystemExit('Refusing unexpected dependency: ' + name)
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
    for name, (source_name, helper_hash) in HELPERS.items():
        helper_path = root / name
        if helper_path.exists() or helper_path.is_symlink():
            raise SystemExit('Refusing existing helper: ' + name)
        source = Path(__file__).with_name(source_name)
        if source.is_symlink() or not source.is_file():
            raise SystemExit('Refusing missing or symlink helper source: ' + source_name)
        helper = source.read_bytes()
        if hashlib.sha256(helper).hexdigest() != helper_hash:
            raise SystemExit('Refusing unexpected helper source: ' + name)
        compile(helper.decode('utf-8'), str(helper_path), 'exec')
        pending.append((helper_path, helper))
    for path, result in pending:
        path.write_bytes(result)
        print(path.relative_to(root).as_posix() + ': ' + hashlib.sha256(result).hexdigest())


if __name__ == '__main__':
    if len(sys.argv) > 2:
        raise SystemExit('Usage: apply_m4_batch1.py [litellm-package-root]')
    root = Path(sys.argv[1]) if len(sys.argv) == 2 else Path(
        next(iter(importlib.util.find_spec('litellm').submodule_search_locations))
    )
    apply(root)
