"""HA-only in-memory deployment gate for pinned LiteLLM 1.104.0.

Router construction authenticates ChatGPT before HTTP startup. Exclude these
models before any provider initialization when the subscription option is off.
The periodic DB updater calls upsert -> add_deployment; cover that path too.
"""
import hashlib
import importlib.util
from pathlib import Path
import sys

ROUTER_SHA256 = '5f700100a802aa77c74b72a63a161ea9b486891ebb8258e8448030c04e28d343'
EDITS = (
    (
        '    def set_model_list(self, model_list: list):\n',
        '    def set_model_list(self, model_list: list):\n'
        '        from litellm.woow_subscription_policy import deployment_enabled\n'
        '        model_list = [m for m in model_list if deployment_enabled(m["litellm_params"])]\n',
    ),
    (
        '        # check if deployment already exists\n',
        '        from litellm.woow_subscription_policy import deployment_enabled\n'
        '        if not deployment_enabled(deployment.litellm_params):\n'
        '            return None\n'
        '        # check if deployment already exists\n',
    ),
    (
        '        _deployment_model_id: Final = deployment.model_info.id or ""\n',
        '        from litellm.woow_subscription_policy import deployment_enabled\n'
        '        if not deployment_enabled(deployment.litellm_params):\n'
        '            return None\n'
        '        _deployment_model_id: Final = deployment.model_info.id or ""\n',
    ),
)


def apply(root):
    path = root/'router.py'
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != ROUTER_SHA256:
        raise SystemExit('Refusing changed upstream Router source')
    result = raw.decode()
    for old, new in EDITS:
        if result.count(old) != 1:
            raise SystemExit('Refusing changed Router policy context')
        result = result.replace(old, new)
    helper_path = root/'woow_subscription_policy.py'
    if helper_path.exists():
        raise SystemExit('Refusing existing subscription policy helper')
    helper = Path(__file__).with_name('woow_subscription_policy.py').read_text()
    compile(result, str(path), 'exec')
    compile(helper, str(helper_path), 'exec')
    path.write_text(result)
    helper_path.write_text(helper)
    print('HA subscription deployment policy applied (pinned Router)')


if __name__ == '__main__':
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(next(iter(importlib.util.find_spec('litellm').submodule_search_locations)))
    apply(root)
