"""Bounded compatibility patch for the pinned LiteLLM 1.104.0 image.

ChatGPT requires SSE upstream even for non-stream clients. Keep that wire body,
but select the buffered parser for non-stream callers so its output recovery runs.
Fail before modifying anything when the reviewed upstream source has changed.
"""
import hashlib
import importlib.util
from pathlib import Path
import sys

PATCHES = {
    'llms/chatgpt/responses/transformation.py': (
        'b1a66c0e15bb882ee394273ca8e24854327f33b2f9d3e4bcc506c48c4e19ed85',
        '        request: Final = super().transform_responses_api_request(\n',
        '        # ChatGPT accepts a message array; the public Responses API also accepts text.\n'
        '        if isinstance(input, str):\n'
        '            input = [{"role": "user", "content": input}]\n'
        '        request: Final = super().transform_responses_api_request(\n',
        1,
    ),
    'llms/custom_httpx/llm_http_handler.py': (
        'a7958b892ba4b309e451297d87be4c0bb341f7e56f297cb710ceee1539a53c8a',
        '        stream = bool(stream or data.get("stream"))\n',
        '        # ChatGPT forces SSE in its upstream body, not in the client response.\n'
        '        # Non-stream callers use its buffered SSE parser and output recovery.\n'
        '        if custom_llm_provider != "chatgpt":\n'
        '            stream = bool(stream or data.get("stream"))\n',
        2,
    ),
    'completion_extras/litellm_responses_transformation/handler.py': (
        '6402f98b522691db939bc54e8fdbb2998cdcdb28c1cd71920489891e8f9dcaeb',
        '        for _ in stream_iter:\n'
        '            pass\n',
        '        from litellm.responses.woow_output_recovery import OutputRecovery\n'
        '        recovery = OutputRecovery() if getattr(stream_iter, "custom_llm_provider", None) == "chatgpt" else None\n'
        '        for event in stream_iter:\n'
        '            if recovery is not None:\n'
        '                recovery.feed(event)\n',
        1,
    ),
    'responses/streaming_iterator.py': (
        '6f1340592f0f06d5eb82712f7cd765e2b0e673de3d0056bf5d4e8cccddfd5a27',
        '            else getattr(result, "error", None)\n',
        '            else (getattr(result, "error", None) or (result if self.custom_llm_provider == "chatgpt" else None))\n',
        1,
    ),
}

# This helper is reused without modification, but its behavior is part of the patch contract.
SOURCE_DEPENDENCIES = {
    "responses/sse_output_recovery.py": "0dbeb282d16f18851708bcc4751c883ac406fdde66ed3a089093eb82d3594bf3",
}

RESPONSE_EDITS = (
    (
        'self._extract_completed_response_from_sse(body_text=body_text)',
        'self._extract_completed_response_from_sse(body_text=body_text, model=model)',
        1,
    ),
    (
        'message=error_message or raw_response.text,\n'
        '                status_code=raw_response.status_code,',
        'message="ChatGPT response stream ended without a terminal response",\n'
        '                status_code=502,',
        1,
    ),
    (
        'def _extract_completed_response_from_sse(self, body_text: str)',
        'def _extract_completed_response_from_sse(self, body_text: str, model: str = "")',
        1,
    ),
    (
        'if event_type == ResponsesAPIStreamEvents.RESPONSE_COMPLETED:',
        'if event_type in (ResponsesAPIStreamEvents.RESPONSE_COMPLETED, ResponsesAPIStreamEvents.RESPONSE_INCOMPLETE):',
        1,
    ),
    (
        '                extracted_error = self._extract_error_message(parsed_chunk)\n'
        '                if extracted_error is not None:\n'
        '                    error_message = extracted_error',
        '                from litellm.responses.streaming_iterator import _map_stream_error_to_exception\n'
        '                error_obj = parsed_chunk.get("error") or (parsed_chunk.get("response") or {}).get("error")\n'
        '                if error_obj is None and event_type == ResponsesAPIStreamEvents.ERROR:\n'
        '                    error_obj = {key: parsed_chunk.get(key) for key in ("message", "code", "param")}\n'
        '                raise _map_stream_error_to_exception(error_obj, model, "chatgpt")',
        1,
    ),
    (
        '        streamed_output_items: Final[dict[int, dict[str, object]]] = {}\n'
        '        text_only_output_items: Final[dict[int, dict[str, object]]] = {}\n',
        '        from litellm.responses.woow_output_recovery import OutputRecovery\n'
        '        recovery = OutputRecovery()\n',
        1,
    ),
    (
        '            if event_type == ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE:\n'
        '                record_output_item_chunk(\n'
        '                    parsed_chunk=parsed_chunk,\n'
        '                    output_items=streamed_output_items,\n'
        '                )\n'
        '                continue\n'
        '\n'
        '            if event_type == ResponsesAPIStreamEvents.OUTPUT_TEXT_DONE:\n'
        '                record_output_text_chunk(\n'
        '                    parsed_chunk=parsed_chunk,\n'
        '                    output_items=streamed_output_items,\n'
        '                    text_only_items=text_only_output_items,\n'
        '                )\n'
        '                continue\n'
        '\n',
        '            recovery.feed(parsed_chunk)\n'
        '\n',
        1,
    ),
    (
        '                # Real OUTPUT_ITEM_DONE events take precedence at any given\n'
        '                # output_index, but text-only items at indices without a\n'
        '                # matching OUTPUT_ITEM_DONE must still be preserved (e.g.\n'
        '                # providers that emit only OUTPUT_TEXT_DONE for some indices).\n'
        '                merged_items: dict[int, dict[str, object]] = {**text_only_output_items}\n'
        '                merged_items.update(streamed_output_items)\n',
        '                terminal = parsed_chunk.get("response")\n'
        '                merged_items = recovery.items(terminal.get("output") if isinstance(terminal, dict) else None)\n',
        1,
    ),
)

COLLECTOR_EDITS = (
    (
        '        async for _ in stream_iter:\n'
        '            pass\n',
        '        from litellm.responses.woow_output_recovery import OutputRecovery\n'
        '        recovery = OutputRecovery() if getattr(stream_iter, "custom_llm_provider", None) == "chatgpt" else None\n'
        '        async for event in stream_iter:\n'
        '            if recovery is not None:\n'
        '                recovery.feed(event)\n',
        1,
    ),
    (
        '            raise ValueError("Stream completed response is invalid")\n'
        '        return response\n',
        '            raise ValueError("Stream completed response is invalid")\n'
        '        return recovery.restore(response) if recovery is not None else response\n',
        2,
    ),
)

def apply(root):
    pending=[]
    for name,(expected,old,new,count) in PATCHES.items():
        path=root/name
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=expected:
            raise SystemExit('Refusing changed upstream source: '+name)
        source=raw.decode()
        if source.count(old)!=count:
            raise SystemExit('Refusing changed patch context: '+name)
        result=source.replace(old,new)
        edits = RESPONSE_EDITS if name == 'llms/chatgpt/responses/transformation.py' else (
            COLLECTOR_EDITS if name == 'completion_extras/litellm_responses_transformation/handler.py' else ()
        )
        for original,replacement,expected_count in edits:
            if result.count(original) != expected_count:
                raise SystemExit('Refusing changed recovery context: '+name)
            result=result.replace(original,replacement)
        compile(result,str(path),'exec')
        pending.append((path,result))
    for name,expected in SOURCE_DEPENDENCIES.items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest() != expected:
            raise SystemExit('Refusing changed recovery dependency: '+name)
    helper_path = root / 'responses/woow_output_recovery.py'
    if helper_path.exists():
        raise SystemExit('Refusing existing recovery helper')
    helper_source = Path(__file__).with_name('woow_output_recovery.py').read_text()
    compile(helper_source, str(helper_path), 'exec')
    pending.append((helper_path, helper_source))
    for path,result in pending:
        path.write_text(result)
        print(path.name+': '+hashlib.sha256(path.read_bytes()).hexdigest())

if __name__=='__main__':
    root=Path(sys.argv[1]) if len(sys.argv)>1 else Path(next(iter(importlib.util.find_spec('litellm').submodule_search_locations)))
    apply(root)
