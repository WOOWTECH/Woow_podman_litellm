"""Create the shared Woow tables and bind the encryption salt to this database."""
import os
import sys
import psycopg


def main():
    with psycopg.connect(
        host=os.environ["DATABASE_HOST"],
        user=os.environ["DATABASE_USERNAME"],
        password=os.environ["DATABASE_PASSWORD"],
        dbname=os.environ["DATABASE_NAME"],
        connect_timeout=10,
    ) as db:
        with db.transaction():
            db.execute("CREATE SCHEMA IF NOT EXISTS woow_platform")
            db.execute("""CREATE TABLE IF NOT EXISTS woow_platform.settings (
                key text PRIMARY KEY, value text NOT NULL,
                updated_at timestamptz NOT NULL DEFAULT now())""")
            db.execute("""CREATE TABLE IF NOT EXISTS woow_platform.chatgpt_auth (
                id smallint PRIMARY KEY CHECK (id = 1), auth jsonb NOT NULL,
                updated_at timestamptz NOT NULL DEFAULT now())""")
            db.execute("""CREATE TABLE IF NOT EXISTS woow_platform.chatgpt_accounts (
                account text PRIMARY KEY, auth jsonb NOT NULL,
                updated_at timestamptz NOT NULL DEFAULT now())""")
            db.execute("""INSERT INTO woow_platform.settings (key, value)
                VALUES ('litellm_salt_key', %s) ON CONFLICT (key) DO NOTHING""",
                (os.environ["LITELLM_SALT_KEY"],))
            row = db.execute("SELECT value FROM woow_platform.settings WHERE key = 'litellm_salt_key'").fetchone()
            if not row or not row[0] or row[0] != os.environ["LITELLM_SALT_KEY"]:
                raise ValueError("database salt differs from Podman secret; restore the matching salt")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("db-prepare: failed (" + type(exc).__name__ + "); refusing startup", file=sys.stderr)
        sys.exit(1)
