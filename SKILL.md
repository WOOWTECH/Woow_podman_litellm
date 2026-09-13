---
name: woow-podman-litellm
description: Deploy, verify and operate the WOOWTECH LiteLLM gateway (LiteLLM + PostgreSQL 16) on a single rootless Podman host with Quadlet units under systemd --user. Use when installing it, changing its settings or models, upgrading, backing up or restoring it, rotating its credentials, or diagnosing an unhealthy gateway.
---

# Woow_podman_litellm: deployment runbook

A short, exact runbook. The reasoning lives in [`README.md`](README.md),
[`DEPLOYMENT.md`](DEPLOYMENT.md), [`docs/architecture.md`](docs/architecture.md) and
[`docs/k3s-to-podman.md`](docs/k3s-to-podman.md).

## 1. What the stack is

Two containers on one rootless Podman host, supervised by `systemd --user` through Quadlet:

- `litellm` — `ghcr.io/berriai/litellm:v1.83.14-stable`, the OpenAI-compatible proxy, published on
  `LITELLM_BIND:LITELLM_PORT` (default `127.0.0.1:4000`), Admin UI at `/ui`.
- `litellm-postgres` — `docker.io/library/postgres:16.15-alpine3.24`, never published, volume
  `litellm-pgdata`.

Network `litellm-net`. Five podman secrets. One read-only config file mounted from
`~/.config/litellm/config.yaml`. **The stack has not yet run on a live host**: treat any deployment
as unverified until `tests/smoke.sh` passes there.

## 2. Install

```bash
git clone https://github.com/WOOWTECH/Woow_podman_litellm.git && cd Woow_podman_litellm
scripts/install.sh                                  # creates ~/.config/litellm/litellm.env, then stops
${EDITOR:-vi} ~/.config/litellm/litellm.env         # set OPENROUTER_API_KEY there, never on a command line
scripts/install.sh --port 4000
tests/smoke.sh
```

Flags: `--port N`, `--bind ADDR`, `--set KEY=VALUE`, `--no-pull`, `--no-start`, `--dry-run`,
`--yes`. `--dry-run` writes nothing and is safe to run repeatedly.

The installer does the preflight, the guards, the render, the podman 4.9.3 generator dry-run, the
image pull, the secrets, the change-aware install and the health waits. Do not install unit files
by hand and do not edit the installed copies: the next run overwrites them (after backing up the
change).

## 3. Secrets: the rules

| Secret | Rule |
|---|---|
| `litellm-postgres-password` | Random, file-mounted (`POSTGRES_PASSWORD_FILE`). Rotate with `scripts/rotate-secrets.sh --db`. |
| `litellm-database-url` | Derived on every install; never edit by hand. |
| `litellm-master-key` | The `/v1` admin credential and the Admin UI password. Rotate with `--master`; every client must be updated. |
| `litellm-salt-key` | **Set once, never rotated.** It encrypts stored provider credentials; replacing it makes them permanently unreadable, silently. `install.sh` refuses a different value, `rotate-secrets.sh --salt` refuses outright. |
| `litellm-openrouter-api-key` | From `OPENROUTER_API_KEY` in the env file; a new value there replaces the secret and restarts the proxy. |

Read a value: `podman secret inspect --showsecret --format '{{.SecretData}}' <name>`. Never echo one
into a log, a ticket or a chat.

## 4. Verify

```bash
tests/smoke.sh                     # the whole check list; exit 0 only when everything passes
podman inspect --format '{{.State.Health.Status}}' litellm litellm-postgres
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4000/health/readiness
journalctl --user -u litellm.service -u litellm-postgres.service -o short-precise | tail -40
```

Never probe plain `/health` (it calls every model upstream and costs money on every poll). The
LiteLLM image has Python but no curl: in-container checks are `python -c` one-liners.

## 5. Change something

| Change | How |
|---|---|
| Port, bind address, log level | edit `~/.config/litellm/litellm.env`, then `scripts/install.sh` |
| Model list | edit `config/config.yaml`, then `scripts/install.sh` (CI pins its hash: mirror the change into `Woow_k3s_litellm`) |
| Image version | edit `Image=` in `quadlet/*.container`, then `scripts/upgrade.sh` |
| Resource limits | edit `PodmanArgs=--memory/--cpus`, then `scripts/install.sh` |

## 6. Upgrade, back up, restore, rotate

```bash
git pull && scripts/upgrade.sh          # snapshot + pg_dump + install + smoke, rollback on failure
scripts/backup.sh                       # ~/backups/litellm/<ts>/: dump, secrets.env, salt fingerprint
scripts/restore.sh <dir> [--with-secrets] [--yes]     # DROPs and recreates the database
scripts/rotate-secrets.sh --db | --master
```

Copy every backup off the host: without `LITELLM_SALT_KEY` the dump's provider credentials are
ciphertext forever.

## 7. Failure modes and fixes

| Symptom | Fix |
|---|---|
| `Unit litellm.service not found` | The generator skipped a file (an unsupported key) or the units are not installed. `tests/dryrun.sh`, then `scripts/install.sh`. |
| `invalid port format` | Someone put a variable in `PublishPort=` of an installed unit. Re-run `scripts/install.sh`. |
| Proxy unhealthy, "relation does not exist" | `DISABLE_SCHEMA_UPDATE` must stay `false`. |
| Proxy unhealthy, DB fine | Read `journalctl --user -u litellm.service`; a wrong `DATABASE_URL` secret shows as an auth failure. Re-run `scripts/install.sh` (it re-derives it). |
| `pg_isready` gate times out (240 s) | Read `podman logs litellm-postgres`: usually a half-initialised volume or a `PGDATA` ownership problem. |
| Models stop working, log mentions the master/salt key | The salt key changed. Restore it from a backup's `secrets.env`. |
| Nothing starts after a reboot | `loginctl show-user "$USER" --property=Linger` must be `yes`. |
| Install aborts on a container collision | An unmanaged container of that name exists. Follow the printed `podman rename` command; it is kept for rollback. |

## 8. MUST NOT

- **MUST NOT** claim the deployment works without a clean `tests/smoke.sh` run on that host.
- **MUST NOT** rotate or "regenerate" `LITELLM_SALT_KEY`.
- **MUST NOT** put a key on a command line, in a unit file, in a commit or in a log.
- **MUST NOT** reuse the k3s Cloudflare tunnel token, or add a cloudflared container here.
- **MUST NOT** publish the gateway on `0.0.0.0` without a proxy or firewall in front of it, and
  **MUST NOT** publish Postgres at all.
- **MUST NOT** run `scripts/uninstall.sh --purge` (the only destructive command) without a current
  backup: it deletes the database volume and the secrets.
- **MUST NOT** edit installed unit files, or `scripts/lib/quadlet-lib.sh` (vendored; CI checks its
  sha256).

## 9. Teardown

```bash
scripts/uninstall.sh                                   # keeps the database, secrets and images
scripts/uninstall.sh --purge --yes --purge-images      # destructive; takes a final backup first
rm -rf ~/.config/litellm                               # the env file is never removed by the scripts
```

## 10. Reference

| Thing | Value |
|---|---|
| Units | `litellm.service`, `litellm-postgres.service`, `litellm-network.service`, `litellm-pgdata-volume.service` |
| Quadlet dir | `~/.config/containers/systemd/` |
| Config dir | `~/.config/litellm/` (`litellm.env` 0600, `config.yaml` 0644) |
| State | `~/.local/state/woow-quadlet/litellm/` (manifest, pending-restart, secrets, rollback snapshots) |
| Backups | `~/backups/litellm/<timestamp>/` |
| Tests | `tests/dryrun.sh` (static, no containers), `tests/smoke.sh` (against a running stack) |
| CI | `.github/workflows/quadlet-ci.yml` (vendored), `.github/workflows/repo-checks.yml` |
