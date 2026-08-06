---
name: woow-podman-litellm
description: Deploy the WOOWTECH LiteLLM gateway (LiteLLM proxy + PostgreSQL 16) on a single Linux host with Podman, via either podman-compose or rootless Podman Quadlet systemd units. Use when asked to install, verify, back up, restore, or tear down this stack.
---

# Woow_podman_litellm - Deployment Skill

Operational runbook. Imperative. Execute in order. Do not improvise substitutions.

## 1. What the stack is

Two containers on one user-defined bridge network:

| Component | Image | Role |
|---|---|---|
| `litellm` | `ghcr.io/berriai/litellm:v1.83.14-stable` | OpenAI-compatible gateway, port 4000, Admin UI at `/ui` |
| `litellm-postgres` | `docker.io/library/postgres:16-alpine` | Key/team/spend/model state, port 5432, never published |

Models are proxied to OpenRouter. `config/config.yaml` declares five: `gpt-4o-mini`, `glm-4.6`, `minimax-m2`, `claude-sonnet-4.5`, `llama-3.3-70b`. Every entry reads `os.environ/OPENROUTER_API_KEY` and posts to `https://openrouter.ai/api/v1`. `store_model_in_db: true`, so more models can be added later through the Admin UI; YAML models and DB models are combined, and a YAML model cannot be deleted from the UI.

This repository ships **no ingress**. No cloudflared, no reverse proxy, no TLS. Exposure is the operator's problem and is documented, not automated.

The same stack runs in production on k3s. That deployment is unrelated to this one and must not be touched. See `docs/k3s-to-podman.md` for the full construct mapping.

## 2. Architecture

```
                      HOST
  +--------------------------------------------------------------+
  |                                                              |
  |  client ---> 127.0.0.1:4000        (Quadlet path: loopback)  |
  |  client --->   0.0.0.0:4000        (compose path: all ifaces)|
  |                    |                                         |
  |         +----------v-----------------------------+           |
  |         |  bridge network (netavark + aardvark)  |           |
  |         |  compose : litellm-network             |           |
  |         |  Quadlet : litellm-net                 |           |
  |         |                                        |           |
  |         |  +----------------+                    |           |
  |         |  | litellm        |  :4000             |           |
  |         |  | /app/config.yaml (ro)               |           |
  |         |  | env: OPENROUTER_API_KEY,            |           |
  |         |  |      LITELLM_MASTER_KEY,            |           |
  |         |  |      LITELLM_SALT_KEY, DATABASE_URL |           |
  |         |  +-------+--------+                    |           |
  |         |          | DNS name: litellm-postgres  |           |
  |         |          v                             |           |
  |         |  +----------------+                    |           |
  |         |  | litellm-postgres|  :5432 (internal) |           |
  |         |  | PGDATA=/var/lib/postgresql/data/pgdata          |
  |         |  +-------+--------+                    |           |
  |         +----------|-----------------------------+           |
  |                    v                                         |
  |            named volume                                      |
  |            compose : pgdata                                  |
  |            Quadlet : litellm-pgdata                          |
  +--------------------------------------------------------------+
                       |
                       v
              https://openrouter.ai/api/v1   (egress, only outbound path)
```

Startup order is health-gated in both paths: postgres becomes healthy, then litellm starts, runs Prisma migrations, then answers.

## 3. Choose the path

Pick **one**. Never run both against the same host - they bind the same port.

| Condition | Path |
|---|---|
| Portainer manages the host | compose |
| Development or throwaway box | compose |
| Consistency with sibling WOOWTECH compose repos | compose |
| Must survive reboot unattended | **Quadlet** |
| Production / left-running host | **Quadlet** |
| systemd must own and restart the workload | **Quadlet** |
| Rootless with boot-time start | **Quadlet** |
| Podman < 4.4, or no systemd user session | compose (Quadlet is unavailable) |

Default to **Quadlet** when the user says "server", "production", or "always on". Default to **compose** when the user says "try it", "Portainer", or "dev".

Differences that matter:

| | compose | Quadlet |
|---|---|---|
| Network | `litellm-network` | `litellm-net` |
| Volume | `pgdata` | `litellm-pgdata` |
| Port bind | `${LITELLM_PORT:-4000}:4000` on all interfaces | `127.0.0.1:4000:4000` loopback only |
| Env file | `<repo>/.env` | `~/.config/litellm/litellm.env` |
| Config source | `./config/config.yaml` bind mount | `~/.config/litellm/config.yaml` copy |
| Restart | compose restart policy | `Restart=always`, `RestartSec=10` |
| Boot | manual / Portainer | `[Install] WantedBy=` + linger |

## 4. Secrets - generate before anything else

Three secrets and one password. Generate them on the target host. Never reuse values across hosts. Never print them into logs, commit messages, or chat.

```bash
echo "sk-$(openssl rand -hex 32)"   # -> LITELLM_MASTER_KEY
echo "sk-$(openssl rand -hex 32)"   # -> LITELLM_SALT_KEY   (generate SEPARATELY)
openssl rand -hex 24                # -> POSTGRES_PASSWORD  (hex only: no $ # ' " )
```

`OPENROUTER_API_KEY` comes from the operator. It starts `sk-or-`. Do not fabricate one; if it is missing, stop and ask.

`DATABASE_URL` must embed the same password and must use the container DNS name:

```
DATABASE_URL=postgresql://litellm:<POSTGRES_PASSWORD>@litellm-postgres:5432/litellm
```

Required keys, both paths: `OPENROUTER_API_KEY`, `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `DATABASE_URL`. Keep `DISABLE_SCHEMA_UPDATE=false`.

**Env-file syntax rules for the Quadlet path** (`~/.config/litellm/litellm.env` is read by systemd, not by a shell):

- Plain `KEY=VALUE` only.
- No `${VAR}`, no `${VAR:-default}`, no `${VAR:?msg}`, no `$(...)`.
- No `export ` prefix.
- No trailing comments on value lines.

The compose `.env` may use shell-style defaults; `docker-compose.yml` relies on `${VAR:?}` hard-fail guards for the five critical keys.

## 5A. Compose path - exact commands

```bash
cd /path/to/Woow_podman_litellm

cp .env.example .env
chmod 600 .env
# edit .env: replace every sk-or-REPLACE_ME / sk-REPLACE_ME / CHANGE_ME_TO_SECURE_PASSWORD
# with the generated values from section 4, and fix DATABASE_URL to match.

export COMPOSE_PROJECT_NAME=litellm      # CLI only; Portainer derives it from the stack name

podman-compose config >/dev/null         # must render with zero errors and zero empty required vars.
                                         # KEEP THE REDIRECT: `config` substitutes every variable, so
                                         # unredirected stdout contains OPENROUTER_API_KEY /
                                         # LITELLM_MASTER_KEY / LITELLM_SALT_KEY / POSTGRES_PASSWORD /
                                         # DATABASE_URL in cleartext. The `${VAR:?}` guard messages you
                                         # actually need go to stderr and still appear.
podman-compose up -d
podman-compose ps
```

Then go to section 6.

To stop without data loss: `podman-compose down`. The `pgdata` volume survives.

## 5B. Quadlet path - exact commands

Preferred: use the installer. It preflights Podman version and cgroup v2, validates the env file, installs units, runs the generator dry-run, pre-pulls images, enables linger, and polls health.

```bash
cd /path/to/Woow_podman_litellm
chmod +x quadlet/*.sh scripts/*.sh   # no-op after a git clone; needed after a zip download

cp .env.quadlet.example .env
chmod 600 .env
# edit .env per section 4, obeying the systemd env-file syntax rules

./quadlet/install.sh --dry-run     # inspect the plan first, ALWAYS
./quadlet/install.sh               # or: --env-file /path/to/env  --no-pull  --no-linger
```

Exit 0 = healthy. Exit 1 = partial; read the debug block it prints and go to section 7.

Manual equivalent, in this exact order, if the installer cannot be used:

```bash
mkdir -p ~/.config/containers/systemd ~/.config/systemd/user ~/.config/litellm
chmod 700 ~/.config/litellm

install -m 0644 config/config.yaml ~/.config/litellm/config.yaml
install -m 0600 .env               ~/.config/litellm/litellm.env

install -m 0644 quadlet/litellm.network            ~/.config/containers/systemd/
install -m 0644 quadlet/litellm-pgdata.volume      ~/.config/containers/systemd/
install -m 0644 quadlet/litellm-postgres.container ~/.config/containers/systemd/
install -m 0644 quadlet/litellm.container          ~/.config/containers/systemd/
install -m 0644 quadlet/litellm-wait-postgres.service ~/.config/systemd/user/

# MANDATORY validation - Quadlet silently skips a file containing any unknown key
QUADLET_UNIT_DIRS=~/.config/containers/systemd \
  /usr/lib/systemd/system-generators/podman-system-generator --user --dryrun

podman pull docker.io/library/postgres:16-alpine
podman pull ghcr.io/berriai/litellm:v1.83.14-stable

loginctl enable-linger "$USER"     # without this the stack dies at logout and never starts at boot
systemctl --user daemon-reload

systemctl --user start litellm-network.service
systemctl --user start litellm-pgdata-volume.service
systemctl --user start litellm-postgres.service
systemctl --user start litellm-wait-postgres.service
systemctl --user start litellm.service
```

Unit names are derived from the **filename**, not from `ContainerName=`/`NetworkName=`/`VolumeName=`:

| File | Generated unit | Podman resource |
|---|---|---|
| `litellm.container` | `litellm.service` | container `litellm` |
| `litellm-postgres.container` | `litellm-postgres.service` | container `litellm-postgres` |
| `litellm.network` | `litellm-network.service` | network `litellm-net` |
| `litellm-pgdata.volume` | `litellm-pgdata-volume.service` | volume `litellm-pgdata` |
| `litellm-wait-postgres.service` | itself (plain unit, not Quadlet) | throwaway probe container |

If the generator dry-run prints different names, the dry-run wins.

Never run `systemctl --user enable litellm.service` - generated units are transient and cannot be enabled. Boot start comes from `[Install] WantedBy=` plus linger.

Config is read once at process start. After editing `~/.config/litellm/config.yaml`, run `systemctl --user restart litellm.service`.

## 6. Verify

Run the smoke test. It is the authoritative check.

```bash
chmod +x scripts/smoke-test.sh   # already 755 in the repo
scripts/smoke-test.sh            # --mode compose|quadlet|auto, --env-file PATH
```

It performs 7 checks and prints `RESULT: n/7 checks passed.` Exit 0 = all pass, 1 = failures, 2 = usage/prereq error. The checks: both containers running; postgres healthcheck healthy; litellm healthcheck healthy; `GET /health/liveliness` 200; `GET /health/readiness` 200 with a `db` field; `GET /v1/models` with the master key returns every `model_name` from `config/config.yaml`; and a count of `LiteLLM%` tables greater than zero.

Anything less than 7/7 is a failed deployment. Do not report success.

Manual spot checks:

```bash
podman ps --format '{{.Names}}\t{{.Status}}'
podman inspect --format '{{.State.Health.Status}}' litellm
podman inspect --format '{{.State.Health.Status}}' litellm-postgres
curl -s http://127.0.0.1:4000/health/liveliness
curl -s http://127.0.0.1:4000/health/readiness
journalctl --user -u litellm.service -n 200 --no-pager      # Quadlet
podman logs --tail 200 litellm                              # both
```

Endpoint rules:

- `/health/liveliness` - unauthenticated, process only, no DB. Use for liveness. (`/health/liveness` is an alias.)
- `/health/readiness` - unauthenticated, DB-aware, 503 when the DB is down. Use for readiness.
- **`/health` - never probe it.** It requires a key and fires a real request at every configured model, spending OpenRouter credits.

Admin UI: `http://127.0.0.1:4000/ui`, user `admin`, password = `LITELLM_MASTER_KEY`.

Backup and restore:

```bash
scripts/backup.sh --out /path/to/backups
# -> /path/to/backups/litellm-backup-YYYYmmdd-HHMMSS.tar.gz (0600)
#    contains MANIFEST.txt, database.dump (pg_dump custom format), config.yaml
#    deliberately EXCLUDES .env / litellm.env - secrets are your responsibility

scripts/restore.sh /path/to/backups/litellm-backup-...tar.gz
# refuses if the archive's salt-key fingerprint does not match the running one;
# requires typing RESTORE; drops and recreates the database. Exit 3 = operator aborted.
```

## 7. Failure modes and fixes

**`Unit litellm-postgres.service not found` after `daemon-reload`.**
Quadlet skipped the file because of an unrecognised key - it does not report a parse error. Run the generator dry-run from section 5B and read stderr. Most common cause: `Notify=healthy` on Podman 4.x, which only accepts true/false. Fix: comment out `Notify=healthy` in `litellm-postgres.container` and rely on `litellm-wait-postgres.service` for ordering, or upgrade to Podman 5.0+.

**`systemctl --user start` times out at 90 seconds.**
The image is being pulled inside the unit start. The shipped units already set `TimeoutStartSec=300` (postgres) / `600` (litellm). Verify the units were installed unmodified, and pre-pull both images before starting.

**Stack disappears at logout, or does not come back after reboot.**
Linger is off. `loginctl enable-linger "$USER"`, then verify with `loginctl show-user "$USER" --property=Linger`.

**`litellm` cannot resolve `litellm-postgres`.**
The container is on Podman's default `podman` network, which has no DNS. It must be on the user-defined bridge. Check with `podman exec litellm getent hosts litellm-postgres`, then `podman inspect --format '{{json .NetworkSettings.Networks}}' litellm`.

**Smoke test check 7 fails: zero `LiteLLM%` tables.**
`DISABLE_SCHEMA_UPDATE=true` on an empty volume - LiteLLM printed the migration SQL instead of applying it. Set `DISABLE_SCHEMA_UPDATE=false` in the env file, restart litellm, re-run the smoke test.

**401/403 on `/v1/models`.**
The `LITELLM_MASTER_KEY` in the env file is not the one the running proxy loaded. The proxy reads env at start. Restart it after any env change; on compose, `podman-compose up -d --force-recreate litellm`.

**Provider calls fail with decryption errors, or logs say `Did your master_key/salt key change recently?`.**
`LITELLM_SALT_KEY` changed. This is non-blocking and therefore silent - LiteLLM returns `None` for the credential instead of erroring. Restore the original salt key. If it is unrecoverable, every credential stored in the DB must be re-entered.

**Healthcheck always fails with "curl: not found".**
The LiteLLM image has no curl. Use the shipped `python -c "import urllib.request,sys; ..."` probes. Do not rewrite them to curl.

**Rootless: postgres data directory appears as `nobody`, initdb fails.**
A host bind mount was used instead of the named volume. Use the named volume as shipped. `PGDATA` must stay `/var/lib/postgresql/data/pgdata` - a subdirectory - because the postgres entrypoint refuses to initdb into a non-empty directory.

**Rootless: `--cpus` has no effect, `podman stats` shows no CPU limit.**
The `cpu`/`cpuset` controllers are not delegated. Create `/etc/systemd/system/user@.service.d/delegate.conf` with `[Service]` and `Delegate=memory pids cpu cpuset`, then log out and back in.

**Quadlet env file fails to load, or values contain literal `${...}`.**
systemd env files are not shell. Remove every `export`, `${VAR}`, `$(...)` and trailing comment. Regenerate any password containing `$`, `#`, or quotes as plain hex.

**compose: litellm starts before postgres is ready.**
`depends_on: condition: service_healthy` needs Podman >= 4.6 and podman-compose >= 1.3. Upgrade, or start postgres first, wait for healthy, then start litellm.

**`podman-compose config >/dev/null` errors on a required variable.**
A `${VAR:?}` guard fired (guard messages go to stderr, so the redirect does not hide them). That variable is missing or still a placeholder in `.env`. Fill it. Do not delete the guard.

## 8. MUST NOT

- **MUST NOT commit `.env`, `.env.quadlet`, `litellm.env`, or any file containing a real key.** `.gitignore` already excludes them; do not add exceptions, do not `git add -f`.
- **MUST NOT write a real API key, password, token, or master key into any tracked file.** Placeholders only: `sk-or-REPLACE_ME`, `sk-REPLACE_ME`, `CHANGE_ME_TO_SECURE_PASSWORD`, `os.environ/...`.
- **MUST NOT rotate, regenerate, or "refresh" `LITELLM_SALT_KEY`.** It encrypts every provider credential in the database. Set it once, at first install, and never again. Rotation makes every stored credential permanently undecryptable, and it fails silently - nothing crashes, credentials just stop working. Reinstalling against an existing `pgdata` volume MUST reuse the original value.
- **MUST NOT reuse the k3s Cloudflare tunnel token, or ship any cloudflared container/service in this stack.** That token belongs to the existing k3s deployment; a second connector on the same token splits traffic and can break the live service. This repo deliberately has no ingress. External access is documentation only.
- **MUST NOT publish the postgres port.** No `ports:` on the postgres service, no `PublishPort=` in `litellm-postgres.container`. Reach the database with `podman exec -it litellm-postgres psql -U litellm -d litellm`.
- **MUST NOT delete the pgdata volume.** Never `podman volume rm litellm-pgdata` / `pgdata`, never `podman-compose down -v`, never `./quadlet/uninstall.sh --purge-data`, unless the operator has explicitly asked for destruction and a fresh `scripts/backup.sh` archive exists. It holds every virtual key, team, user, and spend record.
- **MUST NOT set `DISABLE_SCHEMA_UPDATE=true`** in this stack. The k3s deployment does; this one must not, or the schema is never created.
- **MUST NOT prefix `Exec=` / `command:` with the word `litellm`, and must not set `entrypoint:` / `Entrypoint=`.** The image entrypoint already ends in `exec litellm "$@"`; these fields supply arguments only.
- **MUST NOT run `systemctl --user enable` on a Quadlet-generated unit.**
- **MUST NOT edit generated units under `/run/user/*/systemd/generator/`.** Edit the `.container`/`.network`/`.volume` source, then `systemctl --user daemon-reload`.
- **MUST NOT probe `/health`.** It costs money on every call.
- **MUST NOT run the compose path and the Quadlet path simultaneously** on one host.
- **MUST NOT touch anything under `/root/podman-scout/litellm-gw`** - read-only reference.
- **MUST NOT claim the deployment works without a `RESULT: 7/7 checks passed.` line from `scripts/smoke-test.sh`.**

## 9. Teardown

```bash
# compose - keeps data
podman-compose down

# Quadlet - keeps data, config, images, linger
./quadlet/uninstall.sh

# destructive, only on explicit request, only after a verified backup
podman exec litellm-postgres pg_dump -U litellm -d litellm > backup.sql
./quadlet/uninstall.sh --purge-data      # requires typing DELETE-LITELLM-DATA
./quadlet/uninstall.sh --purge-config    # requires typing DELETE-LITELLM-CONFIG
```

## 10. Reference

| Need | File |
|---|---|
| Compose stack definition | `docker-compose.yml` |
| Model list and proxy settings | `config/config.yaml` |
| Compose env template | `.env.example` |
| Quadlet env template + syntax rules | `.env.quadlet.example` |
| Quadlet units | `quadlet/*.container`, `*.network`, `*.volume`, `litellm-wait-postgres.service` |
| Install / uninstall | `quadlet/install.sh`, `quadlet/uninstall.sh` |
| Verify / back up / restore | `scripts/smoke-test.sh`, `scripts/backup.sh`, `scripts/restore.sh` |
| Diagrams, component reference, security boundary | `docs/architecture.md` |
| k3s to Podman mapping, rejected approaches, unverified items | `docs/k3s-to-podman.md` |

Nothing in this repository has been executed against a live Podman host. Treat every step as unverified until the smoke test passes on the target machine.

Sister repositories: `https://github.com/WOOWTECH/Woow_litellm_docker_compose` (k3s / compose original), `https://github.com/WOOWTECH/Woow_litellm_mcp_server` (MCP admin console).
