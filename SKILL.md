---
name: woow-podman-litellm
description: Deploy and operate WOOW LiteLLM 1.104.0 with PostgreSQL 17 on rootless Podman Quadlet.
---

# WOOW LiteLLM Podman runbook

Read [README.md](README.md) and [docs/alignment.md](docs/alignment.md) before operating the stack.

## Install and verify

```bash
scripts/install.sh --dry-run
scripts/install.sh
tests/smoke.sh
python3 tests/verify-sources.py
systemctl --user status litellm litellm-postgres litellm-backup.timer
```

Use a real user systemd session with `XDG_RUNTIME_DIR=/run/user/$(id -u)`. The UI is `http://127.0.0.1:4000/ui` by default. The bootstrap username comes from `LITELLM_UI_USERNAME` (default `admin`); the password is Podman secret `litellm-ui-password`. The API master key is a different secret. Do not print credentials in logs or responses.

Models and provider credentials are managed through `/ui`. No model is preconfigured. ChatGPT subscription accounts are managed through `/woow/chatgpt`; the account API requires proxy admin authentication. Provider authentication is needed only when testing a real model request.

## Data and upgrades

```bash
scripts/backup.sh
scripts/upgrade.sh
scripts/rotate-secrets.sh --master
scripts/rotate-secrets.sh --ui
```

Backups include the salt key and UI/API credentials; keep a protected off-host copy. `scripts/restore.sh <backup> --with-secrets --yes` replaces the target database. A PG16 volume cannot be started by this PG17 package; use the dump-and-restore path in [docs/alignment.md](docs/alignment.md#postgresql-16-to-17). Never rotate the salt key independently from its database.

The daily backup timer runs at 19:00 UTC and retains seven daily copies. `scripts/backup.sh` and `scripts/upgrade.sh` produce additional manual or pre-upgrade copies that the timer does not prune.
