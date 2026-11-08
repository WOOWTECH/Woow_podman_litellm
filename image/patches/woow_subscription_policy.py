"""Runtime deployment policy; no plugin import, routes, or persistent writes."""
import os
from pathlib import Path

POLICY_PATH = Path('/run/woow/chatgpt-subscription')


def deployment_enabled(params):
    # Both DB loading and Router config loading can contain env references.
    from litellm import get_secret

    def resolved(key):
        value = params.get(key)
        if isinstance(value, str) and value.startswith('os.environ/'):
            value = get_secret(value)
        return value

    # Match pinned upstream precedence without invoking its authenticating resolver.
    provider = resolved('custom_llm_provider')
    if provider:
        is_subscription = provider == 'chatgpt'
    else:
        model = resolved('model')
        is_subscription = isinstance(model, str) and model.startswith('chatgpt/')
    if not is_subscription:
        return True
    # The Podman image includes the root-owned flag; the runtime setting can still
    # disable ChatGPT without rebuilding the image. Missing/invalid policy fails closed.
    if os.environ.get('WOOW_CHATGPT_ENABLED') != 'true':
        return False
    try:
        return POLICY_PATH.read_bytes() == b'enabled\n'
    except OSError:
        return False
