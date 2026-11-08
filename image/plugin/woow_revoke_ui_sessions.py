"""Revoke dashboard keys on credential changes before the proxy opens its socket.

The JWT and its database key have independent lifetimes. Rotating the signing
key or UI_PASSWORD alone does not invalidate the key inside an old UI cookie.
Only the reserved dashboard team is revoked; ordinary API keys are retained.
"""
import hashlib
import hmac
import json
import os
import sys

SETTING = "ui_credentials_fingerprint_v1"
UI_TEAM = "litellm-dashboard"


def credential_fingerprint(environ):
    """Salt-keyed digest: persist neither credentials nor separate password hashes."""
    salt = environ["LITELLM_SALT_KEY"]
    if not salt:
        raise ValueError("missing effective salt")
    values = [
        environ["LITELLM_MASTER_KEY"], environ.get("UI_USERNAME", "admin"), environ["UI_PASSWORD"],
        environ.get("WOOW_UI_SESSION_POLICY_VERSION", "1"),
    ]
    return hmac.new(salt.encode(), json.dumps(values, ensure_ascii=True).encode(), hashlib.sha256).hexdigest()


def reconcile(connection, fingerprint):
    """Delete and record atomically; failure prevents startup and permits retry."""
    with connection.transaction():
        # Also serializes adoption when the fingerprint row does not yet exist.
        connection.execute("SELECT pg_advisory_xact_lock(1827364501)")
        row = connection.execute(
            "SELECT value FROM woow_platform.settings WHERE key = %s FOR UPDATE", (SETTING,)
        ).fetchone()
        if row and hmac.compare_digest(row[0], fingerprint):
            return {"status": "unchanged", "revoked": 0}
        count = connection.execute(
            'DELETE FROM "LiteLLM_VerificationToken" WHERE team_id = %s', (UI_TEAM,)
        ).rowcount
        connection.execute(
            "INSERT INTO woow_platform.settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (SETTING, fingerprint),
        )
    # On adoption, expire legacy dashboard sessions once; later unchanged boots
    # preserve them. A restored DB is reconciled against the current Secret too.
    return {"status": "changed" if row else "adopted", "revoked": count}


def main():
    import psycopg

    host, separator, port = os.environ["DATABASE_HOST"].rpartition(":")
    if not separator:
        host, port = os.environ["DATABASE_HOST"], "5432"
    with psycopg.connect(
        host=host, port=int(port), dbname=os.environ["DATABASE_NAME"],
        user=os.environ["DATABASE_USERNAME"], password=os.environ["DATABASE_PASSWORD"],
        connect_timeout=10, options="-c statement_timeout=30000 -c lock_timeout=10000",
    ) as connection:
        result = reconcile(connection, credential_fingerprint(os.environ))
    print("ui-session-reconcile: " + json.dumps(result), file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Connection errors may contain credential URLs: never print the message.
        print("ui-session-reconcile: failed (" + type(exc).__name__ + "); refusing startup", file=sys.stderr)
        sys.exit(1)
