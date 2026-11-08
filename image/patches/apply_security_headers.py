"""One-shot, hash-guarded LiteLLM 1.104.0 middleware-only build patch.

No proxy/auth/router/plugin/Dockerfile changes; compose with their independent
patches at image build time. Refuse unknown inputs and modified payloads.
"""
import hashlib
import importlib.util
from pathlib import Path
import sys

TARGET = 'proxy/middleware/security_headers_middleware.py'
EXPECTED = '961034ee861a8833b564f1921413a82e8e41e17daf561f9f6db99f3e053176a5'
RESULT = '7eae4c58ca47b08600fef20ba0d72241a48bfa67571731ed611574b2072897c8'


def apply(root):
    target = Path(root) / TARGET
    payload = Path(__file__).with_name('security_headers') / 'security_headers_middleware.py'
    if target.is_symlink() or not target.is_file():
        raise SystemExit('Refusing missing or symlink middleware target')
    before = target.read_bytes()
    if hashlib.sha256(before).hexdigest() != EXPECTED:
        raise SystemExit('Refusing changed upstream security middleware')
    after = payload.read_bytes()
    if hashlib.sha256(after).hexdigest() != RESULT:
        raise SystemExit('Refusing modified security middleware payload')
    compile(after, TARGET, 'exec')
    target.write_bytes(after)
    print(TARGET + ': ' + RESULT)


if __name__ == '__main__':
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        next(iter(importlib.util.find_spec('litellm').submodule_search_locations))
    )
    apply(root)
