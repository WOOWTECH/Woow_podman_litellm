"""Recover only observed output; a terminal response remains mandatory.

Installed beside LiteLLM's SSE helpers by apply_responses_fix.py. Completed
payload > item.done > text.done > text deltas. Never retry or invent content.
"""
from copy import deepcopy

from litellm.responses.sse_output_recovery import (
    record_output_item_chunk,
    record_output_text_chunk,
)


class OutputRecovery:
    def __init__(self):
        self._items = {}
        self._text = {}
        self._deltas = {}
        self._done = set()
        self._observed_tools = set()
        self._resolved_tools = set()

    def _observe_tool(self, event, item_id, tool_type, completed=False):
        index = event.get('output_index')
        valid = type(index) is int and index >= 0 and isinstance(item_id, str) and bool(item_id)
        key = (index if type(index) is int else None,
               item_id if isinstance(item_id, str) else None, tool_type)
        self._observed_tools.add(key)
        if completed and valid:
            self._resolved_tools.add(key)

    def feed(self, event):
        if not isinstance(event, dict):
            event = event.model_dump(mode='json')
        kind = event.get('type')
        if kind in ('response.output_item.added', 'response.output_item.done'):
            item = event.get('item')
            if isinstance(item, dict) and item.get('type') not in ('message', 'reasoning'):
                complete = kind == 'response.output_item.done'
                if item.get('type') == 'function_call':
                    complete = complete and all(isinstance(item.get(field), str) and item[field]
                                                for field in ('name', 'call_id'))
                    complete = complete and isinstance(item.get('arguments'), str)
                self._observe_tool(event, item.get('id'), item.get('type'), complete)
        elif kind in ('response.function_call_arguments.delta', 'response.function_call_arguments.done'):
            # Argument completion does not complete the output item. Do not
            # guess tool metadata or silently return only the accompanying text.
            self._observe_tool(event, event.get('item_id'), 'function_call')
        elif kind in ('response.custom_tool_call_input.delta', 'response.custom_tool_call_input.done'):
            self._observe_tool(event, event.get('item_id'), 'custom_tool_call')
        if kind == 'response.output_item.done':
            record_output_item_chunk(event, self._items)
        elif kind in ('response.output_text.done', 'response.output_text.delta'):
            output_index = event.get('output_index')
            content_index = event.get('content_index')
            # SSE text events must identify their slot. Bound sparse allocation.
            if any(type(index) is not int or not 0 <= index <= 1024
                   for index in (output_index, content_index)):
                return
            key = (output_index, content_index)
            if kind == 'response.output_text.done':
                if not isinstance(event.get('text'), str):
                    return
                self._done.add(key)
            else:
                delta = event.get('delta')
                if key in self._done or not isinstance(delta, str) or not delta:
                    return
                self._deltas[key] = self._deltas.get(key, '') + delta
                event = {**event, 'text': self._deltas[key]}
            record_output_text_chunk(event, self._items, self._text)

    def items(self, terminal_output=None):
        # A nonempty authoritative terminal needs no event-based reconstruction.
        if terminal_output:
            return {}
        available = {(index, item.get('id'), item.get('type'))
                     for index, item in self._items.items()}
        if self._observed_tools - (self._resolved_tools & available):
            raise ValueError('Cannot recover ChatGPT response with unresolved tool output')
        return {**self._text, **self._items}

    def restore(self, response):
        # A real terminal with no observed output stays empty. The chat bridge
        # will continue to reject completed=[] rather than report false success.
        if response.output or response.status not in ('completed', 'incomplete'):
            return response
        recovered = self.items()
        if not recovered:
            return response
        data = response.model_dump()
        data['output'] = [deepcopy(item) for _, item in sorted(recovered.items())]
        if response.status == 'incomplete':
            for item in data['output']:
                if item.get('type') == 'message':
                    item['status'] = 'incomplete'
        validated = type(response)(**data)
        # Preserve private headers/routing metadata and all terminal usage fields.
        return response.model_copy(update={'output': validated.output})
