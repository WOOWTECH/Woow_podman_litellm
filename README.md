# LiteLLM Gateway on Podman (with PostgreSQL 16)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Podman](https://img.shields.io/badge/Podman-4.9.3%20rootless-892CA0?logo=podman&logoColor=white)](https://podman.io/)
[![Quadlet](https://img.shields.io/badge/units-Quadlet%20%2B%20systemd-orange)](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)
[![LiteLLM](https://img.shields.io/badge/LiteLLM-v1.83.14--stable-00A67E)](https://github.com/BerriAI/litellm)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16.15--alpine-336791?logo=postgresql&logoColor=white)](https://www.postgresql.org/)

**English** · [繁體中文](README_zh-TW.md)

---

## Overview

An OpenAI-compatible **LiteLLM proxy** backed by **PostgreSQL 16**, on a single Linux host under
rootless **Podman**, supervised by **systemd through Quadlet**: two containers, one bridge network,
one named volume, one read-only config file, one published port. It fronts five OpenRouter-backed
models behind a single `/v1` API, issues virtual keys with budgets and model allow-lists, persists
keys, teams, users, spend and encrypted credentials in PostgreSQL, and serves an Admin UI at `/ui`.

> **Not yet executed against a live Podman host.** Everything here is validated statically (the
> podman 4.9.3 Quadlet generator, `systemd-analyze --user verify`, shellcheck) and against test
> doubles. The first live run is the toypark1234 fresh-install test in the pull request that
> introduced this layout; treat a deployment as unverified until `tests/smoke.sh` passes on it.

### Why Podman, and how it maps to the k3s deployment

The same gateway already runs on k3s; this package is a deliberate single-node translation of it.
[**`docs/k3s-to-podman.md`**](docs/k3s-to-podman.md) is that analysis: the candidate Podman
approaches, a construct-by-construct mapping of every Kubernetes object, the seven things that do
not translate, and when to stay on Kubernetes. The one-line version: *if "what happens when this
machine goes down?" must have an answer other than "the service is down until it comes back", you
need Kubernetes.*

## Features

| Feature | How it is delivered here |
|---|---|
| OpenAI-compatible gateway | LiteLLM `v1.83.14-stable`, `/v1/*` on port 4000 |
| Five models via one upstream | OpenRouter slugs declared in `config/config.yaml` |
| Virtual keys, budgets, teams, spend | PostgreSQL 16.15, `store_model_in_db: true` |
| Admin UI | `/ui`, user `admin`, password = the master key |
| Boot survival, rootless | Quadlet units + `[Install] WantedBy=default.target` + `loginctl enable-linger` |
| Health-gated startup | `litellm-postgres` only becomes *active* after its `ExecStartPost=` sees Postgres accept TCP connections; the proxy `Requires=` it |
| No plaintext credentials | five podman secrets; Postgres reads its password as a file, the proxy gets the rest as env secrets |
| One copy of the config | `config/config.yaml` is installed to `~/.config/litellm/config.yaml` and mounted read-only |
| Idempotent install | rendered from a 0600 env file; only changed files are written, only their units restart |
| Verification, backup, restore, rotation | `tests/smoke.sh`, `scripts/backup.sh`, `scripts/restore.sh` (salt-fingerprint gate), `scripts/rotate-secrets.sh` |
| No ingress by design | no cloudflared, no tunnel token: external access is documentation only |

## Architecture

```
     API clients  --(HTTP + virtual key sk-...)-->  127.0.0.1:4000 (default)
 PODMAN HOST (rootless, systemd --user) ==============================
 |  litellm.service            Requires=/After= litellm-postgres.service |
 |  +-------------------------------------------------------------+  |
 |  | BRIDGE NET litellm-net                                       |  |
 |  |  [ litellm ]  ghcr.io/berriai/litellm:v1.83.14-stable :4000  |<-- ~/.config/litellm/config.yaml (ro)
 |  |   |  postgresql://litellm@litellm-postgres:5432 (aardvark DNS)|  |
 |  |  [ litellm-postgres ]  postgres:16.15-alpine3.24  NO HOST PORT| |
 |  +---|----------------------------------------------------------+  |
 |      v named volume litellm-pgdata (PGDATA=.../pgdata)             |
 ======================|==============================================
      outbound HTTPS 443 only -> https://openrouter.ai/api/v1
```

Startup sequence, request path and the unit-name derivation are in
[**`docs/architecture.md`**](docs/architecture.md).

| | `litellm` | `litellm-postgres` |
|---|---|---|
| Image | `ghcr.io/berriai/litellm:v1.83.14-stable` | `docker.io/library/postgres:16.15-alpine3.24` |
| Published | `LITELLM_BIND:LITELLM_PORT` -> 4000 (default `127.0.0.1:4000`) | **never** |
| Secrets | `DATABASE_URL`, `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`, `OPENROUTER_API_KEY` (env secrets) | its own password only, as a file |
| Health | startup: `/health/readiness` 40 × 15 s; liveness: `/health/liveliness` 20 s × 6 | `pg_isready` over TCP, 10 s × 3, start period 60 s |
| Limits | 2 GiB, 2.0 CPU | 1 GiB, 1.0 CPU |
| Restart | `Restart=always`, `RestartSec=10` | same |

## Prerequisites

| Requirement | Value |
|---|---|
| Podman | **4.9** minimum, tested on 4.9.3 rootless (Ubuntu 24.04) |
| systemd | a real `systemd --user` session (`XDG_RUNTIME_DIR` set), linger enabled by the installer |
| cgroups / RAM / disk | v2 / 4 GiB / ~10 GiB free / 2 cores |
| Network | outbound HTTPS to `ghcr.io`, `docker.io`, `openrouter.ai` |
| Account | an OpenRouter API key (`sk-or-...`) from <https://openrouter.ai/keys> |

```bash
podman --version && podman info --format '{{.Host.CgroupsVersion}}'   # expect v2
systemctl --user is-system-running                                    # running / degraded, not offline
```

Rootless hosts do not delegate the `cpu`/`cpuset` controllers to user slices, so the units'
`--cpus` may be ignored until an administrator adds a drop-in:

```bash
sudo mkdir -p /etc/systemd/system/user@.service.d
printf '[Service]\nDelegate=memory pids cpu cpuset\n' | sudo tee /etc/systemd/system/user@.service.d/delegate.conf
sudo systemctl daemon-reload     # then log out of ALL sessions and back in
```

## Install

```bash
git clone https://github.com/WOOWTECH/Woow_podman_litellm.git
cd Woow_podman_litellm
scripts/install.sh                      # creates the env file and stops: it needs your key
${EDITOR:-vi} ~/.config/litellm/litellm.env      # set OPENROUTER_API_KEY
scripts/install.sh                      # or: scripts/install.sh --port 18400
```

`scripts/install.sh` is idempotent. It:

1. checks the host (not root, podman >= 4.9, the Quadlet generator, `systemctl --user`) and
   enables linger;
2. creates `~/.config/litellm/litellm.env` (0600) from
   [`config/litellm.env.example`](config/litellm.env.example) on the first run; `--port N`,
   `--bind ADDR` and `--set KEY=VALUE` change a setting and save it there;
3. refuses to continue when a container named `litellm` or `litellm-postgres` exists that Quadlet
   does not manage (it would be deleted by `podman run --replace`), or when the port is taken;
4. renders the units in [`quadlet/`](quadlet/) from that env file (`@@VAR@@` tokens, whitelist in
   `quadlet/render-vars`) and validates them with the podman 4.9.3 generator and
   `systemd-analyze --user verify` **before** anything is installed;
5. pre-pulls both pinned images, so a slow pull never runs inside `TimeoutStartSec`;
6. creates the five podman secrets that are missing and derives `DATABASE_URL` from the database
   password on every run;
7. installs only the files that changed and restarts only their units, then waits for Postgres and
   the proxy to be healthy and runs [`tests/smoke.sh`](tests/smoke.sh).

`scripts/install.sh --dry-run` renders, validates and reports what it would change, without
changing anything. It is deterministic: 30 consecutive runs give 30 identical results (the old
`quadlet/install.sh` failed about two runs in three on valid units, because it grepped the
generator's stdout and stderr together for `error|failed` and matched the units' own comments).

What gets installed:

| Path | What |
|---|---|
| `~/.config/containers/systemd/litellm.container`, `litellm-postgres.container`, `litellm.network`, `litellm-pgdata.volume` | the Quadlet units |
| `~/.config/litellm/litellm.env` | your per-host settings (0600) |
| `~/.config/litellm/config.yaml` | the model list, mounted read-only into the proxy |
| podman secrets `litellm-{postgres-password,database-url,master-key,salt-key,openrouter-api-key}` | the credentials |

### Settings

Edit `~/.config/litellm/litellm.env` and re-run `scripts/install.sh`.

| Key | Default | Notes |
|-----|---------|-------|
| `LITELLM_BIND` | `127.0.0.1` | Publish address. Anything else is reachable from that network; see [External access](#external-access). |
| `LITELLM_PORT` | `4000` | Host port. |
| `LITELLM_LOG` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`. |
| `OPENROUTER_API_KEY` | *(required once)* | Copied into the secret; a new value restarts the proxy. May be blanked afterwards. |
| `LITELLM_MASTER_KEY` | *(generated)* | Optional import. Must start with `sk-`. |
| `LITELLM_SALT_KEY` | *(generated)* | Optional import; **never replaced** once the secret exists. |

The keys only ever travel from this 0600 file into podman secrets. Once installed you can blank
them there; the secrets stay. Read the master key back with:

```bash
podman secret inspect --showsecret --format '{{.SecretData}}' litellm-master-key
```

### Secrets model

| Secret | Consumer | How |
|---|---|---|
| `litellm-postgres-password` | `litellm-postgres` | `type=mount` + `POSTGRES_PASSWORD_FILE`; invisible in `podman inspect` |
| `litellm-database-url` | `litellm` | `type=env DATABASE_URL`, derived from the password on every install |
| `litellm-master-key` | `litellm` | `type=env`; the `/v1` admin credential and the Admin UI password |
| `litellm-salt-key` | `litellm` | `type=env`; encrypts stored provider credentials. **Set once, never rotated** |
| `litellm-openrouter-api-key` | `litellm` | `type=env`; `config.yaml` dereferences it as `os.environ/OPENROUTER_API_KEY` |

Nothing is in git, in a unit file, in `systemctl --user cat`, in the container's create command or
in the journal. On podman 4.9.3 a `type=env` secret **is** visible in `podman inspect` of the
running container, so anyone who can use this user's podman socket can read those four values.
`LITELLM_SALT_KEY` losing means every provider credential stored in the database becomes
permanently unreadable: `scripts/backup.sh` writes it into `secrets.env`, and you keep a copy off
this host.

## First requests

```bash
KEY=$(podman secret inspect --showsecret --format '{{.SecretData}}' litellm-master-key)
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:4000/v1/models | head -c 400
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  http://127.0.0.1:4000/key/generate -d '{"models":["gpt-4o-mini"],"max_budget":5,"key_alias":"demo"}'
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  http://127.0.0.1:4000/v1/chat/completions \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"ping"}]}'
```

Issue scoped virtual keys instead of handing out the master key. The Admin UI is
`http://127.0.0.1:4000/ui` (user `admin`, password = the master key).

**Adding or changing a model**: edit `config/config.yaml`, then `scripts/install.sh`. The installed
copy is updated and the proxy restarts. Verify a new slug against the live catalogue
(`curl -s https://openrouter.ai/api/v1/models`) first: OpenRouter retires slugs, as happened with
the bare `anthropic/claude-3.5-sonnet` entry. `model_list` entries from the file and from the
database are **combined**, not replaced: a model defined in `config.yaml` cannot be deleted from
the Admin UI. `config/config.yaml` is shared byte-for-byte with the k3s deployment; CI pins its
hash, so change both together.

## Operating

```bash
systemctl --user list-units 'litellm*'
journalctl --user -u litellm.service -u litellm-postgres.service -f
podman inspect --format '{{.State.Health.Status}}' litellm litellm-postgres
curl -s http://127.0.0.1:4000/health/liveliness    # process only, no auth, no DB
curl -s http://127.0.0.1:4000/health/readiness     # DB-aware, 503 while the DB is down
tests/smoke.sh                                     # the full post-install check
systemctl --user restart litellm.service
```

> **Never probe plain `/health`.** It requires a key **and** fires a real request at every
> configured model, burning OpenRouter credit on every poll. Note also that **`curl` does not
> exist inside the LiteLLM container** (the image ships Python, not curl), which is why the
> in-container healthchecks are `python -c` one-liners.

## Upgrade

```bash
git pull          # brings a new Image= pin
scripts/upgrade.sh
```

It snapshots the installed units, takes a `pg_dump` **first** (LiteLLM's Prisma migrations are
one-way), runs `scripts/install.sh` and `tests/smoke.sh`, and on any failure puts the previous
units back, restarts on the previous images and tells you how to restore the dump.

## Backup, restore and rotation

```bash
scripts/backup.sh                          # -> ~/backups/litellm/<timestamp>/ (0700, files 0600)
scripts/restore.sh ~/backups/litellm/<timestamp>            # DROPs and recreates the database
scripts/restore.sh <dir> --with-secrets                     # onto another host: take its salt key
scripts/rotate-secrets.sh --db | --master                   # --salt is refused, with the reason
```

A backup holds the `pg_dump -Fc`, a `secrets.env` with the salt and master keys, a salt
fingerprint, and copies of `litellm.env` and `config.yaml`. **Copy it off this host**: without the
salt key the dump's provider credentials are ciphertext forever. `restore.sh` refuses a dump whose
salt fingerprint differs from this host's unless you pass `--with-secrets`.

## Uninstall

```bash
scripts/uninstall.sh                    # stop and remove the units; keep the database, secrets, images
scripts/uninstall.sh --purge            # also delete the volume, network and secrets, after a final backup
scripts/uninstall.sh --purge-images     # no-op here: the two images are upstream and are never removed
```

`--purge` is the only way these scripts delete data; it asks you to type the app name (`--yes`
skips that). The env file stays in `~/.config/litellm/`; delete it yourself.

## External access

Nothing here publishes the gateway to the internet. Pick exactly one:

- a reverse proxy in front of `127.0.0.1:4000` (nginx or Caddy, response buffering **off** so
  streaming works) that terminates TLS and adds its own auth;
- a Tailscale or WireGuard overlay, with `scripts/install.sh --bind <overlay IP>`;
- a firewalled LAN port (`--bind <LAN IP>`), accepting that every key holder on that LAN reaches
  the Admin UI.

**Never reuse the k3s Cloudflare tunnel token.** A second connector registered with the same token
becomes another endpoint of that tunnel and Cloudflare load-balances live production traffic across
both. Create a new tunnel with a new hostname and a new token instead.

## Migrating a compose deployment

Compose is gone (see [Docker and compose](#docker-and-compose)). To adopt data from a
`podman-compose` deployment of the old layout:

```bash
# the compose container has the same name as the Quadlet one
podman exec litellm-postgres pg_dump -U litellm -d litellm --format=custom --no-owner >/tmp/old.dump
podman-compose down                       # in the old checkout; its volume is kept
mkdir -p ~/old-backup && mv /tmp/old.dump ~/old-backup/litellm-$(date +%Y%m%d-%H%M%S).dump
printf 'LITELLM_SALT_KEY=%s\n' "<the salt key from the old .env>" >~/old-backup/secrets.env
chmod 600 ~/old-backup/*; scripts/install.sh; scripts/restore.sh ~/old-backup --with-secrets
```

The old containers must be stopped and renamed (or removed) first: the Quadlet units use the same
container names and `install.sh` refuses to replace containers it does not manage.

## Docker and compose

Docker Compose and Portainer are no longer part of this repo: on the fleet's podman-compose 1.0.6
the health gate is silently dropped and `restart: unless-stopped` is not recovered at boot, and the
two paths shared container names, which could empty the database. The last commit that ships
`docker-compose.yml` is tagged
[`compose-final`](https://github.com/WOOWTECH/Woow_podman_litellm/tree/compose-final) and is not
maintained. For clusters use `Woow_k3s_litellm`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `converting "x.container": invalid port format` | `PublishPort=` cannot hold `${VAR}` on 4.9.3. This repo renders real values; you edited the installed unit. Re-run `scripts/install.sh`. |
| `unsupported key 'X' in group 'Container'` | A key your podman does not know: the **whole unit is skipped**. Check `tests/dryrun.sh`. |
| `Error: secret litellm-... not found` at start | A secret was deleted. `scripts/install.sh` recreates the missing ones, except the database password (see below). |
| The volume exists but `litellm-postgres-password` does not | The database password is unknown. Recreate the secret from a copy, or: create any value, run `scripts/install.sh` (the proxy will fail to connect), then `scripts/rotate-secrets.sh --db`, which sets the role's password over the container's local socket. |
| Proxy unhealthy, log says "relation does not exist" | The schema was never created. `DISABLE_SCHEMA_UPDATE` must stay `false` here (one proxy, no migration job). |
| Models stop working, log asks "Did your master_key/salt key change recently?" | The salt key changed. Restore the old one; it is never rotatable. |
| `Job for litellm.service failed` right after a reboot | Check `loginctl show-user "$USER" --property=Linger` (expect `yes`). |

## Layout

```
quadlet/        litellm.container, litellm-postgres.container, litellm.network,
                litellm-pgdata.volume, render-vars (the @@VAR@@ whitelist)
config/         config.yaml (shared with k3s, hash-pinned in CI), litellm.env.example
scripts/        install, upgrade, uninstall, backup, restore, rotate-secrets;
                lib/quadlet-lib.sh (vendored from Woow_quadlet_migration_plan)
tests/          dryrun.sh (vendored) + dryrun.local.sh + fixtures/, smoke.sh
docs/           architecture.md, k3s-to-podman.md
DEPLOYMENT.md   the long-form guide (prerequisites, secrets, day-2, rootful appendix)
SKILL.md        the short runbook for an agent
```

## License

MIT; see [LICENSE](LICENSE).
