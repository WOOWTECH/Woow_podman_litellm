# Deployment Guide

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Podman](https://img.shields.io/badge/Podman-4.4%2B%20%7C%205.0%2B%20recommended-892CA0?logo=podman&logoColor=white)](https://podman.io/)
[![LiteLLM](https://img.shields.io/badge/LiteLLM-v1.83.14--stable-00A67E)](https://github.com/BerriAI/litellm)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16--alpine-336791?logo=postgresql&logoColor=white)](https://www.postgresql.org/)

Step-by-step instructions for deploying the WOOWTECH LiteLLM gateway on Podman.
This is the long-form companion to [`README.md`](README.md); the design rationale
behind every choice made here lives in [`docs/architecture.md`](docs/architecture.md).

Two deployment paths are supported and both are covered end to end:

| Path | What it is | Section |
|---|---|---|
| **A — `podman-compose`** | root `docker-compose.yml`, one command up/down, Portainer-friendly | [3](#3-path-a--podman-compose) |
| **B — Quadlet + `systemd --user`** | real systemd units, health-gated ordering, starts at boot | [4](#4-path-b--quadlet--systemd---user) |

Nothing in this guide has been executed against a live Podman host. Commands are
derived from the files in this repository and from documented Podman / LiteLLM /
PostgreSQL behaviour. Treat the first run on your own host as the real test, and
use [section 6](#6-verification) to confirm it.

**Contents**

1. [Prerequisites](#1-prerequisites)
2. [Preparing secrets](#2-preparing-secrets)
3. [Path A — podman-compose](#3-path-a--podman-compose)
4. [Path B — Quadlet + `systemd --user`](#4-path-b--quadlet--systemd---user)
5. [First boot vs steady state (`DISABLE_SCHEMA_UPDATE`)](#5-first-boot-vs-steady-state-disable_schema_update)
6. [Verification](#6-verification)
7. [Day-2 operations](#7-day-2-operations)
8. [External access](#8-external-access)
9. [Rootless vs rootful](#9-rootless-vs-rootful)
10. [Troubleshooting](#10-troubleshooting)
11. [Uninstall](#11-uninstall)

---

## 1. Prerequisites

### 1.1 Podman

| Requirement | Path A (compose) | Path B (Quadlet) |
|---|---|---|
| Hard minimum | Podman 4.6 | Podman 4.4 (Quadlet moved into Podman core in 4.4) |
| Recommended | Podman 5.x | **Podman 5.0+** |
| Why 5.0+ matters | newer compose provider support | `Notify=healthy` in `litellm-postgres.container` needs 5.0+ |
| Nice to have | — | Podman 5.5+ adds native `Memory=` / `CPUQuota=` Quadlet keys |

`quadlet/install.sh` enforces this: it **exits** if Podman is older than 4.4, and
**warns** below 5.0 (because of `Notify=healthy`) and below 5.5 (because the units
use `PodmanArgs=--memory=` instead of the native `Memory=` key).

```bash
# Version
podman --version

# Structured version, if you script the check
podman info --format '{{.Version.Version}}'
```

> **Podman 4.x and `Notify=healthy`.** On Podman 4.x the Quadlet `Notify=` key
> only accepts `true` / `false`. The value `healthy` is rejected by the generator,
> and a unit that fails to generate does not exist at all — `systemctl --user start
> litellm-postgres.service` then reports *"Unit litellm-postgres.service not found"*,
> which looks like a missing file rather than a parse error. If you must run on
> Podman 4.x, comment out the `Notify=healthy` line in
> `quadlet/litellm-postgres.container`; the `litellm-wait-postgres.service` oneshot
> unit already provides a readiness gate on its own.

### 1.2 cgroups v2

Both paths need cgroup v2. Resource limits (`mem_limit` / `cpus` in compose,
`PodmanArgs=--memory/--cpus` in Quadlet) are silently weaker or unavailable under
cgroup v1 rootless.

```bash
podman info --format '{{.Host.CgroupsVersion}}'
# expected: v2
```

Rootless hosts additionally do **not** delegate the `cpu` and `cpuset` controllers
to user slices by default, so `--cpus=` may be ignored. To enable delegation:

```bash
sudo mkdir -p /etc/systemd/system/user@.service.d
sudo tee /etc/systemd/system/user@.service.d/delegate.conf >/dev/null <<'EOF'
[Service]
Delegate=memory pids cpu cpuset
EOF
sudo systemctl daemon-reload
# log out of ALL sessions for this user and log back in
```

### 1.3 Compose provider (path A only)

Either of these works:

```bash
# Option 1: podman-compose >= 1.3
podman-compose --version

# Option 2: Docker Compose v2 binary driven through Podman's socket
podman compose version
```

This guide writes `podman-compose <cmd>` throughout. If you use the second option,
substitute `podman compose <cmd>` — the arguments are identical.

### 1.4 A real `systemd --user` session (path B only)

Quadlet units are generated per-user by the systemd user manager. You need a
working user bus, not just a shell:

```bash
systemctl --user is-system-running     # any answer other than "Failed to connect..." is fine
loginctl show-user "$USER" --property=Linger
echo "$XDG_RUNTIME_DIR"                # must be set, normally /run/user/<uid>
```

If `systemctl --user` cannot connect, you are probably in a bare `su` or a
container shell. Log in over SSH as the target user, or use
`machinectl shell <user>@`.

### 1.5 Host resources

| Resource | Minimum | Notes |
|---|---|---|
| RAM | 4 GiB | the two containers are capped at 2 GiB (litellm) + 1 GiB (postgres) = 3 GiB |
| Disk | ~10 GiB free | LiteLLM image is large; plus Postgres data and backups |
| CPU | 2 cores | limits are `cpus: 2.0` (litellm) and `cpus: 1.0` (postgres) |
| Network | outbound HTTPS | to `ghcr.io`, `docker.io` and `openrouter.ai` |

### 1.6 An OpenRouter account

Every model in `config/config.yaml` routes through OpenRouter
(`api_base: https://openrouter.ai/api/v1`). You need an account with credit and an
API key. Create one at <https://openrouter.ai/keys>. Keys start with `sk-or-`.

---

## 2. Preparing secrets

Four secrets are required. Generate all of them before touching either path.

> **Never commit secrets.** `.gitignore` already excludes `.env`, `.env.*`
> (except the `*.example` files), `*.env`, `secrets/`, `*.key`, `*.pem`,
> `backups/`, `*.sql` and `*.tar.gz`. Keep it that way.

### 2.1 `LITELLM_MASTER_KEY`

The admin credential for the proxy **and** the Admin UI password. Must begin with
`sk-`.

```bash
echo "sk-$(openssl rand -hex 32)"
```

### 2.2 `LITELLM_SALT_KEY`

```bash
echo "sk-$(openssl rand -hex 32)"
```

> ### ⚠ SET IT ONCE. NEVER ROTATE IT.
>
> `LITELLM_SALT_KEY` is the encryption key for every provider credential LiteLLM
> stores in PostgreSQL — the OpenRouter key you add through the Admin UI, any
> future provider keys, everything in the `LiteLLM_*` credential tables.
>
> **If you change this value, every credential already encrypted in the database
> becomes permanently undecryptable.** There is no recovery, no re-derivation and
> no support path. You would have to wipe the database and re-enter every
> credential by hand.
>
> Generate it once. Back it up somewhere durable and offline (password manager,
> sealed envelope, your secrets vault) *before* the first `up`. Restore it byte
> for byte if you ever rebuild the host.
>
> Note also: if `LITELLM_SALT_KEY` is left unset, LiteLLM silently falls back to
> using `LITELLM_MASTER_KEY` as the salt. That means rotating your master key would
> then also destroy your stored credentials. Always set both, explicitly and
> separately.

`scripts/backup.sh` records a **fingerprint** of the salt key (`sha256:` plus the
first 16 hex characters of a SHA-256 over `woow-litellm-salt-v1:<key>`) in each
archive's `MANIFEST.txt`. `scripts/restore.sh` compares that fingerprint against
the running stack and refuses to restore on a confirmed mismatch. The fingerprint
is one-way; the key itself is never written into a backup.

### 2.3 `POSTGRES_PASSWORD`

```bash
openssl rand -hex 24
```

Avoid `:` `/` `?` `#` `[` `]` `@` and `%` in the password — those characters are
reserved in a URI and would have to be percent-encoded inside `DATABASE_URL`. The
hex output above avoids the problem entirely. Also avoid `$` and `#` if you are
using the Quadlet path, because `EnvironmentFile=` values are parsed by systemd.

### 2.4 `DATABASE_URL`

This is derived, not generated. It must contain the *same* password you just
generated, and it must point at the Postgres container by name:

```
postgresql://litellm:<POSTGRES_PASSWORD>@litellm-postgres:5432/litellm
```

`litellm-postgres` is the container name and a network alias in both paths, so
`localhost` and `127.0.0.1` will **not** work here. The default `podman` network
has no DNS at all — that is exactly why both paths create a user-defined bridge
network (`litellm-network` for compose, `litellm-net` for Quadlet), which enables
`aardvark-dns` name resolution.

### 2.5 `OPENROUTER_API_KEY`

Copy it from <https://openrouter.ai/keys>. It is referenced from
`config/config.yaml` as `api_key: os.environ/OPENROUTER_API_KEY`, so the value only
ever lives in your env file — never in the config file, never in git.

### 2.6 Summary

| Variable | How to produce it | Placeholder in the examples |
|---|---|---|
| `OPENROUTER_API_KEY` | openrouter.ai/keys | `sk-or-REPLACE_ME` |
| `LITELLM_MASTER_KEY` | `echo "sk-$(openssl rand -hex 32)"` | `sk-REPLACE_ME` |
| `LITELLM_SALT_KEY` | `echo "sk-$(openssl rand -hex 32)"` | `sk-REPLACE_ME` |
| `POSTGRES_PASSWORD` | `openssl rand -hex 24` | `CHANGE_ME_TO_SECURE_PASSWORD` |
| `DATABASE_URL` | assembled by hand from the above | `postgresql://litellm:CHANGE_ME_TO_SECURE_PASSWORD@litellm-postgres:5432/litellm` |

Both `install.sh` and `smoke-test.sh` reject values still matching
`REPLACE_ME`, `CHANGE_ME`, `PASTE_..._HERE` or `<your...>`, so a half-filled env
file fails fast instead of producing a confusing runtime error.

---

## 3. Path A — podman-compose

Use this path for a laptop, a dev box, a Portainer stack, or anywhere you do not
need the stack to come back automatically after a host reboot without a logged-in
session. It matches the shape of the sibling WOOWTECH compose repos.

### 3.1 Clone the repository

```bash
git clone https://github.com/WOOWTECH/Woow_podman_litellm.git
cd Woow_podman_litellm
```

### 3.2 Create the environment file

```bash
cp .env.example .env
chmod 600 .env
```

`.env.example` is 340+ lines of bilingual (English / 中文) commentary around a dozen
variables. Read it — it explains each one in place.

### 3.3 Fill in the values

Edit `.env` and replace every placeholder using the values from
[section 2](#2-preparing-secrets):

```bash
${EDITOR:-vi} .env
```

The variables that **must** change (the compose file uses `${VAR:?message}` for
each of them, so `podman-compose` refuses to start if any is missing):

```dotenv
OPENROUTER_API_KEY=sk-or-...           # was sk-or-REPLACE_ME
LITELLM_MASTER_KEY=sk-...              # was sk-REPLACE_ME
LITELLM_SALT_KEY=sk-...                # was sk-REPLACE_ME  ← set once, never rotate
POSTGRES_PASSWORD=...                  # was CHANGE_ME_TO_SECURE_PASSWORD
DATABASE_URL=postgresql://litellm:...@litellm-postgres:5432/litellm
```

Variables you can usually leave alone:

```dotenv
POSTGRES_USER=litellm
POSTGRES_DB=litellm
LITELLM_PORT=4000
STORE_MODEL_IN_DB=True
LITELLM_MODE=PRODUCTION
LITELLM_LOG=INFO
DISABLE_SCHEMA_UPDATE=false            # see section 5 — leave this at false
```

> If you change `POSTGRES_USER` or `POSTGRES_DB`, you must change them in
> `DATABASE_URL` too. The compose healthcheck reads them from the environment
> (`pg_isready -U ${POSTGRES_USER:-litellm} -d ${POSTGRES_DB:-litellm}`) so it
> follows automatically — but the Quadlet unit hardcodes them, see
> [section 4.4](#44-install-the-files).

### 3.4 Validate before starting

```bash
export COMPOSE_PROJECT_NAME=litellm     # gives k3s-like resource names
podman-compose config >/dev/null        # KEEP THE REDIRECT — see the warning below
```

`config` renders the merged file with all variables substituted. If a required
variable is missing you get the message written into the `${VAR:?...}` default —
for example *"POSTGRES_PASSWORD is required - copy .env.example to .env and set a
strong value (openssl rand -hex 24)"*. Fix it before continuing.

> **Do not run `podman-compose config` with stdout attached to your terminal.**
> "All variables substituted" includes the secret ones. Because this stack passes
> credentials through `environment:` with `${VAR:?...}` interpolation, the rendered
> output contains `OPENROUTER_API_KEY`, `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`,
> `POSTGRES_PASSWORD` and the password-bearing `DATABASE_URL` **in cleartext**. Left
> unredirected they land in terminal scrollback, in `tee`/CI logs, and in whatever a
> frustrated operator pastes into a GitHub issue or a support chat — at which point
> every one of them must be rotated (and `LITELLM_SALT_KEY` cannot be rotated without
> making every encrypted DB column undecryptable, see §2.2).
>
> `>/dev/null` is enough: the `${VAR:?...}` guard messages you actually need are
> written to **stderr** and still appear. If you genuinely need to inspect the
> rendered file, write it somewhere private and delete it immediately:
>
> ```bash
> ( umask 077; podman-compose config > /tmp/compose.rendered.yml )
> less /tmp/compose.rendered.yml
> shred -u /tmp/compose.rendered.yml 2>/dev/null || rm -f /tmp/compose.rendered.yml
> ```

Add `export COMPOSE_PROJECT_NAME=litellm` to your shell profile, or prefix every
compose command with it, so that subsequent commands address the same project.

### 3.5 Start the stack

```bash
podman-compose up -d
```

This creates, in order:

1. the bridge network declared as `litellm-network`, created as
   `<project>_litellm-network` (`litellm_litellm-network` with the
   `COMPOSE_PROJECT_NAME` exported above);
2. the named volume declared as `pgdata`, created as `<project>_pgdata`
   (`litellm_pgdata`);
3. container `litellm-postgres` from `postgres:16-alpine`;
4. container `litellm` from `ghcr.io/berriai/litellm:v1.83.14-stable`, but only
   after Postgres reports healthy — the compose file declares
   `depends_on: postgres: condition: service_healthy`.

### 3.6 Watch the first boot

```bash
podman-compose ps
podman logs -f litellm
```

**What "normal" looks like on the very first boot:**

| Stage | Duration | What you see |
|---|---|---|
| Image pull | 1–10 min | pull progress; the LiteLLM image is large |
| Postgres `initdb` | 5–20 s | *"database system is ready to accept connections"* |
| Postgres healthcheck | up to ~60 s | `start_period 10s`, then `pg_isready` every 10 s, 5 retries |
| LiteLLM starts | — | gated on Postgres being healthy |
| Prisma migrations | 20–90 s | `prisma migrate deploy` creates the `LiteLLM_*` tables |
| LiteLLM healthcheck | `start_period` 120 s | then every 20 s, 6 retries, against `/health/liveliness` |

Until `start_period` elapses, `podman ps` shows the litellm container as
`(health: starting)`. That is expected — it is **not** a failure. Give it the full
120 seconds plus a couple of check intervals before you conclude anything.

**Second and subsequent boots** are much faster: images are cached, Postgres skips
`initdb` because `PGDATA=/var/lib/postgresql/data/pgdata` already contains a
cluster, and `prisma migrate deploy` finds no pending migrations and becomes a
near no-op. Expect the stack to be healthy in well under a minute.

### 3.7 Confirm it works

```bash
curl -s http://127.0.0.1:4000/health/liveliness
curl -s http://127.0.0.1:4000/health/readiness
```

Then run the full check — see [section 6](#6-verification):

```bash
./scripts/smoke-test.sh --mode compose
```

### 3.8 Port binding

By default `docker-compose.yml` publishes on **all interfaces**:

```yaml
ports:
  - "${LITELLM_PORT:-4000}:4000"
```

If the host is not on a trusted network, switch to the loopback-only alternative
that is already present, commented out, in the compose file:

```yaml
ports:
  - "127.0.0.1:${LITELLM_PORT:-4000}:4000"
```

Then put a reverse proxy in front — see [section 8](#8-external-access). The
Quadlet path already binds loopback-only by default.

### 3.9 Lifecycle commands

```bash
podman-compose ps            # status
podman-compose logs -f       # both services
podman-compose restart       # restart both
podman-compose stop          # stop, keep containers
podman-compose down          # remove containers + network — DATA IS PRESERVED
```

> `podman-compose down -v` also deletes the `pgdata` volume. That destroys every
> virtual key, team, user, spend record and stored credential. See
> [section 11](#11-uninstall).

---

## 4. Path B — Quadlet + `systemd --user`

Use this path on a server. Quadlet turns the unit files in `quadlet/` into real
systemd services, so you get dependency ordering, health-gated startup, automatic
restart, `journalctl` integration and start-at-boot without a logged-in session.
This is the closest Podman analogue to the k3s deployment.

Everything below is **rootless** (running as your own unprivileged user). For the
rootful variant see [section 9](#9-rootless-vs-rootful).

### 4.1 What goes where

Quadlet only reads `.container`, `.volume`, `.network`, `.pod`, `.kube`, `.image`
and `.build` files. A plain `.service` file dropped into the Quadlet directory is
**silently ignored** — it must go into the ordinary user unit directory instead.

| Repo file | Installed to | Why |
|---|---|---|
| `quadlet/litellm.network` | `~/.config/containers/systemd/` | Quadlet unit |
| `quadlet/litellm-pgdata.volume` | `~/.config/containers/systemd/` | Quadlet unit |
| `quadlet/litellm-postgres.container` | `~/.config/containers/systemd/` | Quadlet unit |
| `quadlet/litellm.container` | `~/.config/containers/systemd/` | Quadlet unit |
| `quadlet/litellm-wait-postgres.service` | `~/.config/systemd/user/` | plain systemd unit, **not** Quadlet |
| `config/config.yaml` | `~/.config/litellm/config.yaml` | bind-mounted read-only into the proxy |
| `.env.quadlet.example` (filled in) | `~/.config/litellm/litellm.env` | `EnvironmentFile=` for both containers |

Directory modes: `~/.config/litellm` should be `0700`, `litellm.env` `0600`,
`config.yaml` `0644`.

### 4.2 Generated unit names

**Quadlet derives the systemd unit name from the *filename*, not from
`ContainerName=` / `VolumeName=` / `NetworkName=`.** This trips people up
constantly, so here is the authoritative map for this repo:

| File in `quadlet/` | Generated systemd unit | Podman resource created |
|---|---|---|
| `litellm.network` | `litellm-network.service` | network **`litellm-net`** |
| `litellm-pgdata.volume` | `litellm-pgdata-volume.service` | volume `litellm-pgdata` |
| `litellm-postgres.container` | `litellm-postgres.service` | container `litellm-postgres` |
| `litellm.container` | `litellm.service` | container `litellm` |
| `litellm-wait-postgres.service` | `litellm-wait-postgres.service` (not generated — it *is* the unit) | throwaway container `litellm-wait-postgres` |

The rules: `<name>.container` → `<name>.service`; `<name>.volume` →
`<name>-volume.service`; `<name>.network` → `<name>-network.service`.

> **Do not name the network unit after `NetworkName=`.** `NetworkName=litellm-net`
> changes only the *Podman* network name; the systemd unit is still
> **`litellm-network.service`**, from the filename. An `After=` on a mis-typed
> unit name is a silent no-op in systemd, so the mistake never errors — it just
> quietly drops the ordering. Always confirm the real names on your host:
>
> ```bash
> systemctl --user list-unit-files 'litellm*'
> systemctl --user list-units --all 'litellm*'
> ```

Podman also prefixes resource names with `systemd-` unless you override them.
All four units set `ContainerName=` / `VolumeName=` / `NetworkName=` explicitly, so
the resources are named as shown in the table above, not `systemd-litellm` etc.

### 4.3 Prepare the environment file

```bash
mkdir -p ~/.config/litellm
chmod 700 ~/.config/litellm
cp .env.quadlet.example ~/.config/litellm/litellm.env
chmod 600 ~/.config/litellm/litellm.env
${EDITOR:-vi} ~/.config/litellm/litellm.env
```

> **`EnvironmentFile=` is not a shell.** `~/.config/litellm/litellm.env` is parsed
> by systemd, not sourced by bash. That means:
>
> - plain `KEY=VALUE` only;
> - **no** `${VAR}`, `${VAR:-default}` or `${VAR:?message}` — those are compose-only
>   and would be passed through as literal text;
> - **no** command substitution (`$(...)`, backticks);
> - **no** `export ` prefix (`install.sh` explicitly rejects lines starting with it);
> - no trailing `# comment` on a value line — it becomes part of the value;
> - avoid `$` and `#` inside passwords.
>
> Also note this file does **not** contain `LITELLM_PORT` or `PGDATA`. The published
> port is static text in `litellm.container` (`PublishPort=127.0.0.1:4000:4000`) and
> `PGDATA` is set with `Environment=PGDATA=/var/lib/postgresql/data/pgdata` in
> `litellm-postgres.container`.

Set the same five secrets as path A:
`OPENROUTER_API_KEY`, `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`,
`POSTGRES_PASSWORD`, `DATABASE_URL` (plus `POSTGRES_USER` / `POSTGRES_DB` if you
deviate from `litellm` / `litellm`).

> **If you change `POSTGRES_USER` or `POSTGRES_DB`**, you must also edit the
> healthcheck in `quadlet/litellm-postgres.container`, which hardcodes them:
>
> ```ini
> HealthCmd=pg_isready -U litellm -d litellm
> ```
>
> They are hardcoded on purpose: a literal `$` in `HealthCmd=` is subject to systemd
> variable expansion, so `-U $POSTGRES_USER` would not do what you expect.

### 4.4 Install the files

The scripted way, which is what this repo expects you to use:

```bash
# Tracked mode is 755, so a git clone is already executable.
# Only needed if you unpacked a zip/tarball, which loses the mode bit.
chmod +x quadlet/*.sh scripts/*.sh

# Validate the unit files first — installs nothing, starts nothing
./quadlet/install.sh --dry-run

# Real run — this is the step that validates the env file
./quadlet/install.sh --env-file ~/.config/litellm/litellm.env
```

> `--dry-run` only proves the Quadlet units parse. It does **not** read
> `--env-file`, so it will not catch leftover placeholders or missing keys —
> those are checked by the real run, which refuses to proceed if it finds them.

`install.sh` flags:

| Flag | Effect |
|---|---|
| `--dry-run` | run the Quadlet generator over `quadlet/` to prove the units parse, then exit; does **not** read `--env-file`, install, or start anything |
| `--env-file PATH` | source env file to install (default: `<repo>/.env`) |
| `--no-pull` | skip pre-pulling images (first start will be slower and may hit `TimeoutStartSec`) |
| `--no-linger` | skip `loginctl enable-linger` (the stack will then **not** start at boot) |
| `-h`, `--help` | usage |

Environment override: `LITELLM_HEALTH_TIMEOUT` (seconds to wait for health at the
end, default `300`).

What it does, in order:

1. **Preflight** — Podman present; parse the version; exit if < 4.4; warn if < 5.0
   (`Notify=healthy`); note if < 5.5 (native `Memory=` key); check cgroups v2.
2. **Validate the env file** — required keys present
   (`OPENROUTER_API_KEY`, `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`, `DATABASE_URL`,
   `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`); reject leftover
   placeholders; reject `export ` prefixes; warn if `DATABASE_URL` does not point at
   `litellm-postgres:5432` or `postgres:5432`.
3. **Create directories** — `~/.config/containers/systemd`, `~/.config/systemd/user`,
   `~/.config/litellm`.
4. **Install `config.yaml` and the env file** with the right modes.
5. **Install the four Quadlet units and the one plain unit**, rewriting the
   hardcoded `/usr/bin/podman` path in `litellm-wait-postgres.service` to whatever
   `command -v podman` reports.
6. **Validate with the Quadlet generator dry-run** (see 4.5).
7. **Pre-pull** `docker.io/library/postgres:16-alpine` and
   `ghcr.io/berriai/litellm:v1.83.14-stable`.
8. **`loginctl enable-linger`** unless `--no-linger`.
9. **`systemctl --user daemon-reload`** and start the units in order.
10. **Poll for health** until `LITELLM_HEALTH_TIMEOUT`.

Exit code `0` means full success; `1` means partial (something started but health
did not come up in time — check the logs, do not assume failure).

<details>
<summary>Manual equivalent, if you prefer not to run the script</summary>

```bash
mkdir -p ~/.config/containers/systemd ~/.config/systemd/user ~/.config/litellm
chmod 700 ~/.config/litellm

install -m 0644 quadlet/litellm.network              ~/.config/containers/systemd/
install -m 0644 quadlet/litellm-pgdata.volume        ~/.config/containers/systemd/
install -m 0644 quadlet/litellm-postgres.container   ~/.config/containers/systemd/
install -m 0644 quadlet/litellm.container            ~/.config/containers/systemd/
install -m 0644 quadlet/litellm-wait-postgres.service ~/.config/systemd/user/

install -m 0644 config/config.yaml ~/.config/litellm/config.yaml
# litellm.env: create it as in 4.3, mode 0600

podman pull docker.io/library/postgres:16-alpine
podman pull ghcr.io/berriai/litellm:v1.83.14-stable
```

</details>

### 4.5 Validate the units before starting anything

Quadlet parse errors are reported only when the generator runs, and a unit that
fails to generate simply does not exist. Run the generator by hand first:

```bash
/usr/lib/systemd/system-generators/podman-system-generator --user --dryrun
```

On some distributions the binary lives elsewhere:

```bash
/usr/libexec/podman/quadlet --user --dryrun
/usr/lib64/systemd/system-generators/podman-system-generator --user --dryrun
```

The dry run prints each generated unit to stdout and any parse errors to stderr.
You should see `litellm-network.service`, `litellm-pgdata-volume.service`,
`litellm-postgres.service` and `litellm.service`. If one is missing, the
corresponding source file has an error.

### 4.6 Enable linger

Without linger, the systemd user manager is torn down when your last session ends,
taking the containers with it — and it is never started at boot.

```bash
loginctl enable-linger "$USER"
loginctl show-user "$USER" --property=Linger      # expect Linger=yes
```

`install.sh` does this for you unless you passed `--no-linger`.

### 4.7 Reload and start

```bash
systemctl --user daemon-reload
```

`daemon-reload` re-runs the Quadlet generator, so it must be repeated after **every**
edit to a file in `~/.config/containers/systemd/`.

Start in dependency order:

```bash
systemctl --user start litellm-network.service
systemctl --user start litellm-pgdata-volume.service
systemctl --user start litellm-postgres.service
systemctl --user start litellm-wait-postgres.service
systemctl --user start litellm.service
```

In practice `systemctl --user start litellm.service` alone is enough: Quadlet
auto-injects `Requires=` and `After=` on the `.volume` and `.network` units that
`litellm-postgres.container` references, and `litellm.container` declares
`Requires=/After=litellm-postgres.service` plus `Wants=/After=litellm-wait-postgres.service`
itself. Starting explicitly, in order, just makes failures easier to localise.

`litellm-wait-postgres.service` is a `Type=oneshot` / `RemainAfterExit=yes` unit
that runs a throwaway `postgres:16-alpine` container which polls
`pg_isready -h litellm-postgres` every 3 s for up to 180 s. It is the belt to
`Notify=healthy`'s braces, and it is what makes a Podman 4.x fallback viable.

### 4.8 How boot-time autostart actually works

> **You cannot `systemctl --user enable` a Quadlet unit.** The generated services
> are transient — they exist only in `/run`, they have no file on disk to symlink,
> and `systemctl enable` will fail with *"unit file does not exist"* or
> *"transient or generated"*.

Autostart comes from two things instead:

1. **`[Install] WantedBy=default.target multi-user.target`** inside
   `litellm.container` and `litellm-postgres.container`. When `daemon-reload` runs
   the generator, systemd applies that `[Install]` section automatically, creating
   the wants-symlinks in `/run` for you.
2. **Linger** (4.6), which starts your systemd user manager at boot without a login,
   which in turn reaches `default.target`, which pulls in the two services.

Verify both:

```bash
systemctl --user list-dependencies default.target | grep -i litellm
loginctl show-user "$USER" --property=Linger
```

To disable autostart for one service without uninstalling anything, delete or
comment its `[Install]` section and `daemon-reload`. To disable it for the whole
stack, `loginctl disable-linger "$USER"`.

### 4.9 Checking status

```bash
# systemd's view
systemctl --user status litellm-postgres.service
systemctl --user status litellm.service
systemctl --user list-units --all 'litellm*'

# Podman's view
podman ps -a --filter 'label=io.woowtech.stack=litellm-gw'
podman inspect --format '{{.State.Health.Status}}' litellm-postgres
podman inspect --format '{{.State.Health.Status}}' litellm

# Force a healthcheck to run right now instead of waiting for the next interval
podman healthcheck run litellm-postgres
podman healthcheck run litellm
```

Because `litellm-postgres.container` sets `Notify=healthy`, its generated service is
`Type=notify`: systemd reports it as `activating` until the container's healthcheck
first passes, and only then `active (running)`. That is what gates everything
downstream. `litellm.container` deliberately leaves `Notify=` unset and relies on
`HealthStartupCmd` (`/health/readiness`, every 15 s, up to 40 attempts = 10 minutes)
plus the ordinary `HealthCmd` (`/health/liveliness`, every 20 s, 6 retries,
`HealthStartPeriod=120s`) instead.

### 4.10 Logs

```bash
# Follow the proxy
journalctl --user -u litellm.service -f

# Follow Postgres
journalctl --user -u litellm-postgres.service -f

# The readiness gate writes with SyslogIdentifier=litellm-wait-postgres
journalctl --user -t litellm-wait-postgres -n 100

# Everything from this stack since the last boot
journalctl --user -u 'litellm*' -b

# Podman's own container logs (equivalent content, different plumbing)
podman logs -f litellm
podman logs --tail 200 litellm-postgres
```

### 4.11 Restart policy

Quadlet does **not** emit a `Restart=` directive for `.container` units, so both
container units set it by hand:

```ini
[Service]
Restart=always
RestartSec=10
```

`StartLimitIntervalSec=300` / `StartLimitBurst=5` cap the restart loop — five
failures inside five minutes and systemd stops trying, which is the closest analogue
to Kubernetes' `CrashLoopBackOff`. To clear that state after fixing the cause:

```bash
systemctl --user reset-failed litellm.service
systemctl --user start litellm.service
```

`TimeoutStartSec` is raised well above the systemd default of 90 s (300 s for
Postgres, 600 s for LiteLLM) because a cold image pull plus first-boot Prisma
migrations comfortably exceeds 90 s.

---

## 5. First boot vs steady state (`DISABLE_SCHEMA_UPDATE`)

This is the single most common way to get a stack that starts cleanly and then
fails every request with a database error. Read this section before your first
`up`.

### 5.1 What the variable does

LiteLLM's entrypoint runs a Prisma step against `DATABASE_URL` at startup. Which
step depends on `DISABLE_SCHEMA_UPDATE`:

| Value | What runs | Effect |
|---|---|---|
| `false` (LiteLLM's default) | `prisma migrate deploy` | applies pending migrations — **creates the `LiteLLM_*` tables** |
| `true` | `prisma migrate diff` | read-only. Prints the SQL that *would* be needed. **Creates nothing.** |

The trap: with `DISABLE_SCHEMA_UPDATE=true` against an **empty** database, the
container starts, passes `/health/liveliness`, and looks completely fine — because
the liveliness probe only proves the process is up. The first request that touches
the database then fails with `relation "LiteLLM_VerificationToken" does not exist`
or similar.

Note also that LiteLLM parses truthy strings loosely: `true`, `1`, `t`, `y` and
`yes` all mean true. `DISABLE_SCHEMA_UPDATE=1` is *on*, not "version 1".

### 5.2 What this repo does

**Both paths ship `DISABLE_SCHEMA_UPDATE=false`, and you should leave it there.**

- `docker-compose.yml`: `DISABLE_SCHEMA_UPDATE: "${DISABLE_SCHEMA_UPDATE:-false}"`
- `.env.example`: `DISABLE_SCHEMA_UPDATE=false`
- `quadlet/litellm.container`: `Environment=DISABLE_SCHEMA_UPDATE=false`

This is a **deliberate divergence from the k3s manifests**, which set it to `true`.
The reason for the difference:

- On k3s, schema management is handled out of band — a separate migration job runs
  against the database, and the long-lived proxy pods are deliberately denied the
  ability to mutate the schema. Multiple replicas racing `migrate deploy` on
  rollout is a real hazard there.
- Here there is exactly **one** proxy container, no rollout, and no separate
  migration job. Something has to create the schema, and `migrate deploy` in the
  single container is that something.

**Do not copy `true` over from the k3s manifests.** If you already did and hit the
symptom, the fix is:

```bash
# Path A
sed -i 's/^DISABLE_SCHEMA_UPDATE=.*/DISABLE_SCHEMA_UPDATE=false/' .env
podman-compose up -d --force-recreate litellm

# Path B — the value is in the unit, not the env file
${EDITOR:-vi} ~/.config/containers/systemd/litellm.container   # Environment=DISABLE_SCHEMA_UPDATE=false
systemctl --user daemon-reload
systemctl --user restart litellm.service
```

### 5.3 When `true` is the right answer

Only once the schema exists **and** you want to pin it — for example if you upgrade
the LiteLLM image and want to review the migration SQL before it runs, or if your
DBA owns the schema. In that workflow:

1. Set `DISABLE_SCHEMA_UPDATE=true` and restart.
2. Read the SQL the container prints on startup (`prisma migrate diff` output).
3. Apply it yourself, or flip the flag back to `false` for one restart, then back.

Steady state for this package is `false`. It is idempotent: on every boot after the
first, `migrate deploy` finds nothing pending and does nothing.

### 5.4 Confirming the schema exists

`scripts/smoke-test.sh` check 7 exists precisely to catch this. Manually:

```bash
podman exec litellm-postgres psql -U litellm -d litellm -tAc \
  "SELECT count(*) FROM information_schema.tables
    WHERE table_schema='public' AND table_name LIKE 'LiteLLM%';"
```

A healthy stack returns a count well above zero. `0` means the migrations never
ran — check `DISABLE_SCHEMA_UPDATE`, then check `podman logs litellm` for the
Prisma output.

---

## 6. Verification

### 6.1 The smoke test

```bash
./scripts/smoke-test.sh                    # auto-detects the deployment mode
./scripts/smoke-test.sh --mode compose
./scripts/smoke-test.sh --mode quadlet
./scripts/smoke-test.sh --env-file ~/.config/litellm/litellm.env
./scripts/smoke-test.sh --help
```

| Exit code | Meaning |
|---|---|
| `0` | all 7 checks passed |
| `1` | one or more checks failed |
| `2` | usage error / could not determine the environment |

Environment overrides: `PODMAN` (path to the binary), `LITELLM_CONTAINER`
(default `litellm`), `POSTGRES_CONTAINER` (default `litellm-postgres`).

Mode auto-detection, in order: a `PODMAN_SYSTEMD_UNIT=` variable inside the
container ⇒ quadlet; a `com.docker.compose.project=` / `io.podman` label ⇒ compose;
`systemctl --user is-active litellm.service` ⇒ quadlet; a repo-local
`docker-compose.yml` ⇒ compose.

Env-file discovery follows the mode: quadlet checks
`~/.config/litellm/litellm.env` then `<repo>/.env`; compose checks them in the
reverse order. The file is **parsed**, never sourced, and the master key is handed
to `curl` through a mode-`0600` temp file removed by an `EXIT` trap — it never
appears in `ps` output or your shell history.

### 6.2 The seven checks and their manual equivalents

**Check 1 — both containers are running**

```bash
podman ps --format '{{.Names}}'
# expect litellm and litellm-postgres
```

**Check 2 — the Postgres healthcheck is healthy**

```bash
podman inspect --format '{{.State.Health.Status}}' litellm-postgres
# expect: healthy

# If it is not, trigger a check immediately rather than waiting 10s
podman healthcheck run litellm-postgres

# The underlying probe, run by hand
podman exec litellm-postgres pg_isready -U litellm -d litellm
```

**Check 3 — the LiteLLM healthcheck is healthy**

```bash
podman inspect --format '{{.State.Health.Status}}' litellm
podman healthcheck run litellm
```

`starting` is reported separately by the smoke test, with a pointer at first-boot
Prisma migrations. On a cold first boot, wait out `HealthStartPeriod=120s` before
treating it as a fault.

**Check 4 — liveliness**

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4000/health/liveliness
# expect 200
```

Process-only. Proves uvicorn is answering; proves nothing about the database.

**Check 5 — readiness**

```bash
curl -sS http://127.0.0.1:4000/health/readiness | python3 -m json.tool
```

Expect HTTP 200 and a `db` field in the JSON. Readiness is database-aware and
returns **503** when Postgres is unreachable — this is the probe that actually tells
you the stack is usable.

**Check 6 — the model list**

```bash
read -rsp 'master key: ' LITELLM_MASTER_KEY; echo
curl -sS http://127.0.0.1:4000/v1/models \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" | python3 -m json.tool
```

Expect 200 and a non-empty `data` array containing every uncommented `model_name`
from `config/config.yaml`:

| `model_name` | routed to |
|---|---|
| `gpt-4o-mini` | `openrouter/openai/gpt-4o-mini` |
| `glm-4.6` | `openrouter/z-ai/glm-4.6` |
| `minimax-m2` | `openrouter/minimax/minimax-m2` |
| `claude-sonnet-4.5` | `openrouter/anthropic/claude-sonnet-4.5` |
| `llama-3.3-70b` | `openrouter/meta-llama/llama-3.3-70b-instruct` |

Extra entries added later through the Admin UI are fine — the smoke test only
requires that the file's models are all present.

**Check 7 — the database schema exists**

```bash
podman exec litellm-postgres psql -U litellm -d litellm -tAc \
  "SELECT count(*) FROM information_schema.tables
    WHERE table_schema='public' AND table_name LIKE 'LiteLLM%';"
```

See [section 5.4](#54-confirming-the-schema-exists).

**Informational (not counted) — the published port answers**

```bash
timeout 3 bash -c 'exec 3<>/dev/tcp/127.0.0.1/4000 && echo open'
```

### 6.3 An end-to-end completion

The checks above never spend OpenRouter credit. To prove the whole path works,
send one real request — this **does** cost a fraction of a cent:

```bash
curl -sS http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"ping"}]}' \
  | python3 -m json.tool
```

### 6.4 Two things not to do

> **Do not probe plain `/health`.** Unlike `/health/liveliness` and
> `/health/readiness`, `/health` requires the master key **and** issues a real
> request to every configured model. It is slow and it burns OpenRouter credit
> every time it runs. Never wire it into a healthcheck or a monitoring loop.

> **Do not use `curl` inside the LiteLLM container.** The image ships Python but
> **not** curl. That is why both healthchecks use a Python one-liner:
>
> ```bash
> podman exec litellm python -c \
>   "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:4000/health/liveliness').status==200 else 1)"
> ```
>
> From the host, plain `curl http://127.0.0.1:4000/...` is fine.

---

## 7. Day-2 operations

### 7.1 Logs

| | Path A (compose) | Path B (Quadlet) |
|---|---|---|
| Follow the proxy | `podman-compose logs -f litellm` | `journalctl --user -u litellm.service -f` |
| Follow Postgres | `podman-compose logs -f postgres` | `journalctl --user -u litellm-postgres.service -f` |
| Last 200 lines | `podman logs --tail 200 litellm` | `podman logs --tail 200 litellm` |
| Since boot | — | `journalctl --user -u 'litellm*' -b` |

`podman logs <container>` works identically in both paths.

To raise verbosity, set `LITELLM_LOG=DEBUG` — on path A in `.env`, on path B on the
`Environment=LITELLM_LOG=` line in `quadlet/litellm.container` (setting it in the env
file has no effect there, because `--env` overrides `--env-file`). Be aware of LiteLLM's off-by-one
naming: `LITELLM_LOG=INFO` (the shipped default) is already roughly what the CLI
calls `--debug`, and `DEBUG` is considerably noisier than you expect. Turn it back
down when you are done — debug logs can contain request payloads.

### 7.2 Restart

```bash
# Path A
podman-compose restart litellm
podman-compose restart                 # both services

# Path B
systemctl --user restart litellm.service
systemctl --user restart litellm-postgres.service   # restarts litellm too, via Requires=
```

Restarting Postgres pulls the proxy down with it, because `litellm.container`
declares `Requires=litellm-postgres.service`. Restarting only the proxy is safe and
is what you want for a config change.

### 7.3 Updating the image

Both paths pin an exact tag (`ghcr.io/berriai/litellm:v1.83.14-stable`,
`postgres:16-alpine`). Nothing auto-updates: `AutoUpdate=registry` and `Pull=` are
deliberately left unset in the Quadlet units. Upgrades are a decision you make.

**Always back up first** — see [7.5](#75-backup). A LiteLLM upgrade may run new
Prisma migrations against your database, and those are not trivially reversible.

**Path A:**

```bash
# 1. edit the tag
${EDITOR:-vi} docker-compose.yml        # services.litellm.image

# 2. fetch it
podman-compose pull

# 3. recreate with the new image
podman-compose up -d

# 4. verify
./scripts/smoke-test.sh --mode compose
```

**Path B:**

```bash
# 1. edit the tag in the repo copy AND the installed copy
${EDITOR:-vi} quadlet/litellm.container
${EDITOR:-vi} ~/.config/containers/systemd/litellm.container    # Image=

# 2. fetch it before restarting, so TimeoutStartSec is not spent pulling
podman pull ghcr.io/berriai/litellm:NEW_TAG_YOU_JUST_SET

# 3. regenerate the units and restart
systemctl --user daemon-reload
systemctl --user restart litellm.service

# 4. verify
./scripts/smoke-test.sh --mode quadlet
```

Re-running `./quadlet/install.sh` also works and is less error-prone, since it
copies the repo's units over the installed ones, pre-pulls, reloads and restarts.

**Rolling back** is the same procedure with the old tag. Restore the database from
backup as well if the newer version ran migrations.

For reproducibility you can pin by digest instead of tag —
`quadlet/litellm-postgres.container` carries a commented-out example
(`Image=...@sha256:PASTE_DIGEST_HERE`). Find the digest with:

```bash
podman image inspect docker.io/library/postgres:16-alpine \
  --format '{{index .RepoDigests 0}}'
```

### 7.4 Old images

```bash
podman images
podman image prune          # dangling only
podman system df            # what is actually using disk
```

### 7.5 Backup

```bash
./scripts/backup.sh                                 # auto-detect mode, output to <repo>/backups
./scripts/backup.sh --mode quadlet --out /srv/backups
./scripts/backup.sh --env-file ~/.config/litellm/litellm.env
./scripts/backup.sh --help
```

Produces `<out>/litellm-backup-YYYYmmdd-HHMMSS.tar.gz`, mode `0600`, in a directory
created `0700`. Contents:

| Member | What it is |
|---|---|
| `database.dump` | `pg_dump --format=custom --no-owner --no-privileges` |
| `config.yaml` | a copy of the live `config.yaml` |
| `MANIFEST.txt` | metadata (see below) |

The manifest records `backup_format=1`, `created_utc`, `deploy_mode`,
`podman_version`, `litellm_image`, `postgres_image`, `litellm_tables`,
`dump_bytes` and `salt_key_fingerprint`. The script validates the `PGDMP` magic
bytes of the dump and warns loudly if it counted zero `LiteLLM_*` tables (which
would mean you just backed up an empty database).

> **The archive deliberately does NOT contain `.env` or `litellm.env`.** Your
> secrets are not in the backup. That is intentional — a backup archive gets copied
> around, and it should not be a credential leak. It also means **a backup alone
> cannot restore your stack**: you must separately preserve `LITELLM_SALT_KEY`, or
> everything encrypted in `database.dump` is unreadable. See
> [section 2.2](#22-litellm_salt_key).

The manual equivalent:

```bash
podman exec litellm-postgres pg_dump -U litellm -d litellm \
  --format=custom --no-owner --no-privileges > database.dump
```

Schedule it with a user timer or cron, and copy the archives off the host.

### 7.6 Restore

```bash
./scripts/restore.sh backups/litellm-backup-20260806-101500.tar.gz
./scripts/restore.sh <archive> --mode quadlet
./scripts/restore.sh <archive> --restore-config
./scripts/restore.sh <archive> --force-salt-mismatch
./scripts/restore.sh <archive> --yes
```

| Exit code | Meaning |
|---|---|
| `0` | restore completed |
| `1` | restore failed, or was refused (e.g. salt-key mismatch) |
| `2` | usage error |
| `3` | aborted by the operator at the confirmation prompt |

What it does:

1. Inspect the archive and read `MANIFEST.txt`.
2. **Salt-key gate** — compare the manifest's `salt_key_fingerprint` against the
   live stack's. On a *confirmed* mismatch it **refuses** unless you pass
   `--force-salt-mismatch`. If either fingerprint is unknown it warns loudly and
   continues. Overriding this means the restored credential rows will be
   undecryptable garbage; you have been warned twice.
3. Require you to type `RESTORE` (skipped with `--yes`).
4. Stop **only** the proxy. Postgres stays up — it is the restore target.
5. Against the maintenance database `postgres`:
   `DROP DATABASE IF EXISTS "litellm" WITH (FORCE);` then
   `CREATE DATABASE "litellm" OWNER "litellm";`
   (`WITH (FORCE)` needs PostgreSQL 13+, satisfied by `16-alpine`.)
6. `pg_restore -U litellm -d litellm --no-owner --no-privileges --single-transaction`
   — override the flags with the `PG_RESTORE_ARGS` environment variable if needed.
7. Restart the proxy and count the restored `LiteLLM_*` tables.

`--restore-config` also overwrites `<repo>/config/config.yaml` from the archive. On
the Quadlet path that is not sufficient by itself, because the proxy reads
`~/.config/litellm/config.yaml`; the script warns about this. Copy it across:

```bash
install -m 0644 config/config.yaml ~/.config/litellm/config.yaml
systemctl --user restart litellm.service
```

Finish with `./scripts/smoke-test.sh`, which the script itself suggests.

### 7.7 Resizing resource limits

**Path A** — edit `docker-compose.yml` and recreate:

```yaml
services:
  litellm:
    mem_limit: 4g
    cpus: 4.0
```

```bash
podman-compose up -d
```

`mem_limit` / `cpus` are used rather than `deploy.resources.limits` because
podman-compose does not reliably honour the `deploy:` block outside Swarm.

**Path B** — edit the unit and reload:

```ini
[Container]
PodmanArgs=--memory=4g
PodmanArgs=--cpus=4.0
```

```bash
${EDITOR:-vi} ~/.config/containers/systemd/litellm.container
systemctl --user daemon-reload
systemctl --user restart litellm.service
```

On Podman 5.5+ you may use the native `Memory=4g` key instead of
`PodmanArgs=--memory=4g`. The units use `PodmanArgs=` so they work on 5.0–5.4 too.

> Rootless `--cpus=` requires the `cpu` controller to be delegated to your user
> slice. If it is not, Podman may warn or silently ignore the limit. See
> [section 1.2](#12-cgroups-v2). Confirm what actually applied:
>
> ```bash
> podman inspect --format '{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}}' litellm
> podman stats --no-stream
> ```

### 7.8 Adding or changing a model

Models live in `config/config.yaml`. Add an entry to `model_list`:

```yaml
model_list:
  - model_name: my-new-model
    litellm_params:
      model: openrouter/vendor/model-slug
      api_key: os.environ/OPENROUTER_API_KEY
      api_base: https://openrouter.ai/api/v1
```

> Verify the slug before you restart. OpenRouter retires slugs — the bare
> `anthropic/claude-3.5-sonnet` entry this config once used started returning
> **404 "No endpoints found"**, which is why it was repointed and renamed to
> `claude-sonnet-4.5`. Check against the live catalogue:
>
> ```bash
> curl -s https://openrouter.ai/api/v1/models | python3 -c \
>   "import json,sys; [print(m['id']) for m in json.load(sys.stdin)['data']]" | grep -i <vendor>
> ```

**Apply it — path A:**

```bash
${EDITOR:-vi} config/config.yaml
podman-compose restart litellm
```

The file is bind-mounted (`./config/config.yaml:/app/config.yaml:ro,Z`), so no
rebuild and no `up -d` is needed — just a restart to re-read it.

**Apply it — path B:**

```bash
${EDITOR:-vi} config/config.yaml
install -m 0644 config/config.yaml ~/.config/litellm/config.yaml
systemctl --user restart litellm.service
```

The Quadlet unit mounts `%h/.config/litellm/config.yaml`, i.e. the *installed*
copy — editing only the repo copy changes nothing. Both paths mount the file at the
same in-container path, `/app/config.yaml`, and both pass
`--config /app/config.yaml --port 4000` as arguments.

**In both cases: Postgres does not need restarting.** Only the proxy re-reads
`config.yaml`.

> **How config.yaml and the database interact.** Both paths set
> `store_model_in_db: true` / `STORE_MODEL_IN_DB=True`, so models added through the
> Admin UI are persisted in PostgreSQL. For most settings the database wins.
> **`model_list` is the exception: file models and database models are combined, not
> replaced.** A model defined in `config.yaml` therefore cannot be deleted from the
> Admin UI — it reappears on every restart. To remove one, delete or comment its
> entry in `config.yaml` and restart the proxy.

> **Do not add `entrypoint:` or prefix the command with `litellm`.** The image's
> `ENTRYPOINT` is `docker/prod_entrypoint.sh`, which ends in `exec litellm "$@"`.
> Compose `command:` and Quadlet `Exec=` supply *arguments only*.

---

## 8. External access

> ## ⚠ DO NOT REUSE THE EXISTING k3s CLOUDFLARE TUNNEL TOKEN
>
> **This package deliberately ships no `cloudflared` container, no tunnel service
> and no `TUNNEL_TOKEN` variable.** That is not an oversight.
>
> A Cloudflare tunnel token identifies a tunnel, not a connector. Starting a second
> `cloudflared` with the **same** token registers a second connector against the
> **same** tunnel, and Cloudflare will then load-balance production traffic across
> both — sending an unpredictable share of live requests into this Podman stack,
> which has a different database, different virtual keys and different spend
> records.
>
> The token in use by the existing k3s deployment is in use *right now*. Pasting it
> here would split live traffic and could break that deployment. **Never do it.**
>
> If you genuinely need a Cloudflare tunnel for this stack, create a **new, separate
> tunnel** with its own token and its own hostname in the Cloudflare dashboard, and
> run `cloudflared` yourself outside this repository. This stack is not a target for
> the existing k3s deployment's hostname under any circumstance.

By default the proxy is reachable only from the host:

| Path | Binding |
|---|---|
| A (compose) | `${LITELLM_PORT:-4000}:4000` — **all interfaces** by default; a loopback-only line is provided, commented out |
| B (Quadlet) | `PublishPort=127.0.0.1:4000:4000` — loopback only |

Three documented ways to expose it. Pick one; do not stack them.

### 8.1 Reverse proxy (recommended)

Terminate TLS at a proxy on the same host and forward to `127.0.0.1:4000`. Keep the
container bound to loopback.

**nginx:**

```nginx
server {
    listen 443 ssl http2;
    server_name llm.example.internal;

    ssl_certificate     /etc/letsencrypt/live/llm.example.internal/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/llm.example.internal/privkey.pem;

    # LLM responses are slow; the config sets request_timeout: 600
    proxy_connect_timeout 60s;
    proxy_send_timeout    620s;
    proxy_read_timeout    620s;

    location / {
        proxy_pass http://127.0.0.1:4000;

        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection        "";

        # Required for streaming (stream: true) responses to arrive incrementally
        proxy_buffering off;
        proxy_cache off;

        client_max_body_size 32m;
    }
}
```

**Caddy** (obtains and renews TLS automatically):

```caddyfile
llm.example.internal {
    reverse_proxy 127.0.0.1:4000 {
        # -1 disables response buffering — needed for streaming
        flush_interval -1
        transport http {
            read_timeout  620s
            write_timeout 620s
        }
    }
}
```

If SELinux is enforcing, allow the proxy to make outbound network connections:

```bash
sudo setsebool -P httpd_can_network_connect 1
```

`proxy_buffering off` / `flush_interval -1` are not optional. Without them,
server-sent-event streaming responses are buffered and arrive all at once at the
end, which looks to clients like a hang.

### 8.2 Tailscale (or another WireGuard mesh)

Best option for a small team with no public exposure at all. Install Tailscale on
the host, keep the container on loopback, and reach it over the tailnet:

```bash
# On the host
sudo tailscale up

# Bind the proxy to the tailnet address instead of loopback:
#   compose:  ports: - "100.x.y.z:4000:4000"
#   quadlet:  PublishPort=100.x.y.z:4000:4000
```

Or leave it on loopback and use Tailscale Serve, which keeps the listener private
to the tailnet and adds TLS:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:4000
```

Restrict who can reach it with a tailnet ACL. Do **not** use `tailscale funnel`
here — that publishes to the open internet.

### 8.3 Trusted-LAN port exposure (last resort)

Only on a network you fully control, and only with a host firewall in front:

```bash
# Compose already binds 0.0.0.0 by default; for Quadlet, edit the unit:
#   PublishPort=0.0.0.0:4000:4000
# then: systemctl --user daemon-reload && systemctl --user restart litellm.service

# firewalld: allow one subnet only
sudo firewall-cmd --permanent --new-zone=litellm
sudo firewall-cmd --permanent --zone=litellm --add-source=192.168.10.0/24
sudo firewall-cmd --permanent --zone=litellm --add-port=4000/tcp
sudo firewall-cmd --reload
```

This is plaintext HTTP with the master key travelling in an `Authorization` header.
Never do it across an untrusted network, and never on a public IP.

### 8.4 Whichever you choose

- Issue scoped **virtual keys** for callers instead of handing out
  `LITELLM_MASTER_KEY`:

  ```bash
  curl -sS http://127.0.0.1:4000/key/generate \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    -H 'Content-Type: application/json' \
    -d '{"models":["gpt-4o-mini"],"max_budget":5,"key_alias":"demo"}'
  ```

- Remember `LITELLM_MASTER_KEY` is also the Admin UI password. Anyone who reaches
  the UI with it owns the gateway and its spend.
- Rate-limit at the proxy layer if the endpoint is reachable by more than a handful
  of clients.

---

## 9. Rootless vs rootful

**Rootless is the recommended and default configuration for this package**, and it
is what `quadlet/install.sh` and `quadlet/uninstall.sh` implement. A container
escape lands in an unprivileged user account rather than as `root` on the host; the
volume, the network and the units are all owned by that user; and nothing in this
stack needs a privileged port or host device.

Use rootful only if you have a hard requirement — e.g. you must bind port 443
directly, you need a host device, or a corporate policy demands system-wide units.

| | Rootless (default) | Rootful |
|---|---|---|
| Quadlet unit directory | `~/.config/containers/systemd/` | `/etc/containers/systemd/` |
| Plain unit directory | `~/.config/systemd/user/` | `/etc/systemd/system/` |
| Control command | `systemctl --user ...` | `sudo systemctl ...` |
| Logs | `journalctl --user -u ...` | `sudo journalctl -u ...` |
| Reload | `systemctl --user daemon-reload` | `sudo systemctl daemon-reload` |
| `[Install] WantedBy=` | `default.target` | `multi-user.target` |
| Boot autostart needs | `loginctl enable-linger $USER` | nothing extra |
| `%h` expands to | your home directory | `/root` |
| Env file location | `~/.config/litellm/litellm.env` | `/etc/litellm/litellm.env` |
| Ports below 1024 | blocked by default | allowed |
| Podman command | `podman ...` | `sudo podman ...` |
| Volume data on disk | `~/.local/share/containers/storage/volumes/` | `/var/lib/containers/storage/volumes/` |

The units ship with `WantedBy=default.target multi-user.target` — both, so the same
file works either way.

### 9.1 Converting to rootful

```bash
# 1. Env file and config in a system location
sudo install -d -m 700 /etc/litellm
sudo install -m 600 .env.quadlet.example /etc/litellm/litellm.env   # then fill it in
sudo install -m 644 config/config.yaml   /etc/litellm/config.yaml

# 2. Units into the system Quadlet directory
sudo install -m 644 quadlet/litellm.network            /etc/containers/systemd/
sudo install -m 644 quadlet/litellm-pgdata.volume      /etc/containers/systemd/
sudo install -m 644 quadlet/litellm-postgres.container /etc/containers/systemd/
sudo install -m 644 quadlet/litellm.container          /etc/containers/systemd/
sudo install -m 644 quadlet/litellm-wait-postgres.service /etc/systemd/system/

# 3. Repoint the paths: %h is /root under rootful, so change
#      EnvironmentFile=%h/.config/litellm/litellm.env  ->  /etc/litellm/litellm.env
#      Volume=%h/.config/litellm/config.yaml:...       ->  /etc/litellm/config.yaml:...
#    in both .container files.

# 4. Validate, reload, start
sudo /usr/lib/systemd/system-generators/podman-system-generator --dryrun
sudo systemctl daemon-reload
sudo systemctl start litellm.service
sudo systemctl status litellm.service
```

`quadlet/install.sh` and `quadlet/uninstall.sh` are rootless-only. Under rootful you
manage the files manually as above.

### 9.2 Privileged ports

Rootless Podman cannot bind ports below 1024 by default. Preferred fix: leave the
proxy on 4000 and terminate 443 at a reverse proxy ([section 8.1](#81-reverse-proxy-recommended)).

If you truly must bind low ports rootless:

```bash
echo 'net.ipv4.ip_unprivileged_port_start=80' | \
  sudo tee /etc/sysctl.d/99-unprivileged-ports.conf
sudo sysctl --system
```

This lowers the threshold for **every** unprivileged process on the host. Prefer the
reverse proxy.

---

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `litellm-postgres` stuck `unhealthy` | `pg_isready` is being run with the wrong user/db. The Quadlet `HealthCmd=` hardcodes `-U litellm -d litellm`. | Run `podman exec litellm-postgres pg_isready -U litellm -d litellm` by hand. If you changed `POSTGRES_USER` / `POSTGRES_DB`, edit `HealthCmd=` in `~/.config/containers/systemd/litellm-postgres.container` to match, then `systemctl --user daemon-reload && systemctl --user restart litellm-postgres.service`. |
| Postgres exits at startup with *"directory ... exists but is not empty"* | The data directory contains files that are not a cluster. | The units set `PGDATA=/var/lib/postgresql/data/pgdata` precisely to avoid this (the entrypoint refuses to `initdb` into a non-empty directory). Confirm `PGDATA` is set; if the volume is genuinely corrupt and you have a backup, remove the volume and restore. |
| `litellm` stuck `starting` for a few minutes on first boot | Normal. `HealthStartPeriod=120s`, plus a large image pull and first-boot Prisma migrations. | Wait. Watch progress with `podman logs -f litellm`. Only treat it as a fault after `HealthStartupRetries=40 × 15s` (10 minutes) have elapsed. |
| `litellm` never becomes healthy | Cannot reach the database, or bad `DATABASE_URL`. | `curl -s http://127.0.0.1:4000/health/readiness` — a 503 with a failing `db` field confirms it. Check `DATABASE_URL` points at `litellm-postgres:5432` (**not** `localhost`) and that the password matches `POSTGRES_PASSWORD`. |
| `relation "LiteLLM_..." does not exist` | `DISABLE_SCHEMA_UPDATE=true` against an empty database — `prisma migrate diff` printed the SQL but created nothing. | Set it to `false` and restart the proxy. See [section 5](#5-first-boot-vs-steady-state-disable_schema_update). Verify with the table-count query in [6.2 check 7](#62-the-seven-checks-and-their-manual-equivalents). |
| `401 Unauthorized` from the proxy | Missing or wrong `Authorization` header, or `LITELLM_MASTER_KEY` differs between your shell and the container. | Send `-H "Authorization: Bearer $LITELLM_MASTER_KEY"`. Compare with `podman exec litellm printenv LITELLM_MASTER_KEY`. If you changed the key, restart the proxy — it is read at startup. |
| `404` / *"No endpoints found"* from OpenRouter | The model slug was retired upstream. This already happened once here with the bare `anthropic/claude-3.5-sonnet` slug. | Check the live catalogue: `curl -s https://openrouter.ai/api/v1/models`. Update the `model:` field in `config/config.yaml` and restart the proxy ([7.8](#78-adding-or-changing-a-model)). |
| `401` from OpenRouter, or *"insufficient credits"* | `OPENROUTER_API_KEY` is wrong/revoked, or the account has no balance. | Verify the key at <https://openrouter.ai/keys> and confirm the balance. Confirm it reached the container: `podman exec litellm printenv OPENROUTER_API_KEY \| cut -c1-8`. |
| `permission denied` on the Postgres data directory (rootless) | The container's postgres UID (70) does not map to a writable host UID with a bind mount. | Use the **named volume** both paths already use, not a bind mount. If you must bind-mount, either add `:U` to the mount or run `podman unshare chown -R 70:70 /path/to/dir`. |
| `permission denied` reading `config.yaml`, SELinux `avc: denied` in `ausearch -m avc -ts recent` | The mounted file has no container SELinux label. | Both paths already mount with `:Z` (`ro,Z`). Confirm the label with `ls -Z ~/.config/litellm/config.yaml` (expect `container_file_t`). **Never share a `:Z`-labelled file between two stacks** — `:Z` relabels privately and will break the other consumer; use `:z` if the file genuinely must be shared. |
| `bind: address already in use` on 4000 | Something else holds the port — often a leftover container or the k3s-side deployment on the same host. | `ss -ltnp \| grep :4000` (or `sudo lsof -i :4000`). Free it, or change `LITELLM_PORT` in `.env` (path A) / `PublishPort=` in `litellm.container` (path B). |
| Stack does not come back after a reboot (path B) | Linger is off, so the systemd user manager never starts at boot. | `loginctl enable-linger "$USER"`, confirm with `loginctl show-user "$USER" --property=Linger`. Also confirm the units are wanted by `default.target`: `systemctl --user list-dependencies default.target \| grep -i litellm`. |
| `Unit litellm-postgres.service not found` | The Quadlet generator failed to produce the unit — most often `Notify=healthy` on Podman 4.x, where `Notify=` accepts only `true`/`false`. | Run the generator dry-run ([4.5](#45-validate-the-units-before-starting-anything)) and read stderr. On Podman 4.x, comment out `Notify=healthy`; `litellm-wait-postgres.service` still gates readiness. Then `systemctl --user daemon-reload`. |
| Changes to a unit file have no effect | The generator has not re-run, or you edited the repo copy instead of the installed copy. | Edit `~/.config/containers/systemd/<file>`, then `systemctl --user daemon-reload`, then restart. Re-running `./quadlet/install.sh` does all three. |
| `could not translate host name "litellm-postgres"` | The containers are on the default `podman` network, which has no DNS. | Both paths create a user-defined bridge (`litellm-network` / `litellm-net`) for exactly this reason. Check with `podman inspect litellm --format '{{json .NetworkSettings.Networks}}'` and `podman network ls`. Ensure `litellm-network.service` started. |
| `Start operation timed out` in the journal | A cold image pull exceeded `TimeoutStartSec`. | Pre-pull: `podman pull ghcr.io/berriai/litellm:v1.83.14-stable` and `podman pull docker.io/library/postgres:16-alpine`, then start again. `install.sh` does this unless you pass `--no-pull`. |
| Service stops retrying after a handful of crashes | `StartLimitBurst=5` inside `StartLimitIntervalSec=300` was hit — the systemd equivalent of `CrashLoopBackOff`. | Fix the underlying cause, then `systemctl --user reset-failed litellm.service && systemctl --user start litellm.service`. |
| `--cpus` / `--memory` appear to be ignored (rootless) | The `cpu` / `cpuset` controllers are not delegated to your user slice. | Add the `Delegate=` drop-in from [section 1.2](#12-cgroups-v2) and log out of all sessions. Check what actually applied with `podman inspect --format '{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}}' litellm`. |
| `podman-compose` errors about a required variable | A `${VAR:?message}` guard fired. | Read the message — it names the variable and how to generate a value. Fix `.env` and re-run `podman-compose config >/dev/null` before `up` — keep the redirect, see §3.4. |
| A model deleted in the Admin UI keeps reappearing | It is defined in `config.yaml`, and `model_list` is **combined** with the database, not replaced by it. | Delete or comment the entry in `config.yaml` and restart the proxy ([7.8](#78-adding-or-changing-a-model)). |
| `curl: command not found` inside the LiteLLM container | The image ships Python but not curl. | Use the Python urllib one-liner from [6.4](#64-two-things-not-to-do), or probe from the host. |

### 10.1 General diagnostic sweep

```bash
podman ps -a --filter 'label=io.woowtech.stack=litellm-gw'
podman inspect --format '{{.State.Status}} {{.State.Health.Status}}' litellm litellm-postgres
podman logs --tail 200 litellm
podman logs --tail 200 litellm-postgres
podman network ls
podman volume ls

# Path B only
systemctl --user list-units --all 'litellm*'
systemctl --user status litellm.service --no-pager -l
journalctl --user -u 'litellm*' -b --no-pager | tail -n 200
/usr/lib/systemd/system-generators/podman-system-generator --user --dryrun
```

Attach the output of that sweep (with secrets redacted) to any issue you file.

---

## 11. Uninstall

### 11.1 Path A — compose

```bash
cd Woow_podman_litellm
export COMPOSE_PROJECT_NAME=litellm

# Stop and remove containers + network. THE pgdata VOLUME SURVIVES.
podman-compose down
```

> **`podman-compose down` preserves your data.** Bringing the stack back up with
> `podman-compose up -d` reuses the existing `pgdata` volume, with every virtual
> key, team, user and spend record intact.

To destroy the data as well:

```bash
# Back up first — this is irreversible
./scripts/backup.sh --mode compose

podman-compose down -v          # removes containers, network AND the pgdata volume
```

Clean up anything left behind:

```bash
podman volume ls
podman volume rm litellm_pgdata          # name depends on COMPOSE_PROJECT_NAME
podman network ls
podman network rm litellm_litellm-network
podman rmi ghcr.io/berriai/litellm:v1.83.14-stable docker.io/library/postgres:16-alpine

# Local files
rm -f .env                                # your secrets
rm -rf backups/                           # your backups — be sure
cd .. && rm -rf Woow_podman_litellm
```

### 11.2 Path B — Quadlet

```bash
./quadlet/uninstall.sh
```

The default run is **non-destructive**. It stops the services in order
(`litellm.service` → `litellm-wait-postgres.service` → `litellm-postgres.service` →
`litellm-pgdata-volume.service` → the network unit), removes the four Quadlet unit
files and the plain unit, runs `systemctl --user daemon-reload`, and removes the
`litellm-net` podman network.

It does **not** touch the `litellm-pgdata` volume, anything in `~/.config/litellm/`,
the container images, or linger.

| Flag | Effect |
|---|---|
| `--purge-data` | also delete the `litellm-pgdata` volume — **destroys the database** |
| `--purge-config` | also delete `~/.config/litellm/` (config.yaml **and** litellm.env) |
| `--purge-images` | also remove the LiteLLM and Postgres images |
| `--disable-linger` | also run `loginctl disable-linger` |
| `-y`, `--yes` | skip interactive prompts |
| `-h`, `--help` | usage |

Destructive flags require a typed confirmation: `--purge-data` demands you type
`DELETE-LITELLM-DATA`, `--purge-config` demands `DELETE-LITELLM-CONFIG`. Even with
`--yes`, `--purge-data` still gives you a 10-second abort window. The script prints
a backup hint before destroying anything:

```bash
podman exec litellm-postgres pg_dump -U litellm -d litellm > backup.sql
```

Full removal, after taking a backup you have verified:

```bash
./scripts/backup.sh --mode quadlet --out ~/litellm-final-backup
./quadlet/uninstall.sh --purge-data --purge-config --purge-images --disable-linger
```

Verify afterwards:

```bash
systemctl --user list-units 'litellm*'
podman ps -a --filter 'label=io.woowtech.stack=litellm-gw'
podman volume ls
podman network ls
```

All four should show nothing related to this stack.

### 11.3 Manual removal, if the script is unavailable

```bash
systemctl --user stop litellm.service litellm-wait-postgres.service litellm-postgres.service

rm -f ~/.config/containers/systemd/litellm.container \
      ~/.config/containers/systemd/litellm-postgres.container \
      ~/.config/containers/systemd/litellm-pgdata.volume \
      ~/.config/containers/systemd/litellm.network \
      ~/.config/systemd/user/litellm-wait-postgres.service

systemctl --user daemon-reload

podman rm -f litellm litellm-postgres litellm-wait-postgres 2>/dev/null
podman network rm litellm-net 2>/dev/null

# Destructive — only after a verified backup
podman volume rm litellm-pgdata
rm -rf ~/.config/litellm
loginctl disable-linger "$USER"
```

### 11.4 Reinstalling later

> **Keep `LITELLM_SALT_KEY`.** If you preserved the `litellm-pgdata` volume (or a
> backup archive) but reinstall with a *different* salt key, every provider
> credential in that database becomes permanently undecryptable. Restore the exact
> same value into `litellm.env` / `.env` before starting the stack again.
> `scripts/restore.sh` checks this for you via the manifest fingerprint and refuses
> a confirmed mismatch.

---

## Related repositories

- [`WOOWTECH/Woow_litellm_docker_compose`](https://github.com/WOOWTECH/Woow_litellm_docker_compose)
  — the k3s / docker-compose sibling deployment.
- [`WOOWTECH/Woow_litellm_mcp_server`](https://github.com/WOOWTECH/Woow_litellm_mcp_server)
  — the MCP admin console for managing keys, teams and spend on a running gateway.
