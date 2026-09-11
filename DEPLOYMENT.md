# Deployment Guide

The long-form companion to [`README.md`](README.md). It covers one deployment shape: **rootless
Podman + Quadlet + `systemd --user`**, driven by `scripts/install.sh`. Compose is gone (see
README, "Docker and compose").

**English** · [繁體中文](DEPLOYMENT_zh-TW.md)

> **Never executed against a live Podman host yet.** The units are validated with the podman
> 4.9.3 Quadlet generator and `systemd-analyze --user verify`, the scripts with shellcheck and
> test doubles. Treat a deployment as unverified until `tests/smoke.sh` prints no failures on it.

---

## 1. Prerequisites

### 1.1 Podman

```bash
podman --version                                    # 4.9 or newer; 4.9.3 is what this is tested on
podman info --format '{{.Host.CgroupsVersion}}'     # v2
```

`scripts/install.sh` refuses to run below 4.9. Quadlet itself exists from 4.4, but this repo uses
`PodmanArgs=` (4.6+) and is only validated against the generator shipped with 4.9.3.

### 1.2 What podman 4.9.3 does and does not do

These four facts shape the units; the second and third were wrong in earlier versions of this
repo's documentation.

| Fact | Consequence here |
|---|---|
| `Notify=healthy` is **accepted and ignored** (the generated unit gets `--sdnotify=conmon`) | It is not used at all. The readiness gate is `ExecStartPost=` on the database unit (section 4). |
| `PublishPort=` rejects `${VAR}` (`invalid port format`) | The bind address and port are rendered into the unit at install time from the env file (`@@VAR@@` tokens). |
| Drop-in directories (`x.container.d/*.conf`) are **not applied** | Per-host values cannot be layered; the same rendering answers that. |
| `Memory=` needs 5.5+, `StopTimeout=` is 5.x only, `.pod`/`.build` do not exist | Limits go through `PodmanArgs=--memory/--cpus`; no `StopTimeout=`. |

An unknown key makes the generator **skip the whole file** (no `.service` at all), which is why
`tests/dryrun.sh` runs the real generator and asserts that every source file produced a unit.

### 1.3 A real `systemd --user` session

```bash
systemctl --user is-system-running        # not "offline"; "degraded" is fine
echo "$XDG_RUNTIME_DIR"                   # /run/user/<uid>
```

`su`/`sudo -u` does not give you one: log in over SSH or on the console as the deployment user.
`scripts/install.sh` enables `loginctl enable-linger` for you (it needs a polkit-authorised
administrator once if it cannot do it itself); without linger the units stop at logout and never
start at boot.

### 1.4 Host resources

4 GiB RAM (the proxy is capped at 2 GiB, Postgres at 1 GiB), ~10 GiB free disk (the LiteLLM image
is 1.5-2 GiB), 2 cores, and outbound HTTPS to `ghcr.io`, `docker.io` and `openrouter.ai`.

Rootless hosts do not delegate the `cpu`/`cpuset` controllers to user slices, so `--cpus` can be
silently ineffective; see README, "Prerequisites", for the `delegate.conf` fix. `--memory` works
out of the box.

### 1.5 An OpenRouter account

An API key (`sk-or-...`) from <https://openrouter.ai/keys>. It is the only credential you supply;
everything else is generated.

---

## 2. Secrets: what exists and where it lives

Nothing in this repo, in a unit file or in the journal is a credential. `scripts/install.sh`
creates five podman secrets:

| Secret | Value | Reaches the container as |
|---|---|---|
| `litellm-postgres-password` | 32 random alphanumerics | a **file** at `/run/secrets/postgres-password`, read by `POSTGRES_PASSWORD_FILE` |
| `litellm-database-url` | `postgresql://litellm:<password>@litellm-postgres:5432/litellm` | `DATABASE_URL` (env secret), re-derived on every install |
| `litellm-master-key` | `sk-` + 48 random alphanumerics, or your `LITELLM_MASTER_KEY` | `LITELLM_MASTER_KEY` (env secret) |
| `litellm-salt-key` | `sk-` + 48 random alphanumerics, or your `LITELLM_SALT_KEY` | `LITELLM_SALT_KEY` (env secret) |
| `litellm-openrouter-api-key` | your `OPENROUTER_API_KEY` | `OPENROUTER_API_KEY` (env secret) |

The values you supply travel from `~/.config/litellm/litellm.env` (mode 0600) into the secrets and
are never rendered into a unit. After the first successful install you may blank them in that file;
the secrets stay. `config/config.yaml` never holds a secret: it dereferences
`os.environ/OPENROUTER_API_KEY`, `os.environ/LITELLM_MASTER_KEY` and `os.environ/DATABASE_URL`,
which is why the same file works on k3s, and here.

**Two rules that have no workaround.**

1. `LITELLM_SALT_KEY` encrypts every provider credential LiteLLM stores in Postgres. Replacing it
   does not re-encrypt anything: the rows become permanently unreadable, and LiteLLM does not fail
   loudly (decryption errors are non-blocking; models just stop working and the log asks *"Did your
   master_key/salt key change recently?"*). `install.sh` refuses a value that differs from the
   existing secret; `rotate-secrets.sh --salt` refuses outright; `backup.sh` writes it into
   `secrets.env` so you can keep a copy off the host.
2. On podman 4.9.3 an env secret **is** visible in `podman inspect` of the *running* container.
   The four env secrets are therefore readable by anything that can use this user's podman socket
   (including an MCP server with an inspect tool). They are still out of git, the units,
   `systemctl --user cat`, the create command and the journal. Only the database password avoids
   this, because Postgres supports `*_FILE`.

---

## 3. Install

```bash
git clone https://github.com/WOOWTECH/Woow_podman_litellm.git
cd Woow_podman_litellm
scripts/install.sh                                  # creates the env file, then stops: no key yet
${EDITOR:-vi} ~/.config/litellm/litellm.env         # set OPENROUTER_API_KEY
scripts/install.sh --port 4000                      # --bind / --set are the other knobs
```

### 3.1 What it does, in order

1. **Preflight**: not root, podman >= 4.9, the Quadlet generator and the `podman-user-generator`
   link exist, `systemctl --user` answers, cgroup v2; enables linger; takes a per-app lock.
2. **Settings**: creates the 0600 env file from the example if needed, applies `--port/--bind/--set`
   to a private copy.
3. **Validation of the values**: the bind must be an IPv4 address (a non-loopback one is warned
   about), the port a TCP port, the log level one of the five, the master key `sk-`-prefixed. A
   `pgdata` volume that exists without its password secret, or without its salt secret, aborts.
4. **Guards**: a container named `litellm` or `litellm-postgres` that Quadlet does not manage
   aborts with the `podman rename` command to keep it for rollback (Quadlet starts containers with
   `podman run --replace`, which would delete it). A port already in use aborts.
5. **Render + validate**: the units are rendered from the env file into a temporary directory and
   checked with `quadlet -dryrun -user` plus `systemd-analyze --user verify`, and each generated
   unit name is checked for a shadowing file in `~/.config/systemd/user`. Nothing has been written
   yet at this point.
6. **Save**: only now are new `--port/--bind/--set` values written to the env file.
7. **Images**: both pinned images are pulled before any unit changes, so a slow pull never counts
   against `TimeoutStartSec`.
8. **Secrets**: the missing ones are created; `DATABASE_URL` is re-derived.
9. **Install and apply**: only files whose bytes changed are written (atomically, with a backup of
   anything locally modified), the manifest is updated, `daemon-reload` runs, and only the units
   whose files changed are restarted. Unchanged re-runs restart nothing.
10. **Health and smoke**: waits for `litellm-postgres` to be healthy (up to 5 minutes), then for
    `litellm` (up to 15 minutes: the first boot runs the Prisma migrations), then for
    `/health/readiness`, then runs `tests/smoke.sh`.

`--dry-run` stops after step 6 and writes nothing (it also works with no env file: it renders the
example). `--no-pull` refuses to pull. `--no-start` installs the files and stops.

### 3.2 Where things land

```
~/.config/containers/systemd/   litellm.container  litellm-postgres.container
                                litellm.network    litellm-pgdata.volume
~/.config/litellm/              litellm.env (0600)  config.yaml (0644)
~/.local/state/woow-quadlet/litellm/   manifest, pending-restart, secrets, rollback/
~/backups/litellm/<timestamp>/  backups (0700, files 0600)
```

### 3.3 Generated unit names

Quadlet derives the unit name from the **file name**, not from `ContainerName=`:

| File | Unit | Resource |
|---|---|---|
| `litellm.container` | `litellm.service` | container `litellm` |
| `litellm-postgres.container` | `litellm-postgres.service` | container `litellm-postgres` |
| `litellm.network` | `litellm-network.service` | network `litellm-net` (from `NetworkName=`) |
| `litellm-pgdata.volume` | `litellm-pgdata-volume.service` | volume `litellm-pgdata` (from `VolumeName=`) |

`Network=litellm.network` and `Volume=litellm-pgdata.volume:` reference the **files**, which is
what makes Quadlet inject `Requires=`/`After=` on those services. Referencing a `.volume` file that
is not installed silently becomes a fresh `systemd-<name>` volume on 4.9.3 — `tests/dryrun.sh`
fails on that.

```bash
systemctl --user list-units 'litellm*'
QUADLET_UNIT_DIRS=~/.config/containers/systemd /usr/libexec/podman/quadlet -dryrun -user | head -40
```

---

## 4. The readiness gate

`litellm-postgres.container` ends with:

```ini
ExecStartPost=/usr/bin/timeout 240 /bin/sh -c 'until podman exec litellm-postgres pg_isready -h 127.0.0.1 -p 5432 -U litellm -d litellm >/dev/null 2>&1; do sleep 2; done'
```

systemd keeps a unit in `activating (start-post)` until `ExecStartPost=` finishes, so
`litellm.service`'s `Requires=`/`After=litellm-postgres.service` really waits for a database that
accepts TCP connections — the thing `Notify=healthy` was supposed to do and does not, on 4.9.3.

* **Why `-h 127.0.0.1`**: during first-boot init the Postgres entrypoint runs a temporary server on
  the unix socket only. A socket probe reports "ready" while the real server is not up yet.
* **Failure semantics**: if Postgres is not ready within 240 s the unit fails and `Restart=always`
  retries it. LiteLLM stays down instead of crash-looping against a missing database. A *runtime*
  Postgres crash does not stop LiteLLM (`Requires=` only propagates explicit stop/restart); Prisma
  reconnects.
* `HealthStartPeriod=60s` on Postgres exists so `HealthOnFailure=kill` cannot kill a slow first
  `initdb` on a small host.

---

## 5. Verification

```bash
tests/smoke.sh
```

It checks: both units active, both containers healthy, `/health/liveliness` and
`/health/readiness` 200, `/v1/models` with the master key listing exactly the models in
`config/config.yaml`, `/v1/models` without a key 401, the listener bound only to the configured
address, the Postgres environment free of the proxy's keys, and no plaintext credential in
`systemctl --user cat`. Manual equivalents:

```bash
podman inspect --format '{{.State.Health.Status}}' litellm litellm-postgres
podman healthcheck run litellm                     # force a check now instead of waiting
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4000/health/readiness
podman exec litellm-postgres psql -U litellm -d litellm -c '\dt' | head
journalctl --user -u litellm.service -u litellm-postgres.service -o short-precise | tail -40
```

An end-to-end completion costs a fraction of a cent and is the only thing that proves the
OpenRouter key: see README, "First requests".

**Two things not to do**: never probe plain `/health` (it calls every configured model upstream on
every poll), and never point a healthcheck at `curl` inside the LiteLLM container (the image ships
Python, not curl).

---

## 6. Day-2 operations

| Task | Command |
|---|---|
| Status | `systemctl --user list-units 'litellm*'` |
| Logs | `journalctl --user -u litellm.service -f` (add `-u litellm-postgres.service`) |
| Restart the proxy | `systemctl --user restart litellm.service` |
| Stop (keeps data) | `systemctl --user stop litellm.service litellm-postgres.service` |
| Start again | `systemctl --user start litellm.service` (pulls Postgres up first) |
| Change a setting | edit `~/.config/litellm/litellm.env`, then `scripts/install.sh` |
| Change the model list | edit `config/config.yaml`, then `scripts/install.sh` |
| Upgrade | `git pull && scripts/upgrade.sh` |
| Backup / restore | `scripts/backup.sh` / `scripts/restore.sh <dir>` |
| Rotate | `scripts/rotate-secrets.sh --db` / `--master` |
| Uninstall | `scripts/uninstall.sh` (`--purge` to delete data) |

Never edit the installed unit files: the next `scripts/install.sh` backs up the local change and
overwrites it. Change the env file or the repo instead.

### 6.1 Upgrading

The repo is the source of truth for both image versions. Bump `Image=` in the unit (and the README
badge), commit, then:

```bash
scripts/upgrade.sh
```

It snapshots the installed units into `~/.local/state/woow-quadlet/litellm/rollback/<ts>/`, takes
a `pg_dump` first (LiteLLM's Prisma migrations are one-way and run on the new version's first
boot), installs, and runs the smoke test. On failure it restores the snapshot, restarts on the
previous images — `install.sh` never deletes an image tag, so they are still there — re-runs the
smoke test and prints the `restore.sh` command for the pre-upgrade dump.

`TimeoutStartSec=600` covers a migration on a slow host; the startup healthcheck allows 10 minutes.

### 6.2 Old images

`podman images | grep -E 'litellm|postgres'`, then `podman rmi <old tag>` once you are confident.
`scripts/uninstall.sh --purge-images` removes only images that no container and no other installed
Quadlet unit uses (the Postgres image is shared with other WOOWTECH stacks).

### 6.3 Resource limits

`PodmanArgs=--memory=2g --cpus=2.0` (proxy) and `--memory=1g --cpus=1.0` (database) are in the
units. LiteLLM's own production guidance treats ~4 GiB per worker as a floor; 2 GiB is k3s parity.
If the proxy is OOM-killed under load, raise it there and re-run `scripts/install.sh`.

---

## 7. External access

Nothing in this repo publishes the gateway. Pick one:

* **Reverse proxy** in front of `127.0.0.1:4000` (nginx, Caddy). Turn response buffering **off**
  (`proxy_buffering off;`) or streaming responses arrive in one lump. Terminate TLS there.
* **Overlay network** (Tailscale, WireGuard): `scripts/install.sh --bind <overlay IP>`.
* **Trusted LAN port**: `scripts/install.sh --bind <LAN IP>`, plus a host firewall rule. Everything
  on that LAN can then reach `/ui` and `/v1`, protected only by the keys.

**Never reuse the k3s Cloudflare tunnel token.** A second connector with the same token becomes
another endpoint of that tunnel and Cloudflare load-balances live traffic across both. Create a new
tunnel, hostname and token.

---

## 8. Rootless vs rootful

Everything above is rootless, which is the supported shape: the containers run as your user, the
socket and the data live under `$HOME`, and nothing needs a root daemon.

**Rootful appendix.** If you must run this as root (system-wide units), remember that `%h` is
`/root` there, so nothing that resolves under `%h` is portable between the two. The unit's
config mount and the env file are the only `%h` paths:

```ini
# in a rootful copy of litellm.container
Volume=/etc/litellm/config.yaml:/app/config.yaml:ro,Z
```

Install the files in `/etc/containers/systemd/`, put `config.yaml` in `/etc/litellm/` (0644) and
create the five secrets as root (`sudo podman secret create ...`). `scripts/install.sh` does not
support this: it refuses to run as root by design. Ports below 1024 need rootful or a
`net.ipv4.ip_unprivileged_port_start` change; port 4000 does not.

---

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `converting "x.container": invalid port format '${P}'` | `PublishPort=` with a variable. The repo renders real values; you edited an installed unit. Re-run `scripts/install.sh`. |
| `converting "x.container": unsupported key 'K'` | Your podman does not know the key, so the **entire unit is skipped**. Run `tests/dryrun.sh`. |
| `Error: secret litellm-... not found` when a unit starts | The secret was deleted. `scripts/install.sh` recreates everything except the database password. |
| The volume exists but the password secret does not | The role's password is unknown. Create any value as that secret, run `scripts/install.sh` (the proxy will fail to connect), then `scripts/rotate-secrets.sh --db`, which resets the role over the container's local socket. |
| The proxy never becomes healthy; logs mention `relation does not exist` | The schema was not created. `DISABLE_SCHEMA_UPDATE` must stay `false` (one proxy here, no migration job). |
| Models stop working; the log asks about the master/salt key | The salt key changed. Restore the old one from your backup's `secrets.env`. |
| `pg_isready` in `ExecStartPost` never succeeds | Read `podman logs litellm-postgres`. A wrong `PGDATA` ownership or a half-initialised volume shows there. |
| Units vanish after a reboot | Linger: `loginctl show-user "$USER" --property=Linger` must be `yes`. |
| `ql_check_unit_shadow` aborts the install | A file in `~/.config/systemd/user/` has the same name as a generated unit and outranks it. Move it away. |
| A `podman run --replace` deleted a container you cared about | That is why the collision guard exists; restore from `~/backups/litellm/`. |

---

## 10. Uninstall

```bash
scripts/uninstall.sh                 # units and containers gone; volume, network, secrets, images, env kept
scripts/uninstall.sh --purge         # + volume, network and secrets, after a final pg_dump + secrets.env
scripts/uninstall.sh --purge-images  # + the two images, if nothing else uses them
rm -rf ~/.config/litellm             # the env file is never deleted by the scripts
```

A plain uninstall followed by `scripts/install.sh` adopts the same volume and secrets, so the
gateway comes back with its keys, teams and spend intact.
