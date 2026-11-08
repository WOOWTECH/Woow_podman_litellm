"""Fail-closed build patch: a streamed request gives its max_parallel_requests slot back (M4 API-09).

S1  With an SSE keepalive interval (litellm_settings.sse_keepalive_ping_interval_seconds; the PaaS
    chart sets 15 s from 0.3.10 on), open_sse_before_first_byte runs the whole request, pre-call hooks
    included, in a task of its own (produce_task). parallel_request_limiter_v3 keeps the request's
    max_parallel_requests slot (with its TPM reservation and rate-limit headers) on a ContextVar stash
    that its pre-call hook creates lazily, so inside that task; the request task that then serves the
    StreamingResponse never sees it. The success callback at the end of the stream and the disconnect
    cleanup look the stash up there, find nothing and release nothing: every streamed request keeps its
    slot until PARALLEL_REQUEST_SLOT_TTL_SECONDS (3600 s). Fix: create the stash in the request task
    before the fork; the child task fills the same shared object.
S2  When the client leaves during a time-to-first-token wait longer than the interval (after a
    ': ping'), the keepalive relay cancels produce_task while the upstream call is still pending, and a
    CancelledError runs no callback. Fix: when the request is cancelled, release its slot and reconcile
    its budget reservation (the two releases the streaming generator runs on a disconnect; both are
    idempotent), then re-raise the cancellation.

Runs last, after apply_m4_batch1.py, on both the HA and the PaaS chains. Its one target,
proxy/common_request_processing.py, is the reviewed apply_m4_batch1.py output on both. The helper calls
into the rate limiter and the budget reservation as apply_upstream_guard.py installs them and into
ProxyLogging (proxy/utils.py, pristine 1.104.0, edited by no installer): those three are checked as
dependencies. router.py (HA-only subscription policy) is not involved, so both chains give the same
inputs. The HA add-on sets no keepalive interval: there S1 is never reached and S2 only acts on a
cancelled request. Validate every dependency, input, replacement count, output hash, the helper and
Python syntax before writing anything. One-shot: a second application intentionally fails closed.
"""
import hashlib
import importlib.util
from pathlib import Path
import sys

# installed path: (source file next to this installer, reviewed SHA-256)
HELPERS = {
    'proxy/woow_stream_slots.py': (
        'woow_stream_slots.py',
        'e5f3eb785d8d8f2ac530efca090c6f4836259cbb07f238d46838d1761a7b0c2f',
    ),
}

# What the helper calls into: must be exactly the reviewed files.
DEPENDENCIES = {
    # get_or_create_request_stash, the stash and its idempotent slot release (apply_upstream_guard.py output)
    'proxy/hooks/parallel_request_limiter_v3.py': '5e155c3dbbd3c9944b0b03ab1b84bb50aaf00199abade37ea04eb62d1377a316',
    # release_budget_reservation_on_cancel (apply_upstream_guard.py output)
    'proxy/spend_tracking/budget_reservation.py': 'e6e75364b9a31a1f3608c3383e53ed31dcef36b3db844aef7a811950c8e18c53',
    # ProxyLogging._arelease_max_parallel_requests_on_disconnect (pristine LiteLLM 1.104.0)
    'proxy/utils.py': '81e6fdba0c204a852883ac96a1b47ec2254c9fe97547f7e05bd0110e0d90933f',
}

# relative path: (reviewed input SHA-256, reviewed output SHA-256, edits)
PATCHES = {
    'proxy/common_request_processing.py': (
        '2c7f9e0fe459df8bae59bb3f565153c08d90ee03f8475b3e8dd883369eb45de9',  # apply_m4_batch1.py output
        'a3ad784b095a9e8aa0eb2b582737d7999628aae0d4bfedf7722bcab751a0a7d8',
        (
            (  # S1: open_sse_before_first_byte, once the keepalive path is taken
                '    interval: Final = coerce_keepalive_interval(ping_interval_seconds)\n'
                '    if interval is None:\n'
                '        return await produce_response\n'
                '\n'
                '    produce_task: Final = asyncio.ensure_future(produce_response)\n',
                '    interval: Final = coerce_keepalive_interval(ping_interval_seconds)\n'
                '    if interval is None:\n'
                '        return await produce_response\n'
                '\n'
                '    # Woow API-09: produce_response runs in its own task from here on. The rate limiter keeps the\n'
                '    # max_parallel_requests slot on a ContextVar stash its pre-call hook creates lazily; created in\n'
                '    # that task it never reaches the task serving the stream. Create it before the fork.\n'
                '    from litellm.proxy.woow_stream_slots import ensure_request_stash\n'
                '\n'
                '    ensure_request_stash()\n'
                '    produce_task: Final = asyncio.ensure_future(produce_response)\n',
                1,
            ),
            (  # S2: base_process_llm_request wraps the request it hands to open_sse_before_first_byte
                '        return await open_sse_before_first_byte(\n'
                '            self._process_llm_request(\n',
                '        # Woow API-09: the keepalive relay cancels this request when the client leaves before the\n'
                '        # first token; no failure callback runs for that, so the slot is released on cancellation.\n'
                '        from litellm.proxy.woow_stream_slots import release_slot_if_cancelled\n'
                '\n'
                '        return await open_sse_before_first_byte(\n'
                '            release_slot_if_cancelled(\n'
                '                proxy_logging_obj,\n'
                '                user_api_key_dict,\n'
                '                self._process_llm_request(\n',
                1,
            ),
            (  # S2: closes the wrapper
                '                skip_pre_call_logic=skip_pre_call_logic,\n'
                '            ),\n'
                '            ping_interval_seconds=ttft_keepalive_interval(self.data, llm_router),\n',
                '                skip_pre_call_logic=skip_pre_call_logic,\n'
                '                ),\n'
                '            ),\n'
                '            ping_interval_seconds=ttft_keepalive_interval(self.data, llm_router),\n',
                1,
            ),
        ),
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
        raise SystemExit('Usage: apply_stream_slot_release.py [litellm-package-root]')
    root = Path(sys.argv[1]) if len(sys.argv) == 2 else Path(
        next(iter(importlib.util.find_spec('litellm').submodule_search_locations))
    )
    apply(root)
