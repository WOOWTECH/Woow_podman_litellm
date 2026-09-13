# Architecture

`WOOWTECH / Woow_podman_litellm` runs the same LiteLLM gateway that the existing
k3s deployment runs, but on a single host under Podman. Two containers, one
user-defined bridge network, one named volume for the database, one read-only
config file, and exactly one port reachable from outside the stack.

This document describes **what the files in this repository actually declare** —
every container name, volume name, network name, port and path below was taken
from `config/config.yaml` and the units in `quadlet/`, not from a template.
Nothing here has been executed against a live Podman host yet, so the behaviour
described is documented behaviour, not measured behaviour.

One deployment path ships in this repo: **Quadlet units under `systemd --user`**,
installed by `scripts/install.sh`. The per-host values (publish address and port,
log level) are rendered into the units at install time from
`~/.config/litellm/litellm.env`; the defaults are `127.0.0.1:4000`, the network is
`litellm-net` and the volume is `litellm-pgdata`. The compose path that used to
ship alongside it was removed (README, "Docker and compose"); its last commit is
tagged `compose-final`.

Sister repositories:

- k3s / Docker Compose version — https://github.com/WOOWTECH/Woow_litellm_docker_compose
- MCP admin console — https://github.com/WOOWTECH/Woow_litellm_mcp_server

---

## 1. Runtime topology

```mermaid
flowchart TB
    client["API clients<br/>OpenAI-compatible SDKs, MCP admin console"]

    subgraph host["Podman host"]
        direction TB
        pub["Published port, default 127.0.0.1:4000<br/>rendered from litellm.env"]
        cfg["Host file HOME/.config/litellm/config.yaml"]
        envf["Host env file, mode 0600<br/>HOME/.config/litellm/litellm.env<br/>(install-time input only)"]
        vol[("Named volume litellm-pgdata")]

        subgraph net["Podman bridge network litellm-net"]
            direction TB
            proxy["Container litellm<br/>ghcr.io/berriai/litellm v1.83.14-stable<br/>listens on 4000 inside the network"]
            db["Container litellm-postgres<br/>postgres 16-alpine<br/>listens on 5432 inside the network<br/>NO host port published"]
        end
    end

    orouter["OpenRouter<br/>openrouter.ai/api/v1"]
    vendors["Upstream vendors<br/>OpenAI, Anthropic, Z.ai, MiniMax, Meta"]

    client -->|"HTTP with a virtual key"| pub
    pub --> proxy
    cfg -.->|"bind mount read-only at /app/config.yaml"| proxy
    envf -.->|"env file, never baked into an image"| proxy
    envf -.->|"POSTGRES_PASSWORD"| db
    proxy -->|"postgresql 5432 over container-name DNS"| db
    db ---|"PGDATA at /var/lib/postgresql/data/pgdata"| vol
    proxy ==>|"outbound HTTPS 443"| orouter
    orouter ==> vendors
```

Points the diagram is making:

- **Postgres publishes nothing.** Its unit has no `PublishPort=` at all, so the
  database is reachable only from inside the Podman network, by the container name
  `litellm-postgres` (the old `--network-alias=postgres` is gone with compose: the
  connection string uses the container name).
- **A user-defined network is mandatory, not cosmetic.** Podman's default `podman`
  network has no DNS. Container-name resolution — which is what
  `DATABASE_URL=postgresql://litellm:...@litellm-postgres:5432/litellm` depends on —
  comes from `aardvark-dns` on a user-defined bridge.
- **Only one port leaves the stack**, and only one direction of traffic leaves the
  host: outbound HTTPS to `https://openrouter.ai/api/v1`. There is **no
  cloudflared container, no tunnel, and no tunnel token anywhere in this package**
  (see [Security boundary](#7-security-boundary)).

### 1a. Same diagram, plain ASCII

```
                         +---------------------------------+
                         |          API clients            |
                         |  OpenAI-compatible SDKs / MCP   |
                         +----------------+----------------+
                                          |
                            HTTP + virtual key (sk-...)
                                          |
                                          v
  PODMAN HOST  ==========================================================
  |                                                                     |
  |   published port 4000                                               |
  |     default : 127.0.0.1:4000 -> 4000  (rendered from litellm.env)    |
  |     quadlet : 127.0.0.1:4000 -> 4000   (loopback only)              |
  |                              |                                      |
  |   +--------------------------|---------------------------------+    |
  |   | PODMAN BRIDGE NETWORK    |                                  |    |
  |   |   bridge network: litellm-net                                |    |
  |   |                          v                                  |    |
  |   |   +----------------------------------------------------+    |    |
  |   |   |  container: litellm                                |    |    |
  |   |   |  ghcr.io/berriai/litellm:v1.83.14-stable           |    |    |
  |   |   |  listens 4000                                      |    |    |
  |   |   +----------------------------------------------------+    |    |
  |   |            |                              ^                 |    |
  |   |            | postgresql 5432              | ro bind mount   |    |
  |   |            | (container-name DNS)         | /app/config.yaml|    |
  |   |            v                              |                 |    |
  |   |   +-------------------------------+       |                 |    |
  |   |   |  container: litellm-postgres  |       |                 |    |
  |   |   |  postgres:16-alpine           |       |                 |    |
  |   |   |  listens 5432                 |       |                 |    |
  |   |   |  *** NO HOST PORT PUBLISHED ***|      |                 |    |
  |   |   +---------------+---------------+       |                 |    |
  |   +-------------------|-------------------------|---------------+    |
  |                       |                         |                    |
  |                       v                         |                    |
  |          +-------------------------+   +--------+------------------+ |
  |          |  named volume           |   | host file config.yaml     | |
  |          |  volume: litellm-pgdata |   | + podman secrets          | |
  |          |  quadlet: litellm-pgdata|   +---------------------------+ |
  |          |  PGDATA=/var/lib/       |                                 |
  |          |   postgresql/data/pgdata|                                 |
  |          +-------------------------+                                 |
  |                                                                      |
  |                          outbound HTTPS 443 only                     |
  ================================|=======================================
                                  v
                    +-------------------------------+
                    |  https://openrouter.ai/api/v1 |
                    +---------------+---------------+
                                    v
                    OpenAI / Anthropic / Z.ai / MiniMax / Meta
```

---

## 2. Startup and health gating

The proxy must not start before the database can answer authenticated queries.
Both paths enforce that, with different mechanisms but the same shape.

```mermaid
sequenceDiagram
    autonumber
    participant MGR as systemd user manager
    participant VOL as Named volume
    participant PG as litellm-postgres
    participant LLM as litellm
    participant CL as Client

    MGR->>VOL: create the named volume if it does not exist
    MGR->>PG: start postgres 16-alpine with PGDATA under /var/lib/postgresql/data/pgdata
    PG->>VOL: initdb on first boot, then open the data directory
    loop healthcheck every 10s
        MGR->>PG: pg_isready -U litellm -d litellm
        PG-->>MGR: not accepting connections yet
    end
    PG-->>MGR: pg_isready succeeds, container reported healthy
    Note over MGR,PG: THE GATE - litellm-postgres.service stays in activating (start-post)<br/>until its ExecStartPost= loop sees pg_isready succeed over TCP
    MGR->>LLM: start the proxy container
    LLM->>LLM: read /app/config.yaml and resolve os.environ references
    LLM->>PG: schema step - prisma migrate deploy, DISABLE_SCHEMA_UPDATE is false
    PG-->>LLM: tables created on a fresh volume, no-op on an existing one
    LLM->>PG: load virtual keys, teams and stored models
    loop startup probe every 15s, up to 40 attempts
        MGR->>LLM: GET /health/readiness
        LLM-->>MGR: not ready while the database is not connected
    end
    LLM-->>MGR: readiness returns 200, startup phase complete
    loop liveness probe every 20s
        MGR->>LLM: GET /health/liveliness
        LLM-->>MGR: 200, process alive
    end
    CL->>LLM: POST /v1/chat/completions
    LLM-->>CL: 200 with the completion
```

Notes on the gate:

- **The gate is `ExecStartPost=` on `litellm-postgres.service`**: a
  `timeout 240 sh -c 'until podman exec litellm-postgres pg_isready -h 127.0.0.1 ...'`
  loop. systemd keeps the unit in `activating (start-post)` until that returns, so
  `litellm.container`'s `Requires=` + `After=litellm-postgres.service` really waits
  for a database that accepts TCP connections.
- **`Notify=healthy` is not used.** podman 4.9.3 accepts the key and ignores it: the
  generated unit gets `--sdnotify=conmon`, so "started" would mean "the container
  started", not "the database answers". The earlier `litellm-wait-postgres.service`
  oneshot (a throwaway container polling from the same network, pulled in with a soft
  `Wants=`) is gone with it: the same probe now lives where it gates every dependent.
- **`-h 127.0.0.1` on the probe.** During first-boot init the Postgres entrypoint runs
  a temporary server on the unix socket only; a socket probe reports ready too early.
- **Two probe phases.** `HealthStartupCmd`
  (the k8s `startupProbe` analogue) points at `/health/readiness`, which is
  database-aware and covers image extraction plus the first Prisma migration;
  `HealthCmd` (the `livenessProbe` analogue) points at `/health/liveliness`, a pure
  process-alive check with `HealthStartPeriod=120s` behind it.
- **Never probe plain `/health`.** That endpoint requires a valid key *and* fires a
  real request at every configured model — probing it would burn OpenRouter credits
  on every interval.
- **The LiteLLM image ships Python but not `curl`.** Every healthcheck in this repo
  is therefore a `python -c "import urllib.request,sys; ..."` one-liner, not a
  `curl -f`. A curl-based check fails permanently with "executable not found".
- **`DISABLE_SCHEMA_UPDATE` is deliberately `false` here**, unlike the k3s
  Deployment. With `true`, LiteLLM swaps `prisma migrate deploy` for a read-only
  `prisma migrate diff` that only *prints* SQL and creates no tables — against the
  brand-new empty volume this stack starts with, every DB-backed operation would
  then fail with "relation does not exist".

---

## 3. Request path

```mermaid
flowchart LR
    c["Client request<br/>Authorization Bearer virtual key"]
    auth["Auth layer<br/>look the virtual key up in Postgres"]
    deny["Rejected<br/>401 unauthorised or 429 budget exceeded"]
    route["Router<br/>match the public model name against model_list"]
    or["OpenRouter<br/>openrouter.ai/api/v1"]
    vendor["Upstream vendor<br/>OpenAI, Anthropic, Z.ai, MiniMax, Meta"]
    resp["Response assembled and returned"]
    db[("litellm-postgres<br/>spend logs, key usage counters")]

    c -->|"POST /v1/chat/completions"| auth
    auth -->|"key invalid, blocked or over budget"| deny
    auth -->|"key valid"| route
    route -->|"public name gpt-4o-mini resolves to openrouter/openai/gpt-4o-mini"| or
    or --> vendor
    vendor -->|"tokens and usage"| or
    or --> resp
    resp --> c
    resp -.->|"side branch, after the response<br/>spend and request log write"| db
    auth -.->|"key lookup read"| db
```

How the routing decision is actually made:

`config/config.yaml` maps a short public model name onto an OpenRouter slug, and
every entry carries `api_key: os.environ/OPENROUTER_API_KEY` and
`api_base: https://openrouter.ai/api/v1`:

| Public name the client asks for | Resolves to |
|---|---|
| `gpt-4o-mini` | `openrouter/openai/gpt-4o-mini` |
| `glm-4.6` | `openrouter/z-ai/glm-4.6` |
| `minimax-m2` | `openrouter/minimax/minimax-m2` |
| `claude-sonnet-4.5` | `openrouter/anthropic/claude-sonnet-4.5` |
| `llama-3.3-70b` | `openrouter/meta-llama/llama-3.3-70b-instruct` |

Everything upstream of the proxy is a single vendor relationship: the host holds
**one** OpenRouter key, and OpenRouter fans out to the actual model vendors. Client
applications never see that key — they only ever hold a LiteLLM virtual key.

The spend/log write is drawn as a side branch because it is not on the critical
path of the response. `litellm_settings` in `config.yaml` sets `request_timeout: 600`
and `drop_params: true`; the latter silently strips parameters an upstream model
does not support instead of failing the request.

---

## 4. The deployment path

```mermaid
flowchart TB
    subgraph inputs["Inputs - one source of truth"]
        direction TB
        i1["Image ghcr.io/berriai/litellm v1.83.14-stable"]
        i2["Image postgres 16.15-alpine3.24"]
        i3["config/config.yaml - no secrets, only os.environ references"]
        i4["HOME/.config/litellm/litellm.env (0600) - per-host values and the keys"]
    end

    subgraph install["scripts/install.sh"]
        direction TB
        s1["render quadlet/*.container|volume|network<br/>replacing the @@VAR@@ tokens from litellm.env"]
        s2["validate: quadlet -dryrun -user + systemd-analyze --user verify"]
        s3["pull images, create the five podman secrets,<br/>install only changed files, restart only their units"]
    end

    subgraph running["Result"]
        direction TB
        r1["Units litellm-network.service, litellm-pgdata-volume.service,<br/>litellm-postgres.service, litellm.service<br/>network litellm-net, volume litellm-pgdata<br/>port on the configured address only<br/>Restart=always, starts at boot with linger enabled"]
    end

    inputs --> s1 --> s2 --> s3 --> r1
```

The units in `quadlet/` are the source of truth and the installed copies are byte-identical
to the rendered result, so `scripts/install.sh` can tell a local edit from an upgrade and
back up anything it is about to overwrite.

### Quadlet unit-name derivation

Quadlet derives the systemd unit name from the **file name**, not from the
`ContainerName=` / `VolumeName=` / `NetworkName=` value inside the file:

| File in `quadlet/` | Generated systemd unit | Podman resource created |
|---|---|---|
| `litellm.network` | `litellm-network.service` | network `litellm-net` (from `NetworkName=`) |
| `litellm-pgdata.volume` | `litellm-pgdata-volume.service` | volume `litellm-pgdata` |
| `litellm-postgres.container` | `litellm-postgres.service` | container `litellm-postgres` |
| `litellm.container` | `litellm.service` | container `litellm` |
| `litellm-wait-postgres.service` | *(not Quadlet)* plain unit, installed to `~/.config/systemd/user/` | throwaway container `litellm-wait-postgres` |

Two consequences that bite people:

- `systemctl --user enable litellm.service` **does not work** for a Quadlet-generated
  unit. Enablement happens only through the `[Install]` section inside the `.container`
  file. For a rootless user, `loginctl enable-linger $USER` is additionally mandatory,
  or nothing starts until that user logs in.
- Quadlet does **not** emit `Restart=` for `.container` units, so the systemd default
  `Restart=no` applies unless you write it yourself. Both container units here carry a
  hand-written `[Service] Restart=always` with `RestartSec=10`, plus
  `StartLimitIntervalSec=300` / `StartLimitBurst=5` to approximate Kubernetes'
  `CrashLoopBackOff` — a permanently broken config parks the unit in `failed` instead
  of flooding the journal forever.

### A third path that was considered and rejected

`podman kube play` can consume a Kubernetes-flavoured YAML file and would have let
this repo ship something close to the k3s manifests verbatim. It is **not** shipped,
for three reasons:

1. It creates an infra-pod with a shared network namespace, so both containers share
   `localhost` and the same published-port surface — that is a different topology from
   the one documented above, and it silently changes what "Postgres publishes no port"
   means.
2. Its supported subset of the Kubernetes API is partial and moves between Podman
   releases; probes, resource limits and volume semantics do not map cleanly, so the
   file would look like a k8s manifest while behaving differently.
3. It would be a second thing to keep in sync with `config.yaml` and the images, with no
   capability Quadlet lacks.

Anyone who wants it can generate a starting point with `podman generate kube` from a
running stack; this repo does not carry one.

---

## 5. Component reference

| Component | Image | Container name | Listens on | Published to host? | Volume / mount | Healthcheck | Restart policy |
|---|---|---|---|---|---|---|---|
| LiteLLM proxy | `ghcr.io/berriai/litellm:v1.83.14-stable` | `litellm` | `4000/tcp` | **Yes**, at the rendered `LITELLM_BIND:LITELLM_PORT` (default `127.0.0.1:4000`) | `config.yaml` bind-mounted read-only at `/app/config.yaml` (`ro,Z`); no data volume | startup phase `/health/readiness` 15s × 40, then `/health/liveliness` 20s / 10s / 6, start period 120s | `Restart=always`, `RestartSec=10`, `HealthOnFailure=kill` |
| PostgreSQL | `docker.io/library/postgres:16.15-alpine3.24` | `litellm-postgres` | `5432/tcp` | **No** `PublishPort=` at all | named volume at `/var/lib/postgresql/data`, `PGDATA=/var/lib/postgresql/data/pgdata` | `pg_isready -h 127.0.0.1 -U litellm -d litellm`, 10s / 5s / 3, start period 60s, plus the `ExecStartPost=` gate | `Restart=always`, `RestartSec=10`, `HealthOnFailure=kill` |
| Bridge network | n/a | n/a | n/a | n/a | `litellm-net` (driver `bridge`) | n/a | created by `litellm-network.service` |
| Database volume | n/a | n/a | n/a | n/a | `litellm-pgdata` | n/a | survives `systemctl --user stop` and `scripts/uninstall.sh`; only `--purge` (or an explicit `podman volume rm`) destroys it |

Resource limits: Postgres 1 GiB / 1.0 CPU, LiteLLM 2 GiB / 2.0 CPU
(`PodmanArgs=--memory=` / `--cpus=`), matching the
k3s limits exactly. **Rootless caveat:** the `cpu` and `cpuset` cgroup controllers are
not delegated to user slices by default, so `--cpus` can be silently ineffective —
verify with `podman stats` before relying on it.

Operational scripts: `tests/smoke.sh` (read-only checks against a running stack, also
run at the end of every install), `scripts/backup.sh` (a timestamped directory with a
custom-format `pg_dump`, `secrets.env`, a salt fingerprint and copies of `litellm.env`
and `config.yaml`), `scripts/restore.sh` (destructive: drops and recreates the
database, and refuses to proceed on a confirmed `LITELLM_SALT_KEY` fingerprint
mismatch), `scripts/upgrade.sh` (snapshot, dump, install, smoke, automatic rollback)
and `scripts/rotate-secrets.sh`.

---

## 6. Data flow and persistence

There are exactly two places state lives, and they overlap.

### In Postgres (the named volume)

Written by LiteLLM through Prisma, created on first start by the schema step:

- **Virtual keys** — every `sk-...` key minted for a client application, together with
  its team, budget, rate limits, allowed models, expiry and blocked/unblocked state.
- **Spend logs and usage** — per-request rows with model, token counts and computed
  cost; the aggregates behind the Admin UI and `/spend/*` endpoints.
- **Teams, internal users and organisations** — the ownership graph budgets hang off.
- **Models added at run time**, because `store_model_in_db: true` is set in
  `config.yaml` and `STORE_MODEL_IN_DB=True` in the environment. Anything added through
  the Admin UI or `/model/new` lands here, not in `config.yaml`.
- **Encrypted provider credentials** — provider API keys, callback credentials and MCP
  environment values that were entered through the Admin UI. These are stored as
  **ciphertext**, encrypted with a key derived from `LITELLM_SALT_KEY`.

### In `config.yaml` (the read-only bind mount)

- `model_list` — the five public model names and their OpenRouter slugs.
- `general_settings` — `master_key: os.environ/LITELLM_MASTER_KEY`,
  `database_url: os.environ/DATABASE_URL`, `store_model_in_db: true`.
- `litellm_settings` — `drop_params: true`, `request_timeout: 600`.

**`config.yaml` contains no secret values.** It contains `os.environ/NAME` references
that LiteLLM resolves at start-up from the environment. That is precisely why the same
file works unchanged on k3s and under Quadlet — and why this repo mounts
one file rather than maintaining a duplicated copy inside a ConfigMap, which is a
standing hand-sync hazard in the k3s repo.

### Which wins on conflict

With `store_model_in_db: true`, **the database wins for most settings.** LiteLLM loads
`config.yaml` first and then deep-merges the DB-stored configuration over it, so a
value set through the Admin UI overrides the same key in the YAML file and survives
restarts.

**`model_list` is the exception:** the YAML `model_list` and the DB-stored models are
**combined**, not replaced. A model defined in `config.yaml` therefore cannot be
deleted from the Admin UI — it reappears at every restart, because the file is
re-read every time. Delete it from `config.yaml` (and redeploy) if you want it gone.

Practical consequence: after anyone has used the Admin UI, `config.yaml` is no longer
a complete description of the running gateway. The database is. Treat backups
accordingly.

### Persistence lifetimes

| Action | Database volume | Config | Secrets |
|---|---|---|---|
| `systemctl --user restart litellm.service` | kept | re-read from the host file | re-read from the podman secrets |
| `systemctl --user stop ...` | **kept** | untouched | untouched |
| `scripts/install.sh` (any re-run) | kept | rewritten when it changed | created only when missing; the salt key is never replaced |
| `scripts/uninstall.sh` | kept | kept (the env file and `config.yaml` stay) | kept |
| `scripts/uninstall.sh --purge` | **destroyed** after a final `pg_dump` (typed confirmation, or `--yes`) | the env file is kept | **destroyed** |
| `podman volume rm litellm-pgdata` | **destroyed** | untouched | untouched |

---

## 7. Security boundary

### What is exposed

- **The proxy's port only**, bound to `127.0.0.1` by default and therefore unreachable
  from the LAN without a deliberate change (`scripts/install.sh --bind <address>`,
  which warns when the address is not loopback).
- Rootless publishing of port 4000 needs no privilege; only ports below 1024 do.

### What is not exposed

- **PostgreSQL is not reachable from the host or the LAN.** No `ports:`, no
  `PublishPort=`. The only way in is from inside the Podman network — or via
  `podman exec`, which is what `backup.sh` and `restore.sh` use.
- **There is no cloudflared container, no tunnel, and no tunnel token in this
  package**, on purpose.

> **Do not reuse the existing k3s deployment's Cloudflare tunnel token here.**
> A second connector registered with the same token would split inbound production
> traffic between two different backends and could break the existing k3s deployment.
> If you want a tunnel for this stack, create a **new** tunnel with a **new** hostname
> and a **new** token. This package serves no production hostname; external exposure is
> documentation only.

Recommended ways to reach the gateway from elsewhere, in order of preference:
a reverse proxy in front of `127.0.0.1:4000` terminating TLS and adding
authentication; a Tailscale / WireGuard overlay; or, last resort and only on a trusted
network, changing the bind address to `0.0.0.0`.

### Where secrets live at rest

| Secret | Where it lives at rest |
|---|---|
| `OPENROUTER_API_KEY`, `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY` | podman secrets, created by `scripts/install.sh` from `~/.config/litellm/litellm.env` (0600, outside the git tree). You may blank the env file afterwards |
| the database password | the podman secret `litellm-postgres-password`; Postgres reads it as a **file**, so it is not in `podman inspect` |
| `DATABASE_URL` | the podman secret `litellm-database-url`, derived from that password on every install |
| Provider credentials entered in the Admin UI | encrypted in Postgres, inside the named volume (with the salt key) |
| Virtual keys issued to clients | hashed/stored in Postgres |

Rules this repo enforces:

- **No real credential is ever written into any tracked file.** The template carries
  placeholders only (`REPLACE_ME`, `os.environ/...`). `.gitignore` blocks `.env`,
  `.env.*`, `*.env` (except `*.env.example` and the dry-run fixtures), `secrets/`,
  `*.key`, `*.pem`; CI greps for credential-shaped strings on every push.
- `scripts/install.sh` refuses to install without an OpenRouter key, and every unit
  it renders is checked for unresolved tokens before anything is written, so the
  stack cannot come up with an empty master key.
- **A `pg_dump` of this database contains every virtual key row and every encrypted
  provider credential.** `.gitignore` therefore blocks `backups/`, `*.sql`, `*.sql.gz`,
  `*.dump`, `*.tar`, `*.tar.gz`, `*.tgz`, `*.zip`. Treat a dump as a secret in its own
  right and store it where you would store a password vault export.
- `scripts/backup.sh` writes `secrets.env` (salt and master key, 0600) **next to** the
  dump, because a dump without its salt key is unrestorable. Treat the whole backup
  directory as a credential and keep it off this host.

### The `LITELLM_SALT_KEY` warning

> ### SET IT ONCE. NEVER ROTATE IT.
>
> `LITELLM_SALT_KEY` derives the key that encrypts **every provider credential LiteLLM
> stores in Postgres**. Change it — or lose it — and every credential already in the
> database becomes **permanently undecryptable**. There is no recovery procedure.
>
> LiteLLM does not crash when this happens. It logs a decryption error and returns
> `None`, so the damage is silent and can go unnoticed for a long time.
>
> - Generate it once, at install time, and store it in a password manager or secrets
>   vault — somewhere durable, and somewhere that is **not** this git repository and
>   **not** a backup archive.
> - Back it up **separately** from the database dump. A dump without its matching salt
>   key is useless: it is nothing but ciphertext.
> - `scripts/restore.sh` compares the archive's `salt.fingerprint` against the target
>   stack's current key and **refuses to run** on a confirmed mismatch, unless you pass
>   `--with-secrets`, which also installs the archive's salt key. `install.sh` refuses a
>   salt key in the env file that differs from the existing secret, and
>   `rotate-secrets.sh --salt` refuses outright.
> - If `LITELLM_SALT_KEY` is left unset, LiteLLM silently falls back to
>   `LITELLM_MASTER_KEY`. That means rotating the master key would then also destroy
>   every stored credential. Always set both, explicitly and separately.

### Other boundaries worth stating

- The container filesystems are ephemeral; the only persistent state is the named
  volume. Optional hardening keys (`NoNewPrivileges`, `DropCapability=ALL`,
  `ReadOnly=true`) are present but commented out in `litellm.container` because they
  were never live-tested — enable them one at a time and check the logs after each.
- The `:Z` SELinux flag on the config bind mount applies a **private** label. It is
  correct for the Quadlet path because that copy of `config.yaml` belongs to this stack
  alone. If you ever point both deployment paths at the same host file, switch to `:z`
  (shared) or the second consumer will lose access. On non-SELinux hosts the flag is
  ignored.
- Nothing in this stack was executed against a live Podman host while it was written.
  Verify each step in your own environment before putting it in front of anything that
  matters.
