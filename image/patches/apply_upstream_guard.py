"""Fail-closed build patch: upstream (OpenRouter) error leaks, token limits, budget admission.

P0  Provider errors reach clients, streaming error frames and spend logs without the gateway's
    OpenRouter account (user_id), error.metadata (raw upstream text, provider_name, is_byok,
    flagged input, headers) or request/generation ids, on openrouter/ and on openai/ routes
    pointed at OpenRouter; only rate-limit and retry-after provider headers are forwarded as
    llm_provider-*; /health shows non-admin keys no raw request or stack trace. The original
    text stays in the server log.
P1  max_tokens / max_completion_tokens / max_output_tokens must be null or an integer 1..2**31-1
    (OpenAI 400 envelope with param, before any provider call or spend log).
P1  Budget admission is strict (a request whose input plus reasoning reserve does not fit the
    remaining budget is refused, not shrunk), reasoning models reserve at least R output tokens
    and a minimum input size, a negative or
    boolean max_tokens no longer shrinks the reservation, the refusal carries
    x-should-retry: false and no longer echoes the key hash, and an unpriced model used by a
    budget-limited key is logged (or refused).
P2  reasoning_effort is forwarded to OpenRouter for every model, not only models in the cost map.
D1  Authentication and key errors (unknown, deleted or revoked key, key not in cache, ...) and
    key-scoped rate-limit errors reach clients, failure callbacks and spend-log error_information
    without the virtual key's hash; the masked sk-...last4 stays, and the auth error logged on the
    server keeps the hash for admins.

Runs last, after apply_api_errors.py, on both the HA and the PaaS chains. Nine inputs are pristine
LiteLLM 1.104.0 files; proxy/common_request_processing.py is the reviewed apply_api_errors.py
output. Validate every input, replacement count, output hash, every helper and Python syntax
before writing anything. One-shot: a second application intentionally fails closed.
"""
import hashlib
import importlib.util
from pathlib import Path
import sys

# installed path: (source file next to this installer, reviewed SHA-256)
HELPERS = {
    'litellm_core_utils/woow_provider_errors.py': (
        'woow_provider_errors.py',
        '3aecbff2ca670d7c4bed4293433a20e391e5c07da9e0d57b211b2f11026ab585',
    ),
    'proxy/woow_request_guard.py': (
        'woow_request_guard.py',
        'b24669ba4757786988f482ccf788ba02258c37daea04a36e18256f812f775980',
    ),
    'proxy/auth/woow_auth_errors.py': (
        'woow_auth_errors.py',
        'ba8b4c8f5b750f66f3af1b974970130a75c696ad434b5c0944b7a167b0ce5e9f',
    ),
}

# relative path: (reviewed input SHA-256, reviewed output SHA-256, edits)
PATCHES = {
    'litellm_core_utils/exception_mapping_utils.py': (
        '7896a70c295d5730a975fc7edee30d91821bd9d6e09830bafd280f5a1c2da771',
        '8aac54ecede7cf11e3c0d934802eadb215cd51c4835df7f42d1e6c85311e57b6',
        (
            (
                '            message = str(original_exception)\n'
                '\n'
                '    if message is not None and isinstance(\n'
                '        message, str\n'
                '    ):  # done to prevent user-confusion. Relevant issue - https://github.com/BerriAI/litellm/issues/1414\n',
                '            message = str(original_exception)\n'
                '    # Woow P0: OpenAI-compatible routes (openai/, text completion, embeddings) pointed at\n'
                '    # OpenRouter: the full SDK text decides, the inner message alone has no user_id.\n'
                '    from litellm.litellm_core_utils.woow_provider_errors import scrub_compatible_message\n'
                '\n'
                '    message = scrub_compatible_message(custom_llm_provider, message, original_exception)\n'
                '\n'
                '    if message is not None and isinstance(\n'
                '        message, str\n'
                '    ):  # done to prevent user-confusion. Relevant issue - https://github.com/BerriAI/litellm/issues/1414\n',
                1,
            ),
            (
                '            if isinstance(original_exception, BaseException):\n'
                '                exception_type = type(original_exception).__name__\n',
                '            # Woow P0: OpenRouter bodies carry the gateway account (user_id) and metadata.raw.\n'
                '            from litellm.litellm_core_utils.woow_provider_errors import scrub_provider_error\n'
                '\n'
                '            error_str = scrub_provider_error(\n'
                '                custom_llm_provider, error_str, getattr(original_exception, "status_code", None)\n'
                '            )\n'
                '            if isinstance(original_exception, BaseException):\n'
                '                exception_type = type(original_exception).__name__\n',
                1,
            ),
        ),
    ),
    'llms/openrouter/chat/transformation.py': (
        'f6cf4733c03c0b993f009dadc3fc9636434ad47cee20a4cefde2c70078578bec',
        '56e8d6f1242e7f9cd6036dcaaec2cdea620fe4c7c2566389c74184f745fcce7d',
        (
            (
                '        supported_params: Final = super().get_supported_openai_params(model=model)\n'
                '        try:\n'
                '            if litellm.supports_reasoning(model=model, custom_llm_provider="openrouter") or litellm.supports_reasoning(\n'
                '                model=model\n'
                '            ):\n'
                '                supported_params.append("reasoning_effort")\n'
                '                supported_params.append("thinking")\n',
                '        supported_params: Final = super().get_supported_openai_params(model=model)\n'
                '        # Woow P2: OpenRouter accepts reasoning_effort for any model; models missing from the\n'
                '        # cost map must not have it silently dropped (drop_params).\n'
                '        supported_params.append("reasoning_effort")\n'
                '        try:\n'
                '            if litellm.supports_reasoning(model=model, custom_llm_provider="openrouter") or litellm.supports_reasoning(\n'
                '                model=model\n'
                '            ):\n'
                '                supported_params.append("thinking")\n',
                1,
            ),
            (
                '                error_chunk: Final = chunk["error"]\n'
                '                error_message: Final = OpenRouterErrorMessage(\n'
                '                    message="Message: {}, Metadata: {}, User ID: {}".format(\n'
                '                        error_chunk["message"],\n'
                '                        error_chunk.get("metadata", {}),\n'
                '                        error_chunk.get("user_id", ""),\n'
                '                    ),\n'
                '                    code=error_chunk["code"],\n'
                '                    metadata=error_chunk.get("metadata", {}),\n'
                '                )\n'
                '                raise OpenRouterException(\n'
                '                    message=error_message["message"],\n'
                '                    status_code=error_message["code"],\n'
                '                    headers=error_message["metadata"].get("headers", {}),\n'
                '                )\n',
                '                error_chunk: Final = chunk["error"]\n'
                '                # Woow P0: no user_id, metadata or provider headers; string codes map to a status.\n'
                '                from litellm.litellm_core_utils.woow_provider_errors import stream_error_parts\n'
                '\n'
                '                message, status = stream_error_parts(error_chunk)\n'
                '                raise OpenRouterException(message=message, status_code=status, headers={})\n',
                1,
            ),
        ),
    ),
    'llms/anthropic/experimental_pass_through/adapters/streaming_iterator.py': (
        '928654271696a04b87d3adbba9313d23fbb66cd3250fba5e3281515162a3b69b',
        'ca7fdb570f91657c486df34ca72f0e9e7153ce5adae12c27df92b7730dbf9689',
        ((
            '    if isinstance(exc, (BaseLLMException, MidStreamFallbackError)):\n'
            '        return exc.status_code, exc.message\n',
            '    if isinstance(exc, (BaseLLMException, MidStreamFallbackError)):\n'
            '        # Woow P0: second line of defence for /v1/messages mid-stream provider errors.\n'
            '        from litellm.litellm_core_utils.woow_provider_errors import client_safe_stream_message\n'
            '\n'
            '        return exc.status_code, client_safe_stream_message(exc.message)\n',
            1,
        ),),
    ),
    'litellm_core_utils/llm_response_utils/get_headers.py': (
        'fb2922ad9bccad07db19070eb08ecbe8db4aa6203bb2b36b35e09d5b02e2ce78',
        'fbae86904ca809c2908b49c9ead1fdf3cdc916d5de5bb16dfcffcc5b5d7e2de9',
        ((
            '    llm_provider_headers: Final = {}\n'
            '    for k, v in response_headers.items():\n'
            '        if "llm_provider" not in k:\n',
            '    # Woow P0: forward only rate-limit/retry-after provider headers (WOOW_PROVIDER_HEADERS=all\n'
            '    # restores every header); cf-ray, x-request-id and provider probes stay server-side.\n'
            '    from litellm.litellm_core_utils.woow_provider_errors import provider_header_allowed\n'
            '\n'
            '    llm_provider_headers: Final = {}\n'
            '    for k, v in response_headers.items():\n'
            '        if not provider_header_allowed(k):\n'
            '            continue\n'
            '        if "llm_provider" not in k:\n',
            1,
        ),),
    ),
    'litellm_core_utils/core_helpers.py': (
        '045e8d1e77e7e152da8eb21b0bb6e5dcc3e88a685b8b984357e69638dbf03d8a',
        'd5bc555486e4fc2e303b6678b4515f32b089abca028b276ddb77aff536d216c4',
        ((
            '        else:\n'
            '            additional_headers["{}-{}".format("llm_provider", k)] = v\n',
            '        else:\n'
            '            # Woow P0: streaming/raw provider headers follow the same allowlist as get_headers.\n'
            '            from litellm.litellm_core_utils.woow_provider_errors import provider_header_allowed\n'
            '\n'
            '            if provider_header_allowed(k):\n'
            '                additional_headers["{}-{}".format("llm_provider", k)] = v\n',
            1,
        ),),
    ),
    'proxy/common_request_processing.py': (
        'adc20ff67119c85e3306a9786f79dc1513cb25e1fcd23d1171a48a8a5b1e021b',
        '2184dccb1c24728a1e6ad249cfe3e1d9c8331f8d2c1bb5c93adce7e15939a117',
        ((
            '        validate_request_body(route_type, self.data)\n'
            '        self.data = await add_litellm_data_to_request(\n',
            '        validate_request_body(route_type, self.data)\n'
            '        # Woow P1: output token limits must be integers 1..2**31-1, before any provider call.\n'
            '        from litellm.proxy.woow_request_guard import validate_token_limits\n'
            '\n'
            '        validate_token_limits(route_type, self.data)\n'
            '        self.data = await add_litellm_data_to_request(\n',
            1,
        ),),
    ),
    'proxy/spend_tracking/budget_reservation.py': (
        '433bf0a8f2e4ed5d7301c49c4f7d7151da65ed509157429b85bb32f53869cca5',
        'e6e75364b9a31a1f3608c3383e53ed31dcef36b3db844aef7a811950c8e18c53',
        (
            (
                'from litellm.proxy.utils import PrismaClient, ProxyLogging\n',
                'from litellm.proxy.utils import PrismaClient, ProxyLogging\n'
                'from litellm.proxy import woow_request_guard as _woow_guard  # Woow P1 budget admission\n',
                1,
            ),
            (
                '    current_spend: float,\n'
                '    fail_closed_budget_enforcement: bool = False,\n'
                ') -> float:\n',
                '    current_spend: float,\n'
                '    fail_closed_budget_enforcement: bool = False,\n'
                '    admission_floor_cost: float | None = None,  # Woow P1\n'
                ') -> float:\n',
                1,
            ),
            (
                '    if fail_closed_budget_enforcement and current_spend - counter.max_budget > 1e-12:\n'
                '        _raise_counter_budget_exceeded(\n'
                '            counter=counter,\n'
                '            current_cost=current_spend - reservation_cost,\n'
                '            estimated_cost=reservation_cost,\n'
                '        )\n',
                '    # Woow P1: strict admission (default) refuses a request whose unbounded part (input plus the\n'
                '    # reasoning reserve) does not fit the remaining budget and resizes the rest as upstream does;\n'
                '    # WOOW_BUDGET_ADMISSION=full refuses any estimate above it, resize restores upstream.\n'
                '    required: Final = _woow_guard.admission_required_cost(\n'
                '        reservation_cost, admission_floor_cost, fail_closed_budget_enforcement\n'
                '    )\n'
                '    if required is not None and required - remaining_before_reservation > 1e-12:\n'
                '        _raise_counter_budget_exceeded(\n'
                '            counter=counter,\n'
                '            current_cost=current_spend - reservation_cost,\n'
                '            estimated_cost=required,\n'
                '        )\n',
                1,
            ),
            (
                '    estimate_detail: Final = "" if estimated_cost is None else f"Estimated request cost: {estimated_cost}, "\n',
                '    # Woow P1: say that the estimate, not spend so far, did not fit what is left.\n'
                '    estimate_detail: Final = _woow_guard.budget_estimate_detail(estimated_cost, counter.max_budget - current_cost)\n',
                1,
            ),
            (
                '            f"{counter.entity_type}={counter.entity_id} "\n',
                '            f"{counter.entity_type} budget. "  # Woow P1: never echo the key hash or entity id\n',
                1,
            ),
            (
                '    if reservation_cost is None or reservation_cost <= 0:\n'
                '        return None\n',
                '    if reservation_cost is None:\n'
                '        # Woow P1: a budget-limited key on a model without a price is logged (or refused).\n'
                '        _woow_guard.check_unpriced_budgeted_request(get_model_from_request(request_body, route, llm_router=llm_router))\n'
                '    if reservation_cost is None or reservation_cost <= 0:\n'
                '        return None\n'
                '    # Woow P1: what strict admission must fit: the estimate with every output limit taken as 1.\n'
                '    admission_floor_cost: Final = (\n'
                '        estimate_request_max_cost(\n'
                '            request_body=_woow_guard.admission_floor_body(request_body),\n'
                '            route=route,\n'
                '            llm_router=llm_router,\n'
                '            input_token_counts=input_token_counts,\n'
                '        )\n'
                '        if _woow_guard.admission_floor_needed()\n'
                '        else None\n'
                '    )\n',
                1,
            ),
            (
                '                        fail_closed_budget_enforcement=fail_closed_budget_enforcement,\n'
                '                    )\n'
                '                    continue\n',
                '                        fail_closed_budget_enforcement=fail_closed_budget_enforcement,\n'
                '                        admission_floor_cost=admission_floor_cost,  # Woow P1\n'
                '                    )\n'
                '                    continue\n',
                1,
            ),
            (
                '        if model_group_info is not None:\n'
                '            return model_group_info.model_dump()\n',
                '        if model_group_info is not None:\n'
                '            # Woow P1: per-model budget reserves (model_info.woow_*) live on the deployments.\n'
                '            return _woow_guard.with_deployment_overrides(\n'
                '                model_group_info.model_dump(), llm_router.get_model_list(model_name=model) or []\n'
                '            )\n',
                1,
            ),
            (
                '    if counted is not None:\n'
                '        return counted\n',
                '    if counted is not None:\n'
                '        # Woow P1: reasoning models get an input floor for the provider\'s hidden prompt.\n'
                '        return _woow_guard.reserve_input_tokens(model_info, counted)\n',
                1,
            ),
            (
                '    return min(DEFAULT_MAX_OUTPUT_TOKENS_FALLBACK if requested is None else requested, model_ceiling)\n',
                '    # Woow P1: client max_tokens does not bound hidden reasoning; reserve at least R tokens.\n'
                '    return _woow_guard.reserve_output_tokens(\n'
                '        request_body,\n'
                '        model_info,\n'
                '        min(DEFAULT_MAX_OUTPUT_TOKENS_FALLBACK if requested is None else requested, model_ceiling),\n'
                '        model_ceiling,\n'
                '    )\n',
                1,
            ),
            (
                '    return next((tokens for tokens in map(_to_int, candidates) if tokens is not None), None)\n',
                '    # Woow P1: bool, 0 and negative values fall back to the default estimate instead of shrinking it.\n'
                '    return next((tokens for tokens in map(_woow_guard.positive_token_count, candidates) if tokens is not None), None)\n',
                1,
            ),
        ),
    ),
    'proxy/health_endpoints/_health_endpoints.py': (
        '8ea1e58c7ee57f77e21e7dd4e711f18ef135a485e38564cf0958ff36c0e92a03',
        '5ff405b436493823c8ca6276f58a52405f151e658da3b75472f86cb4f4a01432',
        ((
            '            out[key] = [({k: v for k, v in ep.items() if k not in drop} if isinstance(ep, dict) else ep) for ep in eps]\n',
            '            # Woow P0: non-admin callers also lose the raw request (api_base) and the stack trace.\n'
            '            from litellm.litellm_core_utils.woow_provider_errors import health_endpoint_for_non_admin\n'
            '\n'
            '            out[key] = [(health_endpoint_for_non_admin(ep, drop) if isinstance(ep, dict) else ep) for ep in eps]\n',
            1,
        ),),
    ),
    'proxy/auth/auth_exception_handler.py': (
        '9da190e8c1f5db0287f420c20e875069eae16b6af76b8188378600e7347998a2',
        '688c415b964dbc0b70d5b3dcdb8ae33fc3cbd1e6c6db4c657259a2ac16cc86e0',
        ((
            '            type=ProxyErrorTypes.budget_exceeded,\n'
            '            param=None,\n'
            '            code=getattr(e, "status_code", status.HTTP_429_TOO_MANY_REQUESTS),\n'
            '        )\n',
            '            type=ProxyErrorTypes.budget_exceeded,\n'
            '            param=None,\n'
            '            code=getattr(e, "status_code", status.HTTP_429_TOO_MANY_REQUESTS),\n'
            '            headers={"x-should-retry": "false"},  # Woow P1: retrying cannot succeed\n'
            '        )\n',
            1,
        ), (
            '                exc_info=True if litellm.log_client_error_tracebacks or not is_expected_client_error(e) else None,\n'
            '                extra=log_extra,\n'
            '            )\n',
            '                exc_info=True if litellm.log_client_error_tracebacks or not is_expected_client_error(e) else None,\n'
            '                extra=log_extra,\n'
            '            )\n'
            '            # Woow D1: the server log above keeps the key hash for admins; the client, failure\n'
            '            # callbacks and the spend log get the message without it (masked sk-...last4 stays).\n'
            '            from litellm.proxy.auth.woow_auth_errors import scrub_auth_exception\n'
            '\n'
            '            scrub_auth_exception(e)\n',
            1,
        ), (
            '            final_exception: Final = mark_invalid_virtual_key_error(_as_proxy_exception(e), is_invalid_virtual_key)\n',
            '            # Woow D1: also covers an error a failure callback returned in place of the original.\n'
            '            final_exception: Final = mark_invalid_virtual_key_error(\n'
            '                scrub_auth_exception(_as_proxy_exception(e)), is_invalid_virtual_key\n'
            '            )\n',
            1,
        ),),
    ),
    'proxy/hooks/parallel_request_limiter_v3.py': (
        '90c865d2b1912ef25c90f988ccd5b56b452edc5f0e57c7320b864c542dd9b8ec',
        '5e155c3dbbd3c9944b0b03ab1b84bb50aaf00199abade37ea04eb62d1377a316',
        ((
            '                detail = (\n'
            '                    f"Rate limit exceeded for {descriptor_key}: {descriptor_value}. "\n',
            '                # Woow D1: api_key, model_per_key, tag_per_key and mcp_per_key values carry the key hash.\n'
            '                from litellm.proxy.auth.woow_auth_errors import scrub_key_hash\n'
            '\n'
            '                detail = (\n'
            '                    f"Rate limit exceeded for {descriptor_key}: {scrub_key_hash(descriptor_value)}. "\n',
            1,
        ),),
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
        raise SystemExit('Usage: apply_upstream_guard.py [litellm-package-root]')
    root = Path(sys.argv[1]) if len(sys.argv) == 2 else Path(
        next(iter(importlib.util.find_spec('litellm').submodule_search_locations))
    )
    apply(root)
