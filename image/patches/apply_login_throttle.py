"""One-shot downstream LiteLLM 1.104.0 login-security patch.

Only login_throttle.py changes. Unknown, already-patched and symlink inputs fail
closed. No imports of LiteLLM (and no runtime services) are needed to install.
"""
import hashlib
import importlib.util
from pathlib import Path
import sys

TARGET = "proxy/auth/login_throttle.py"
EXPECTED = "f7d95417ff154622b436cbb306720492c20b94b7950d81be21cf862746aed6d9"
RESULT = "87f280351406b2b308c42ef28fe95a8b7b09b849c2e2e4f45f30c56bd3b6decb"

LOGGER = '''
# Only these two security events bypass the verbose logger's ERROR threshold.
# Never pass usernames, addresses, exception text or credential data here.
_security_logger: Final = logging.getLogger("woow.security.login_throttle")
_security_logger.setLevel(logging.WARNING)
_security_logger.propagate = False
if not _security_logger.handlers:
    _security_handler = logging.StreamHandler()
    _security_handler.setLevel(logging.WARNING)
    _security_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    _security_logger.addHandler(_security_handler)

'''

REFUSED = '''        verbose_proxy_logger.warning(
            "Admin UI sign-in refused: the %s is blocked for %s more seconds; username=%r source=%s",
            block.scope,
            block.retry_after,
            username,
            self.client_ip,
        )'''
SAFE_REFUSED = '''        _security_logger.warning(
            "Admin UI sign-in refused: the %s is blocked for %s more seconds; "
            "username=[redacted] source=[redacted]",
            block.scope,
            block.retry_after,
        )'''
BLOCKED = '''        verbose_proxy_logger.warning(
            "Admin UI sign-in blocked for %s seconds after too many failures; scope=%s username=%r source=%s",
            user_block or source_block,
            "user" if user_block else "source",
            self.username,
            self.throttle.client_ip,
        )'''
SAFE_BLOCKED = '''        _security_logger.warning(
            "Admin UI sign-in blocked for %s seconds after too many failures; "
            "scope=%s username=[redacted] source=[redacted]",
            user_block or source_block,
            "user" if user_block else "source",
        )
        # The failure which crosses the limit must itself return 429, not 401.
        raise self.throttle.refused(user_block or source_block)'''


def patched_source(before):
    if hashlib.sha256(before).hexdigest() != EXPECTED:
        raise SystemExit("Refusing changed upstream login throttle")
    text = before.decode("utf-8")
    for old, new in (
        ("import ipaddress\n", "import ipaddress\nimport logging\n"),
        ("Scope: TypeAlias = Literal[\"user\", \"source\"]\n", LOGGER + 'Scope: TypeAlias = Literal["user", "source"]\n'),
        (REFUSED, SAFE_REFUSED),
        (BLOCKED, SAFE_BLOCKED),
    ):
        if text.count(old) != 1:
            raise SystemExit("Refusing changed login throttle patch context")
        text = text.replace(old, new, 1)
    after = text.encode("utf-8")
    if hashlib.sha256(after).hexdigest() != RESULT:
        raise SystemExit("Refusing changed login throttle patch result")
    compile(after, TARGET, "exec")
    return after


def apply(root):
    root = Path(root)
    target = root / TARGET
    if root.is_symlink() or any(p.is_symlink() for p in (root / "proxy", root / "proxy/auth", target)):
        raise SystemExit("Refusing symlink login throttle target")
    if not target.is_file():
        raise SystemExit("Refusing missing login throttle target")
    after = patched_source(target.read_bytes())
    target.write_bytes(after)
    print(TARGET + ": " + RESULT)


if __name__ == "__main__":
    if len(sys.argv) > 2:
        raise SystemExit("usage: apply_login_throttle.py [litellm-package-root]")
    root = Path(sys.argv[1]) if len(sys.argv) == 2 else Path(
        next(iter(importlib.util.find_spec("litellm").submodule_search_locations))
    )
    apply(root)
