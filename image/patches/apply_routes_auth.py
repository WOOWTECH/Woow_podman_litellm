"""Require standard authentication for /routes in pinned LiteLLM 1.104.0.
Apply after apply_api_compat.py. Does not change health routes or header middleware.
One-shot: unknown inputs, symlinks and repeated application fail without writing.
"""
from pathlib import Path
import hashlib,sys
import importlib.util
TARGET='proxy/_types.py'
EXPECTED='ef49572eafbfbb5819617feb8ee9a42e1c9b20b87ca58e764054a71319583923'
RESULT='d43c827c6ce1bbdaa2cc6282f637364c20d04109bdc102f2a716660a359c0247'
BEFORE='    public_routes = frozenset(\n        (\n            "/routes",\n'
AFTER='    public_routes = frozenset(\n        (\n'

def apply(root):
 target=Path(root)/TARGET
 if target.is_symlink() or not target.is_file():raise SystemExit('Refusing missing or symlink target')
 before=target.read_bytes()
 if hashlib.sha256(before).hexdigest()!=EXPECTED:raise SystemExit('Refusing changed route types source; requires API patch first')
 text=before.decode()
 if text.count(BEFORE)!=1:raise SystemExit('Refusing unexpected public routes anchor')
 after=text.replace(BEFORE,AFTER,1).encode()
 if hashlib.sha256(after).hexdigest()!=RESULT:raise SystemExit('Refusing unexpected output')
 compile(after,TARGET,'exec')
 target.write_bytes(after)
 return RESULT

if __name__=='__main__':
 if len(sys.argv)>2:raise SystemExit('Usage: apply_routes_auth.py [litellm-package-root]')
 if len(sys.argv)==2:
  root=Path(sys.argv[1])
 else:
  spec=importlib.util.find_spec('litellm')
  if spec is None or not spec.submodule_search_locations:raise SystemExit('LiteLLM package missing')
  root=Path(next(iter(spec.submodule_search_locations)))
 print(apply(root))
