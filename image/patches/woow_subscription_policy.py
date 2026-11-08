"""HA runtime deployment policy; no plugin import, routes, or persistent writes."""
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
    # init-woow-env writes this root-owned tmpfs file before LiteLLM starts.
    # Missing/unreadable/invalid policy fails closed for subscription deployments.
    try:
        return POLICY_PATH.read_bytes() == b'enabled\n'
    except OSError:
        return False
