# WOOW LiteLLM for rootless Podman

[繁體中文](README_zh-TW.md)

This package runs LiteLLM **1.104.0** and PostgreSQL **17.11** as two rootless Quadlet services. The proxy image is built from the [pinned upstream image](image/Dockerfile), the reviewed HA compatibility patches, and the Woow plugin copied from PaaS chart **0.3.16**. See [alignment and provenance](docs/alignment.md).

The Admin UI at `/ui` owns models, providers, virtual keys, budgets and teams. The shipped `config.yaml` has an empty model list. ChatGPT subscription sign-in at `/woow/chatgpt` supports multiple accounts and stores their tokens in PostgreSQL; runtime token files live on tmpfs. No provider credential is required to install.

## Fresh install

Prerequisites: Podman 4.9 or newer, rootless user systemd, linger, approximately 4 GiB RAM and outbound access to GHCR and Docker Hub.

```bash
git clone https://github.com/WOOWTECH/Woow_podman_litellm.git
cd Woow_podman_litellm
scripts/install.sh --dry-run
scripts/install.sh
tests/smoke.sh
```

The installer builds `localhost/woow-litellm:1.104.0-0.3.16` locally, pulls the digest-pinned PostgreSQL image, creates the secrets, and starts the services. On the first boot it prepares the Woow database tables and salt copy, runs Prisma migrations, revokes old dashboard sessions when credentials changed, restores ChatGPT account files from the database, then opens port 4000. A failed preparation or migration keeps the proxy unavailable.

Default binding is `127.0.0.1:4000`. Change it through `~/.config/litellm/litellm.env` or `scripts/install.sh --bind <IP> --port <PORT>`. The local URL is `http://127.0.0.1:4000/ui`; its bootstrap user defaults to `admin` and its independent password is stored in the Podman secret `litellm-ui-password`. The API master key is a separate secret. Do not post credentials in tickets or chats. A public endpoint needs a separate TLS reverse proxy and access policy.

## Features

| Area | Podman implementation |
| --- | --- |
| LiteLLM and patches | 1.104.0 digest-pinned base plus the HA 0.1.15 reviewed compatibility and security patch chain |
| Woow plugin | PaaS 0.3.16 shared nine files, byte-checked against the lock; extra session revocation module |
| ChatGPT subscriptions | Browser and device login, multi-account routing, DB-backed account tokens and tmpfs runtime files |
| API and UI | `/v1`, `/ui`, `/woow/chatgpt`, `/docs`; admin authentication and HttpOnly UI cookie hardening |
| Usage and error handling | 90-day spend log retention, no prompt/response spend logging, SSE keepalive, bounded request size, timeout and retry hints, budget and usage limit handling |
| Database | PostgreSQL 17.11, one named volume for data; a systemd timer runs `scripts/backup.sh --daily` at 19:00 UTC and keeps seven daily dumps |

The PaaS operator, tenant plans, Kubernetes NetworkPolicy, Cloudflare ingress, and its separate management MCP service are platform components. This single-host package exposes no ingress or MCP service automatically. The LiteLLM Admin UI can configure model providers and MCP connections supported by LiteLLM.

## Upgrade and data

`scripts/upgrade.sh` creates a `pg_dump` before changing the proxy. A pre-existing PostgreSQL **16** volume is deliberately rejected by `scripts/install.sh`: PostgreSQL 17 cannot read a PostgreSQL 16 data directory. Export that database with the old package and restore it into a new PostgreSQL 17 volume, preserving the original `LITELLM_SALT_KEY`. Do not remove the original volume until an independently stored dump has been checked. See [migration instructions](docs/alignment.md#postgresql-16-to-17).

```bash
scripts/backup.sh
scripts/restore.sh <backup-dir> --with-secrets
scripts/rotate-secrets.sh --master
scripts/rotate-secrets.sh --ui
systemctl --user status litellm litellm-postgres
```

`scripts/backup.sh` stores a database dump and the salt key under a private host directory; copy that backup off the host. `scripts/restore.sh` drops and recreates the target database, so use it only for an intended restore. The salt key must stay with the database or encrypted provider credentials become unreadable.

## Verification status

The Dockerfile patch chain and Quadlet rendering have been checked locally. The release should only be called deployed after `scripts/install.sh` and `tests/smoke.sh` pass on its intended host. No paid model request is made by the smoke test.
