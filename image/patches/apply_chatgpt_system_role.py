"""Fail-closed build patch for ChatGPT Responses system input compatibility.

Runs after apply_responses_fix.py. Both the HA and PaaS
chains produce the same input hash. Only ChatGPT message roles are normalized;
no generic OpenAI behavior, auth, routing, retries or instruction text changes.
A second application or unexpected source is rejected before writing.
"""
import hashlib
import importlib.util
from pathlib import Path
import sys

PATCHES = {'llms/chatgpt/responses/transformation.py': ('f20de0053f82f2fc1826b4bef778c02d8235fe02c0fca50dc560bea98a4eb258',
                                              '664413197293c7a59f34184db9d7ec68e80531ad6f727e8c350c9f09e901763c',
                                              (('        base_instructions: Final = '
                                                'get_chatgpt_default_instructions()\n',
                                                '        # The ChatGPT backend rejects system '
                                                'input messages. Preserve their\n'
                                                '        # content and position as developer '
                                                'instructions, including empty content.\n'
                                                '        # Copy changed messages so caller-owned '
                                                'conversation history is untouched.\n'
                                                '        if isinstance(request.get("input"), '
                                                'list):\n'
                                                '            request["input"] = [\n'
                                                '                {**item, "role": "developer"}\n'
                                                '                if isinstance(item, Mapping)\n'
                                                '                and item.get("type", "message") '
                                                '== "message"\n'
                                                '                and item.get("role") == "system"\n'
                                                '                else item\n'
                                                '                for item in request["input"]\n'
                                                '            ]\n'
                                                '        base_instructions: Final = '
                                                'get_chatgpt_default_instructions()\n',
                                                1),))}


def apply(root):
    pending = []
    for name, (expected, result_hash, edits) in PATCHES.items():
        path = Path(root) / name
        if path.is_symlink() or not path.is_file():
            raise SystemExit("Refusing missing or symlink target: " + name)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise SystemExit("Refusing changed upstream source: " + name)
        source = raw.decode("utf-8")
        for old, new, count in edits:
            if source.count(old) != count:
                raise SystemExit("Refusing changed patch context: " + name)
            source = source.replace(old, new)
        result = source.encode("utf-8")
        if hashlib.sha256(result).hexdigest() != result_hash:
            raise SystemExit("Refusing unexpected patched source: " + name)
        compile(source, str(path), "exec")
        pending.append((path, result))
    for path, result in pending:
        path.write_bytes(result)
        print(path.name + ": " + hashlib.sha256(result).hexdigest())


if __name__ == "__main__":
    if len(sys.argv) > 2:
        raise SystemExit("Usage: apply_chatgpt_system_role.py [litellm-package-root]")
    root = Path(sys.argv[1]) if len(sys.argv) == 2 else Path(
        next(iter(importlib.util.find_spec("litellm").submodule_search_locations))
    )
    apply(root)
