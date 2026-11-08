# LiteLLM source alignment (2026-11-08)

This Podman package was compared with:

- Gitea `woow-paas/woow-paas-charts` main `cbe71358fafb26fb46af79efd2dbc3c87293c64e`, `charts/litellm` Chart 0.3.16, appVersion 1.104.0.
- GitHub `WOOWTECH/Woow_ha_litellm` main `cf009e3e9909ece9b82d0649c2877c539e02d300`, add-on 0.1.15.
- The HA plugin lock names Gitea commit `b3e8371487794d8531d1b5c01f11969088cd166d`; all nine listed plugin files match this package's `image/plugin/` by SHA-256. PaaS main also contains the same files byte for byte. `woow_revoke_ui_sessions.py` is a tenth PaaS-only file included here.

| Area | Podman implementation | Source |
| --- | --- | --- |
| Proxy version | 1.104.0 non-root upstream image by digest, with locally built overlay | HA `litellm/Dockerfile`, PaaS `Chart.yaml` |
| Compatibility/security patches | Eleven hash-guarded patch installers in the same order as HA | HA `litellm/patches/`, `litellm/Dockerfile` |
| Python dependency repair | Hash-locked `multidict` 6.9.1 wheel overlay | PaaS `paas-odoo-ci/litellm/overlay/` at `5c41784675314062fee49bc1f47ad61249b725df` |
| Woow plugin | Exact nine locked files plus session revocation | PaaS `charts/litellm/files/`, HA `woow-plugin.lock.json` |
| Models and keys | Empty `model_list`; Admin UI and PostgreSQL own tenant models and provider credentials | PaaS `templates/configmap.yaml` |
| ChatGPT accounts | Multi-account login and routing; DB copy restored into tmpfs before HTTP starts; root-owned deployment flag baked into the Podman image and gated by `WOOW_CHATGPT_ENABLED` | PaaS `woow_chatgpt.py`, `woow_chatgpt_plugin.py`; HA subscription policy adapted for Podman |
| Admin authentication | Independent master key and UI password, default username `admin`; session revocation and HttpOnly cookie hardening | PaaS `woow_revoke_ui_sessions.py`, `woow_ui_cookie_hardening.py` |
| Spend and API behaviour | 90-day retention, no prompts/responses in spend logs, SSE pings, no server retries, 90-second upstream timeout, 32 MiB request cap | PaaS `values.yaml`, `templates/configmap.yaml` |
| Database | PostgreSQL 17.11 with one persistent volume; salt key copied into DB and checked on startup | PaaS `templates/scripts-configmap.yaml`, `postgresql.image` |
| Backups | Daily host dump at 19:00 UTC, keep 7; upgrade takes a pre-migration dump | PaaS `backup` defaults, adapted for systemd |

The PaaS image tag `20261024.0943` includes a separate `paas-odoo-ci` vendor bundle. This package uses the reviewed HA patch chain plus the PaaS hash-locked multidict repair on the same pinned upstream base. The nine shared plugin files are identical; this does **not** establish binary equivalence to that PaaS vendor image. The PaaS PostgreSQL image also contains pgvector, whereas this package uses the digest-pinned official PostgreSQL 17.11 image. No pgvector-dependent LiteLLM feature is configured here.

PaaS-specific tenant provisioning, Kubernetes NetworkPolicy, public Cloudflare ingress, HA Supervisor integration, and the separate `mcp-litellm` management template are outside the single-host Podman package. MCP connections can be set up through LiteLLM, but this package does not deploy an MCP server.

## PostgreSQL 16 to 17

An existing PG16 data directory cannot be opened by PG17. `scripts/install.sh` checks `PG_VERSION` and stops before changing units or secrets. For a migration:

1. On the old installation, run `scripts/backup.sh`. Verify the dump's `.sha256` and securely copy the whole backup directory, especially `secrets.env`, off the host.
2. On a fresh host or a separate Podman user with no `litellm-pgdata` volume, install this version. This creates a PG17 database and new temporary secrets.
3. Run `scripts/restore.sh <copied-backup-directory> --with-secrets --yes`. It replaces the temporary database and master/salt secrets with the old backup values, then starts the proxy and runs `tests/smoke.sh`.
4. Check `/health/readiness`, `/v1/models`, `/ui`, `/woow/chatgpt`, and the timer. Update the client endpoint only after acceptance. Keep the original PG16 volume until the new instance is verified.

Restoring a dump from PG16 to PG17 can expose SQL extension or schema incompatibilities. The restore script exits on errors and leaves a failed target for inspection; do not discard the original volume or backup. No in-place destructive migration is automated.
