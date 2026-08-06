# LiteLLM Gateway on Podman (with PostgreSQL 16)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Podman](https://img.shields.io/badge/Podman-4.4%2B%20%7C%205.0%2B%20recommended-892CA0?logo=podman&logoColor=white)](https://podman.io/)
[![LiteLLM](https://img.shields.io/badge/LiteLLM-v1.83.14--stable-00A67E)](https://github.com/BerriAI/litellm)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16--alpine-336791?logo=postgresql&logoColor=white)](https://www.postgresql.org/)

[English](#overview) | [繁體中文](README_zh-TW.md)

---

## Overview

An OpenAI-compatible **LiteLLM proxy** backed by **PostgreSQL 16**, on a single Linux host under
**Podman**: two containers, one bridge network, one named volume, one read-only config file, one
published port. It fronts five OpenRouter-backed models behind a single `/v1` API, issues virtual
keys with budgets and model allow-lists, persists keys, teams, users, spend and encrypted
credentials in PostgreSQL, and serves an Admin UI at `/ui`. Pick **one** of two paths:

- **Path A — `docker-compose.yml`** driven by `podman-compose` (or Portainer). Low friction,
  matches the sibling WOOWTECH compose repos, deployable straight from a Git URL.
- **Path B — `quadlet/` systemd units**, rootless. systemd supervises the containers, they start
  at boot, startup is health-gated. This is the production path.

> **Nothing here has been executed against a live Podman host** — this is documented, not
> measured, behaviour. Treat a deployment as unverified until `scripts/smoke-test.sh` prints
> `RESULT: 7/7 checks passed.` Never run both paths on one host — they bind the same port.

### Why Podman, and how it maps to the k3s deployment

This is what the repo is really about. The same gateway already runs on k3s; this package is a
deliberate single-node translation of it — not a port, and not a copy.
[**`docs/k3s-to-podman.md`**](docs/k3s-to-podman.md) is that analysis: the three candidate Podman
approaches and why `podman kube play` is **not** shipped; a construct-by-construct mapping of every
Kubernetes object in the k3s manifests; the seven things that do not translate (rolling updates,
reconciliation, a real secret store, the three-probe model, scheduler resource semantics, dynamic
provisioning, Service objects); the deliberate divergences; and when to stay on Kubernetes. The
one-line version: *if "what happens when this machine goes down?" must have an answer other than
"the service is down until it comes back", you need Kubernetes.*

## Features

| Feature | How it is delivered here |
|---|---|
| OpenAI-compatible gateway | LiteLLM `v1.83.14-stable`, `/v1/*` on port 4000 |
| Five models via one upstream | OpenRouter slugs declared in `config/config.yaml` |
| Virtual keys, budgets, teams, spend | PostgreSQL 16, `store_model_in_db: true` |
| Admin UI | `/ui`, user `admin`, password = `LITELLM_MASTER_KEY` |
| Two deployment paths | root `docker-compose.yml` **and** rootless `quadlet/` units |
| Health-gated startup | compose `condition: service_healthy`; Quadlet `Notify=healthy` + a oneshot wait unit |
| Boot survival, rootless | systemd `[Install]` + `loginctl enable-linger`; path B installs into `~/.config/`, no root daemon |
| One copy of the config | the real `config/config.yaml` is mounted — no ConfigMap duplicate to hand-sync |
| Secrets and database kept off-host-surface | `.env` / `~/.config/litellm/litellm.env` at mode `0600` and git-ignored; no `ports:` or `PublishPort=` on Postgres |
| Verification, backup, restore | `scripts/smoke-test.sh` (7 checks, exit 0 only on 7/7), `scripts/backup.sh`, `scripts/restore.sh` with a salt-key fingerprint gate |
| No ingress by design | no cloudflared, no tunnel token — external access is documentation only |

## Architecture

```mermaid
flowchart TB
    client["API clients (OpenAI-compatible SDKs)"]
    subgraph host["Podman host"]
        pub["Published port 4000<br/>compose 0.0.0.0 / quadlet 127.0.0.1"]
        cfg["config.yaml read-only + env file mode 0600"]
        vol[("Named volume pgdata / litellm-pgdata")]
        subgraph net["Bridge network litellm-network / litellm-net"]
            proxy["litellm v1.83.14-stable :4000"]
            db["litellm-postgres 16-alpine :5432, no host port"]
        end
    end
    client -->|"HTTP + virtual key"| pub --> proxy
    cfg -.->|"/app/config.yaml ro,Z + env"| proxy
    cfg -.->|"POSTGRES_PASSWORD"| db
    proxy -->|"5432 over container-name DNS"| db --- vol
    proxy ==>|"outbound HTTPS 443"| orouter["OpenRouter openrouter.ai/api/v1"]
```

The same picture without mermaid:
```
     API clients  --(HTTP + virtual key sk-...)-->  published port 4000
 PODMAN HOST ========================================================
 |  compose: 0.0.0.0:4000        quadlet: 127.0.0.1:4000            |
 |  +---|-------------------------------------------------------+  |
 |  | BRIDGE NET   compose litellm-network / quadlet litellm-net |  |
 |  |  [ litellm ] ghcr.io/berriai/litellm:v1.83.14-stable :4000 |<-- config.yaml, ro
 |  |   |  postgresql -> litellm-postgres:5432 (aardvark DNS)    |   /app/config.yaml
 |  |  [ litellm-postgres ] postgres:16-alpine  *NO HOST PORT*   |  |
 |  +---|-------------------------------------------------------+  |
 |      v named volume pgdata (A) / litellm-pgdata (B), PGDATA=.../pgdata
 ======================|============================================
      outbound HTTPS 443 only -> https://openrouter.ai/api/v1
```

Full diagrams — startup sequence, request path, Quadlet unit-name derivation, component
reference, data flow, security boundary — are in [**`docs/architecture.md`**](docs/architecture.md).

## Service Details

| | `litellm` | `litellm-postgres` |
|---|---|---|
| Image | `ghcr.io/berriai/litellm:v1.83.14-stable` | `docker.io/library/postgres:16-alpine` |
| Role | OpenAI-compatible proxy, Admin UI at `/ui` | keys, teams, users, spend, encrypted credentials |
| Port 4000 / 5432, published | `${LITELLM_PORT:-4000}:4000` (A) / `127.0.0.1:4000:4000` (B) | **never** |
| Arguments | `--config /app/config.yaml --port 4000` | image entrypoint, unmodified |
| Config / data | `config/config.yaml` mounted `ro,Z` at `/app/config.yaml` | named volume at `/var/lib/postgresql/data`, `PGDATA=.../pgdata` |
| Health probe | `python -c` urllib probe of `/health/liveliness` (20s/10s/6, start 120s) | `pg_isready -U litellm -d litellm` (10s/5s, start 10s) |
| Startup probe | path B only: `/health/readiness`, 40 × 15s | — |
| Limits | 2 GiB memory, 2.0 CPU | 1 GiB memory, 1.0 CPU |
| Restart | `unless-stopped` (A) / `Restart=always`, `RestartSec=10` (B) | same |

Models in `config/config.yaml`, all routed through `https://openrouter.ai/api/v1`: `gpt-4o-mini`
→ `openrouter/openai/gpt-4o-mini`, `glm-4.6` → `openrouter/z-ai/glm-4.6`, `minimax-m2` →
`openrouter/minimax/minimax-m2`, `claude-sonnet-4.5` → `openrouter/anthropic/claude-sonnet-4.5`,
`llama-3.3-70b` → `openrouter/meta-llama/llama-3.3-70b-instruct`. `litellm_settings` sets
`drop_params: true` and `request_timeout: 600`; the API key, master key and database URL are
dereferenced as `os.environ/...`, so no secret is written into `config/config.yaml`.

## Deploy to Portainer

Portainer consumes **path A only** — it reads a Compose file from a repository root, and Quadlet
units are systemd configuration that cannot be pasted into a stack. *Stacks → Add stack →
Repository:*

| Field | Value |
|---|---|
| Repository URL | `https://github.com/WOOWTECH/Woow_podman_litellm` |
| Repository reference | `refs/heads/main` |
| Compose path | `docker-compose.yml` |
| Authentication | off (public repository) |

Then add the variables from [Environment Variables Reference](#environment-variables-reference)
in Portainer's own *Environment variables* editor — the repository contains no `.env`, only
`.env.example`. To paste the file instead, fetch the raw URL and use *Add stack → Web editor*:

```bash
curl -sSLO https://raw.githubusercontent.com/WOOWTECH/Woow_podman_litellm/main/docker-compose.yml
```

The web editor cannot fetch `config/config.yaml`, so place that file on the host yourself where
the bind mount expects it (`./config/config.yaml`, relative to the stack's working directory).
Portainer derives the project name from the stack name — hence no top-level `name:` key.

## Prerequisites

| Requirement | Path A (compose) | Path B (Quadlet) |
|---|---|---|
| Podman | 4.6 min — `depends_on: condition: service_healthy` needs `podman wait --condition=healthy` | 4.4 min, **5.0+ recommended** — `Notify=healthy` needs 5.0+, native `Memory=` needs 5.5+ |
| Compose provider | `podman-compose >= 1.3`, or Docker Compose v2 via `podman compose` | not needed |
| systemd | not needed | a real `systemd --user` session (`XDG_RUNTIME_DIR` set) |
| cgroups / RAM / disk | v2 / 4 GiB / ~10 GiB free / 2 cores | same |
| Network | outbound HTTPS to `ghcr.io`, `docker.io`, `openrouter.ai` | same |
| Account | OpenRouter API key (`sk-or-...`) from <https://openrouter.ai/keys> | same |

```bash
podman --version && podman info --format '{{.Host.CgroupsVersion}}'   # expect: v2
podman-compose --version            # path A
systemctl --user is-system-running  # path B
echo "sk-$(openssl rand -hex 32)"   # -> LITELLM_MASTER_KEY
echo "sk-$(openssl rand -hex 32)"   # -> LITELLM_SALT_KEY  (generate SEPARATELY)
openssl rand -hex 24                # -> POSTGRES_PASSWORD (hex avoids $ # ' " )
```

Generate those secrets on the target host; never reuse them. Rootless hosts also do not delegate
the `cpu`/`cpuset` controllers to user slices, so `--cpus` may be ignored until you add a drop-in:

```bash
sudo mkdir -p /etc/systemd/system/user@.service.d
sudo tee /etc/systemd/system/user@.service.d/delegate.conf >/dev/null <<'EOF'
[Service]
Delegate=memory pids cpu cpuset
EOF
sudo systemctl daemon-reload    # then log out of ALL sessions and log back in
```

## Quick Start

Pick one path. Full step-by-step coverage, including rootful installs, is in
[**`DEPLOYMENT.md`**](DEPLOYMENT.md).

### Path A — podman-compose (quickest)

1. **Clone and create `.env`.**
   ```bash
   git clone https://github.com/WOOWTECH/Woow_podman_litellm.git && cd Woow_podman_litellm
   cp .env.example .env && chmod 600 .env
   ```
2. **Fill in the values** with `${EDITOR:-vi} .env` — replace `sk-or-REPLACE_ME`, both
   `sk-REPLACE_ME` placeholders and `CHANGE_ME_TO_SECURE_PASSWORD`, and put the same password
   into `DATABASE_URL`.
3. **Validate first** with
   `export COMPOSE_PROJECT_NAME=litellm && podman-compose config >/dev/null`. The
   `${VAR:?message}` guards make this fail loudly rather than start a broken container.
   **Keep the `>/dev/null`.** `config` renders the merged file with every variable
   *substituted*, so unredirected stdout prints `OPENROUTER_API_KEY`,
   `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`, `POSTGRES_PASSWORD` and the
   password-bearing `DATABASE_URL` in cleartext — into your terminal scrollback, into
   any `tee`/CI log, and into anything you later paste into an issue or a chat. The
   guard messages you actually need go to stderr and still appear. If you do need to
   read the rendered file, redirect it to a `chmod 600` file and delete it afterwards.
4. **Start, then watch the first boot.** Expect `(health: starting)` for a while: image pull,
   then Prisma migrations, then the healthcheck's 120 s `start_period`.
   ```bash
   podman-compose up -d && podman-compose ps
   podman logs -f litellm
   ```
5. **Verify** with `./scripts/smoke-test.sh --mode compose` — it must print `7/7`.

### Path B — Quadlet + systemd (recommended for a machine that must survive reboots)

1. **Clone the repo.** Every `*.sh` is tracked mode `755`, so a `git clone` gives you executable
   scripts. A zip/tarball download drops the mode bit, so the `chmod` is a harmless safety net.
   ```bash
   git clone https://github.com/WOOWTECH/Woow_podman_litellm.git && cd Woow_podman_litellm
   chmod +x quadlet/*.sh scripts/*.sh
   ```
2. **Create the env file outside the git tree.** systemd parses this one, not a shell: plain
   `KEY=VALUE` only — no `${VAR}`, no `$(...)`, no `export`, no trailing comments.
   ```bash
   mkdir -p ~/.config/litellm && chmod 700 ~/.config/litellm
   cp .env.quadlet.example ~/.config/litellm/litellm.env
   chmod 600 ~/.config/litellm/litellm.env && ${EDITOR:-vi} ~/.config/litellm/litellm.env
   ```
3. **Dry-run the installer, always:** `./quadlet/install.sh --dry-run`
   (unit-file syntax only — it does not read `--env-file`; the env file is validated by step 4).
4. **Install.** Preflights Podman and cgroup v2, validates the env file, copies the units, runs
   the generator dry-run, pre-pulls images, enables linger, reloads and starts. Flags
   `--no-pull`, `--no-linger`, `-h`. Exit `0` = healthy; `1` = partial, read the debug block.
   ```bash
   ./quadlet/install.sh --env-file ~/.config/litellm/litellm.env
   ```
5. **Confirm the units.** Names derive from the *filename*, so you get `litellm.service`,
   `litellm-postgres.service`, `litellm-network.service` and `litellm-pgdata-volume.service`.
   If the generator dry-run disagrees with any name in this repo, the dry-run wins.
   ```bash
   /usr/lib/systemd/system-generators/podman-system-generator --user --dryrun
   systemctl --user list-units 'litellm*'
   loginctl show-user "$USER" --property=Linger     # expect Linger=yes
   ```
6. **Verify** with `./scripts/smoke-test.sh --mode quadlet` — it must print `7/7`.

## Post-Installation Configuration

**Admin UI:** `http://127.0.0.1:4000/ui` — user `admin`, password `LITELLM_MASTER_KEY`. Issue
scoped virtual keys rather than handing out the master key, and send one real completion to prove
the whole path works (a fraction of a cent):

```bash
H=(-H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json')
curl -sS http://127.0.0.1:4000/key/generate "${H[@]}" \
  -d '{"models":["gpt-4o-mini"],"max_budget":5,"key_alias":"demo"}'
curl -sS http://127.0.0.1:4000/v1/chat/completions "${H[@]}" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"ping"}]}'
```

**Add or change a model** by editing `config/config.yaml`, then restarting the proxy only:

```bash
${EDITOR:-vi} config/config.yaml
podman-compose restart litellm                                     # Path A — bind-mounted
install -m 0644 config/config.yaml ~/.config/litellm/config.yaml   # Path B — installed copy
systemctl --user restart litellm.service                           # Path B
```

Verify a new slug against the live catalogue (`curl -s https://openrouter.ai/api/v1/models`)
first — OpenRouter retires slugs, as happened here with the bare `anthropic/claude-3.5-sonnet`
entry. Note too that `model_list` entries from the file and from the database are **combined**,
not replaced: a model defined in `config.yaml` cannot be deleted from the Admin UI.

**External access is documentation only.** Nothing here publishes the gateway to the internet.
`DEPLOYMENT.md` §8 covers three options, and you should pick exactly one: a reverse proxy in
front of `127.0.0.1:4000` (nginx or Caddy, buffering off so streaming works), a Tailscale or
WireGuard overlay, or a firewalled LAN port.

## Useful Commands

| Task | Path A (compose) | Path B (Quadlet) |
|---|---|---|
| Status | `podman-compose ps` | `systemctl --user list-units 'litellm*'` |
| Follow proxy logs | `podman-compose logs -f litellm` | `journalctl --user -u litellm.service -f` |
| Follow database logs | `podman-compose logs -f postgres` | `journalctl --user -u litellm-postgres.service -f` |
| Restart the proxy | `podman-compose restart litellm` | `systemctl --user restart litellm.service` |
| Stop (keeps data) | `podman-compose down` | `systemctl --user stop litellm.service litellm-postgres.service` |
| Start again | `podman-compose up -d` | `systemctl --user start litellm-postgres.service litellm.service` |
| Uninstall (keeps data) | `podman-compose down`, then delete the checkout | `./quadlet/uninstall.sh` (removes the unit files; re-run `install.sh` to come back) |
| Apply a unit/file edit | `podman-compose up -d` | `systemctl --user daemon-reload` then restart |

```bash
podman inspect --format '{{.State.Health.Status}}' litellm litellm-postgres
podman exec -it litellm-postgres psql -U litellm -d litellm
curl -s http://127.0.0.1:4000/health/liveliness    # process only, no auth, no DB
curl -s http://127.0.0.1:4000/health/readiness     # DB-aware, 503 when the DB is down
./scripts/smoke-test.sh                            # --mode compose|quadlet|auto
./scripts/backup.sh --out /srv/backups             # -> litellm-backup-<ts>.tar.gz, 0600
./scripts/restore.sh /srv/backups/litellm-backup-<ts>.tar.gz
```

> **Never probe plain `/health`.** It requires a key **and** fires a real request at every
> configured model, burning OpenRouter credit on every poll — use `/health/liveliness` or
> `/health/readiness`. Also, **`curl` does not exist inside the LiteLLM container** (the image
> ships Python, not curl), which is why both in-container healthchecks are
> `python -c "import urllib.request,sys; ..."` one-liners. From the host, `curl` is fine.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `litellm` sits at `(health: starting)` for minutes on first boot | Normal — image pull, Prisma migrations, then a 120 s `start_period` | Wait, watching `podman logs -f litellm`. Suspect a fault only after the full startup budget (path B: 40 × 15 s). |
| `relation "LiteLLM_..." does not exist`, or zero `LiteLLM%` tables | `DISABLE_SCHEMA_UPDATE=true` against an empty database — LiteLLM runs a read-only `prisma migrate diff` that creates nothing | Set it back to `false` and restart the proxy. See `DEPLOYMENT.md` §5. |
| `could not translate host name "litellm-postgres"` | The container landed on Podman's default `podman` network, which has no DNS | Both paths create a user-defined bridge for exactly this reason. Check `podman network ls` and that `litellm-network.service` (path B) started. |
| `Unit litellm-postgres.service not found` | Quadlet **silently skipped** the file over an unrecognised key — most often `Notify=healthy` on Podman 4.x | Run the generator dry-run and read stderr. On 4.x comment out `Notify=healthy`; `litellm-wait-postgres.service` still gates readiness. Then `daemon-reload`. |
| `Start operation timed out`, or the stack is gone after a reboot (path B) | A cold image pull exceeded `TimeoutStartSec`; or linger is off, so the systemd user manager never starts | Pre-pull both images (`install.sh` does, unless `--no-pull`). `loginctl enable-linger "$USER"`, confirmed with `loginctl show-user "$USER" --property=Linger`. |
| `bind: address already in use` on 4000 | Something else holds the port | `ss -ltnp \| grep :4000`. Free it, or change `LITELLM_PORT` (A) / `PublishPort=` (B). |
| `401 Unauthorized` from the proxy | The running proxy loaded a different `LITELLM_MASTER_KEY` | Compare with `podman exec litellm printenv LITELLM_MASTER_KEY`. The key is read at startup — restart after any env change. |
| `permission denied` on the Postgres data directory | A rootless bind mount was used; container UID 70 does not map to a writable host UID | Use the named volume both paths ship. Never bind-mount `PGDATA`. |
| `--cpus` appears to be ignored | `cpu`/`cpuset` are not delegated to your user slice | Add the `delegate.conf` drop-in from [Prerequisites](#prerequisites), then log out of all sessions. |
| `Did your master_key/salt key change recently?` in the logs | `LITELLM_SALT_KEY` changed. Decryption failures are **non-blocking**, so nothing crashes | Restore the original salt key. See [Security Notes](#security-notes). |

A longer table and a general diagnostic sweep are in [`DEPLOYMENT.md` §10](DEPLOYMENT.md); the same failure modes in imperative form are in [`SKILL.md`](SKILL.md).

## File Structure

```
Woow_podman_litellm/
├── README.md                          # this file
├── README_zh-TW.md                    # 繁體中文版
├── DEPLOYMENT.md                      # full deployment guide, day-2 ops, uninstall
├── DEPLOYMENT_zh-TW.md                # 繁體中文部署指南
├── SKILL.md                           # imperative runbook (agent-facing skill)
├── LICENSE                            # MIT
├── .gitignore                         # blocks .env, data/, backups/, *.sql, *.tar.gz, ...
├── docker-compose.yml                 # PATH A — the whole stack, Portainer-deployable
├── .env.example                       # PATH A env template (bilingual)
├── .env.quadlet.example               # PATH B env template + systemd env-file syntax rules
├── config/
│   └── config.yaml                    # models + general/litellm settings (mounted, never copied)
├── quadlet/                           # PATH B — rootless systemd units
│   ├── litellm.network                # -> litellm-network.service,       network litellm-net
│   ├── litellm-pgdata.volume          # -> litellm-pgdata-volume.service, volume litellm-pgdata
│   ├── litellm-postgres.container     # -> litellm-postgres.service
│   ├── litellm.container              # -> litellm.service
│   ├── litellm-wait-postgres.service  # plain unit (NOT Quadlet), Podman 4.x ordering fallback
│   ├── install.sh                     # preflight, install, validate, pull, linger, start
│   └── uninstall.sh                   # non-destructive by default; --purge-* flags
├── scripts/
│   ├── smoke-test.sh                  # 7 checks; exit 0 only on 7/7
│   ├── backup.sh                      # pg_dump + config.yaml + MANIFEST.txt, mode 0600
│   └── restore.sh                     # salt-key fingerprint gate, drop/create, pg_restore
├── docs/
│   ├── architecture.md                # diagrams, component reference, security boundary
│   └── k3s-to-podman.md               # why Podman, full construct mapping, rejected options
└── .github/workflows/
    └── lint.yml                       # STATIC checks only — never starts a container
```

CI runs `bash -n` + `shellcheck`, parses both YAML files, pins the `config/config.yaml` hash to the
k3s copy, asserts the Quadlet units keep their required sections, and rejects credential-shaped
strings. **A green CI badge does not mean the stack was deployed successfully** — nothing in this
repo has been executed against a live Podman host. `scripts/smoke-test.sh` is the only thing that
can make that claim.

Every `*.sh` in the repo is tracked mode `755`; if you downloaded a zip/tarball instead of cloning,
the mode bit is lost — run `chmod +x quadlet/*.sh scripts/*.sh` to restore it. Both paths mount
the config at `/app/config.yaml` — path A from
`./config/config.yaml`, path B from the installed copy `~/.config/litellm/config.yaml`.

## Environment Variables Reference

Every variable in `.env.example`. `.env.quadlet.example` drops `LITELLM_PORT` (on path B the
published port is static text in `litellm.container`) and ships `STORE_MODEL_IN_DB`,
`LITELLM_MODE`, `LITELLM_LOG` and `DISABLE_SCHEMA_UPDATE` commented out, because on path B those
four are pinned by `Environment=` lines in the unit file, which override the env file.

| Variable | Default | Required | Description |
|---|---|---|---|
| `OPENROUTER_API_KEY` | `sk-or-REPLACE_ME` | **yes** | OpenRouter key from <https://openrouter.ai/keys>. Read by `config/config.yaml` as `os.environ/OPENROUTER_API_KEY`. |
| `LITELLM_MASTER_KEY` | `sk-REPLACE_ME` | **yes** | Admin credential **and** the Admin UI password for user `admin`. Must start with `sk-`. Generate with `echo "sk-$(openssl rand -hex 32)"`. |
| `LITELLM_SALT_KEY` | `sk-REPLACE_ME` | **yes** | Encrypts every provider credential stored in PostgreSQL. **Set once, never rotate.** Generate separately from the master key. |
| `POSTGRES_USER` | `litellm` | no | Database role. Matches the k3s deployment. If you change it, update `DATABASE_URL` and the Quadlet `HealthCmd=`. |
| `POSTGRES_PASSWORD` | `CHANGE_ME_TO_SECURE_PASSWORD` | **yes** | Database password. Generate with `openssl rand -hex 24` — hex avoids `$`, `#` and quoting problems. |
| `POSTGRES_DB` | `litellm` | no | Database name. Same caveats as `POSTGRES_USER`. |
| `DATABASE_URL` | `postgresql://litellm:CHANGE_ME_TO_SECURE_PASSWORD@litellm-postgres:5432/litellm` | **yes** | Must embed the same password and must use the host `litellm-postgres` — **not** `localhost`. Percent-encode any of `: / ? # [ ] @` in the password. |
| `LITELLM_PORT` | `4000` | no | Host port published by path A only. Path B binds `127.0.0.1:4000:4000` in the unit file. |
| `STORE_MODEL_IN_DB` | `True` | no | Persists models added from the Admin UI into PostgreSQL. Path B: pinned in `quadlet/litellm.container`; the env file value is ignored. |
| `LITELLM_MODE` | `PRODUCTION` | no | Disables LiteLLM's `load_dotenv()`, so a stray local `.env` is not auto-loaded. Path B: pinned in `quadlet/litellm.container`; the env file value is ignored. |
| `LITELLM_LOG` | `INFO` | no | `DEBUG` / `INFO` / `ERROR`. Note the off-by-one naming: `INFO` is already roughly the CLI's `--debug`. Debug logs can contain request payloads. Path B: pinned in `quadlet/litellm.container`; the env file value is ignored. |
| `DISABLE_SCHEMA_UPDATE` | `false` | no | **Leave at `false`.** The k3s deployment sets `true`; copying that here means `prisma migrate diff` prints the SQL and creates nothing against an empty volume, and the proxy never becomes healthy. Path B: pinned in `quadlet/litellm.container`; the env file value is ignored. |

The compose file guards five of these with `${VAR:?message}` — `OPENROUTER_API_KEY`,
`LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`, `DATABASE_URL`, `POSTGRES_PASSWORD` — and refuses to
start without them. Quadlet has no equivalent guard — a missing value is simply empty and the
container fails later, which is part of why `scripts/smoke-test.sh` exists.

## Security Notes

**Never commit `.env`.** `.gitignore` already blocks `.env`, `.env.*` (allowing only the
`*.example` templates), `*.env`, `secrets/`, `*.key`, `*.pem`, `data/`, `pgdata/`, `backups/`,
`*.sql`, `*.tar.gz` and friends. Do not add exceptions, do not `git add -f`. Keep the file at
mode `600`; on path B keep it out of the working tree entirely, in `~/.config/litellm/` (`700`).

> ### ⚠ `LITELLM_SALT_KEY` — set it once, never rotate it
>
> It encrypts every provider credential LiteLLM stores in PostgreSQL. **Change it and every one
> of them becomes permanently undecryptable** — no recovery short of wiping the database and
> re-entering each credential by hand. Worse, the failure is silent: decryption errors are
> non-blocking, so LiteLLM logs *"Did your master_key/salt key change recently?"*, returns `None`
> and keeps serving. Left unset, it falls back to `LITELLM_MASTER_KEY` as the salt, which makes
> rotating the master key equally destructive. Set both explicitly and separately before the
> first start, and back the salt key up off-host; `scripts/backup.sh` stores only a truncated
> SHA-256 **fingerprint**, so `scripts/restore.sh` refuses a restore into a mismatched stack.

**The Postgres port is not published, in either path** — no `ports:` on the compose service, no
`PublishPort=` in `litellm-postgres.container`. The database holds every virtual key hash, budget
row and encrypted credential, and nothing on the host needs the port; for ad-hoc access use
`podman exec -it litellm-postgres psql -U litellm -d litellm`.

**Use `podman secret` if you prefer it in production.** Both paths deliberately use a plain `0600`
env file, because Podman's default secret driver stores secrets unencrypted under the user's data
directory — relocating the problem, not solving it. Quadlet exposes secrets via `Secret=`.

**Rotating `LITELLM_MASTER_KEY`** is safe *only* while `LITELLM_SALT_KEY` is set explicitly:
update the value in `.env` / `litellm.env`, restart the proxy (`podman-compose up -d
--force-recreate litellm`, or `systemctl --user restart litellm.service`), and re-issue the Admin
UI password. Existing virtual keys survive. **The caveat:** if `LITELLM_SALT_KEY` was never set,
the master key *is* the salt and rotating it silently destroys every stored credential. Better
never to hand out the master key: issue scoped keys with `max_budget` and a `models` allow-list.

> ### ⚠ This package intentionally ships no Cloudflare tunnel
>
> There is no `cloudflared` container, no tunnel unit and no `TUNNEL_TOKEN` variable anywhere in
> this repository, and that is deliberate. A tunnel token identifies a **tunnel**, not a
> connector: starting a second `cloudflared` with the **same** token registers another origin
> against the same tunnel, and Cloudflare load-balances across both — sending an unpredictable
> share of live requests into this stack, which has a different database, different virtual keys
> and different spend records. **The token used by the existing k3s deployment is in use right
> now. Never paste it here.** If you need a tunnel, create a **new** one with its own token and
> hostname and run `cloudflared` yourself, outside this repository.

## Updating

Both images are pinned and nothing auto-updates: `AutoUpdate=registry` is deliberately unset,
because unattended upgrades of a database engine under a live data directory are exactly what you
do not want. **Take a backup first** — the newer proxy may run migrations on start.

```bash
./scripts/backup.sh --out /srv/backups
# Path A
${EDITOR:-vi} docker-compose.yml          # change the image tag
podman-compose pull && podman-compose up -d && ./scripts/smoke-test.sh --mode compose
# Path B
${EDITOR:-vi} quadlet/litellm.container   # change Image=
podman pull ghcr.io/berriai/litellm:NEW_TAG   # the tag you just set, not the old one
./quadlet/install.sh --env-file ~/.config/litellm/litellm.env   # copies, reloads, restarts
./scripts/smoke-test.sh --mode quadlet
```

Rolling back is the same procedure with the previous tag, plus a restore if migrations ran. To pin
by digest instead, both `.container` units carry a commented `Image=...@sha256:PASTE_DIGEST_HERE`
line; `podman image inspect <img> --format '{{index .RepoDigests 0}}'` prints one. None ships here,
because none was pulled or verified.

## License

[MIT](LICENSE) © 2026 WOOWTECH.

## Other deployment platforms

- **K3s / Docker Compose** — [`WOOWTECH/Woow_litellm_docker_compose`](https://github.com/WOOWTECH/Woow_litellm_docker_compose).
  The origin of this repository and the source of truth for the *application* — model list,
  environment contract, image tags. Its k3s manifests run the existing production gateway;
  nothing in this repository manages, mutates or points at that deployment.
- **MCP admin console** — [`WOOWTECH/Woow_litellm_mcp_server`](https://github.com/WOOWTECH/Woow_litellm_mcp_server).
  Manage keys, teams, users and spend on a running gateway from an MCP client.
