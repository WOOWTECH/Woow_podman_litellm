# From k3s to Podman: How This Stack Was Translated

**Repository:** `WOOWTECH/Woow_podman_litellm`
**Subject:** deploying the WOOWTECH LiteLLM gateway on a single host with Podman, derived from the k3s manifests that run the same stack today.

This is the analysis document. It explains *why* the repo is shaped the way it is: which Podman deployment models were considered, what each one can and cannot express, exactly how every Kubernetes construct in the source manifests was mapped, where the mapping breaks down, and where the Podman version deliberately diverges from a literal translation because a literal translation would have been wrong.

Nothing in this document was validated against a live Podman host. Every behavioural claim is either read directly out of the files in this repository, read directly out of the k3s manifests, or attributed to documented Podman/systemd/LiteLLM behaviour. Where the research was uncertain, the uncertainty is stated rather than smoothed over.

---

## 1. Why this repo exists

### 1.1 The thing being deployed

The workload is small and completely conventional: a **LiteLLM proxy** (`ghcr.io/berriai/litellm:v1.83.14-stable`) that presents an OpenAI-compatible API in front of five OpenRouter-backed models, plus a **PostgreSQL 16** database (`postgres:16-alpine`) that stores virtual keys, budgets, spend logs and encrypted provider credentials. Two containers, one config file, one network, one persistent volume. That is the entire stack.

The model list, taken verbatim from `config/config.yaml`, is `gpt-4o-mini`, `glm-4.6`, `minimax-m2`, `claude-sonnet-4.5` and `llama-3.3-70b`, each pointing at an `openrouter/...` upstream with `api_base: https://openrouter.ai/api/v1` and `api_key: os.environ/OPENROUTER_API_KEY`. `general_settings` sets `master_key`, `database_url` and `store_model_in_db: true`; `litellm_settings` sets `drop_params: true` and `request_timeout: 600`.

### 1.2 Relationship to `Woow_litellm_docker_compose`

The sibling repository `Woow_litellm_docker_compose` is the origin of this one. It holds two things:

- **the k3s manifests** — `k8s/00-namespace.yaml` through `k8s/05-cloudflared.yaml`, which are what actually runs the production gateway today; and
- **a plain-docker compose file**, an earlier and simpler expression of the same stack.

That repository is the source of truth for the *application*: the model list, the environment contract, the image tags, the health endpoints. This repository is the source of truth for *one particular way of running it* — on a single machine, with Podman, with no cluster underneath.

The relationship is deliberately one-directional. `config/config.yaml` here is a byte-for-byte copy of the config that the k3s side embeds. Nothing in this repo attempts to manage, mutate or take over the k3s deployment, and nothing here should ever be pointed at the live gateway's public hostname. The k3s deployment is referred to throughout only as "the existing k3s deployment".

### 1.3 What this repo is for

There are three honest reasons to want a Podman version of a stack that already runs on Kubernetes:

1. **The target does not have a cluster and should not get one.** A homelab box, an edge appliance, a developer workstation, a small VPS. Installing k3s to run two containers is a poor trade: you take on an API server, etcd/SQLite, a scheduler, a CNI, a CSI provisioner and a kubelet, all to schedule a workload that will never move.
2. **Rootless daemonless containers are a real security improvement.** Podman has no root daemon. A rootless Podman container that escapes its namespace lands in an unprivileged user account, not on a root-owned socket.
3. **systemd is already the init system.** On a single node the thing that ought to supervise your containers, restart them on failure and start them at boot already exists. Kubernetes reimplements that; Quadlet just uses it.

There is a fourth, smaller reason that turned out to matter: **the k3s manifests carry a duplication hazard that the Podman version removes.** `k8s/03-litellm-config.yaml` is a ConfigMap containing a complete copy of `config/config.yaml`, prefixed with a comment warning `KEEP THIS IN SYNC with config/config.yaml`. Any comment of that form is a bug waiting to be filed. Both Podman paths in this repo mount the real `config/config.yaml` file directly, so there is exactly one copy and it cannot drift.

### 1.4 What this repo deliberately does not ship

- **No cloudflared.** `k8s/05-cloudflared.yaml` runs a `cloudflare/cloudflared:latest` connector using a `TUNNEL_TOKEN` from the `cloudflared-token` Secret, and its own header states that it **reuses the existing tunnel**. A Cloudflare tunnel token identifies a tunnel, not a connector; starting a second connector with the same token adds another origin to the *same* tunnel, and Cloudflare will then load-balance across both. Half of the production traffic would arrive at a homelab box. This repo therefore ships no cloudflared container, no service, and no token field anywhere. External access is documentation only — reverse proxy, Tailscale, or a deliberate port exposure — with an explicit warning never to reuse the k3s tunnel token.
- **No `podman kube play` manifest.** Section 2 explains why at length.
- **No claim of live verification.** Nothing here was executed against a real Podman host. Where a construct depends on a Podman version boundary, the boundary is named so you can check it yourself.

---

## 2. The three candidate Podman approaches

Podman offers three genuinely different ways to run a multi-container stack. They are not variants of each other; they have different lifecycle owners, different config languages and different failure modes.

### 2.1 podman-compose (and `podman compose`)

**How it works.** `podman-compose` is a Python program that parses a Compose file and translates each service into `podman` CLI invocations. It is not a daemon and it does not implement the Compose spec in a container engine — it is a translator. Separately, Podman 4.7+ ships a `podman compose` subcommand which is a thin shim that *delegates to whichever external Compose provider is installed*, typically Docker Compose v2 talking to the Podman socket. The two are different programs with different fidelity; this repo's `docker-compose.yml` is written to work with either.

**What it gives you.**

- *Zero translation cost from the existing artefact.* The sibling repo already has a compose file. Anyone in the organisation can read this one.
- *Portainer deployability.* Portainer's "Stacks" feature consumes a Compose file at the repository root and nothing else. This is why `docker-compose.yml` lives at the root and has no top-level `name:` key — Portainer derives the project name itself.
- *Consistency with the four sibling WOOWTECH repos*, which all deploy the same way.
- *Real, expressive health gating.* `depends_on: postgres: condition: service_healthy` is honoured, so the proxy does not start until `pg_isready` passes.
- *Variable interpolation with hard failure.* `${OPENROUTER_API_KEY:?...}` refuses to start the stack when a required secret is missing, rather than starting a broken container. Compose does this; systemd unit files cannot.

**What it costs.**

- *An extra dependency that is not part of Podman.* `podman-compose` is a separately packaged Python project with its own release cadence and its own bug list. `podman compose` needs a Docker Compose binary present. Either way something outside Podman must be installed and kept current.
- *Fidelity gaps.* `condition: service_healthy` requires `podman wait --condition=healthy`, which means **Podman ≥ 4.6 and podman-compose ≥ 1.3**. On older combinations `depends_on` silently degrades to start-order-only, which reintroduces exactly the race the k3s init container existed to prevent.
- *No boot integration by itself.* Nothing starts a compose stack when the machine boots unless you add a systemd unit that shells out to `podman-compose up`, which is a supervision layer wrapping a translation layer wrapping a container engine.
- *Restart policy lives in the wrong place.* Compose's `restart:` is implemented by the container engine's restart policy, which is not the same thing as systemd supervision and does not compose well with it.

**What it cannot do.** It cannot express systemd ordering against non-container units, cannot participate in `systemctl` dependency graphs, and cannot give you a per-container journal identity or a per-container `systemctl status`.

### 2.2 Quadlet systemd units

**How it works.** Quadlet is *part of Podman*. It is a systemd generator: at every `daemon-reload`, `/usr/lib/systemd/system-generators/podman-system-generator` scans `~/.config/containers/systemd/` (rootless) or `/etc/containers/systemd/` (rootful) for `.container`, `.volume`, `.network`, `.pod`, `.kube`, `.build`, `.image` and `.artifact` files and writes real, transient `.service` units into the generator output directory. systemd then supervises the containers as ordinary services.

**The naming rule matters and is easy to get wrong.** The generated service name derives from the unit **filename**, plus a per-type suffix:

| Quadlet file | Generated systemd unit |
|---|---|
| `foo.container` | `foo.service` (no suffix) |
| `foo.volume` | `foo-volume.service` |
| `foo.network` | `foo-network.service` |
| `foo.pod` | `foo-pod.service` |

`ContainerName=`, `VolumeName=` and `NetworkName=` change the *Podman resource* name only; they do not change the unit name. This is verified against Podman's `pkg/systemd/quadlet/quadlet.go` (`getServiceName()`). Applied to this repo: `quadlet/litellm.network` sets `NetworkName=litellm-net`, so the Podman network is `litellm-net` but the systemd unit is **`litellm-network.service`**.

> **Do not derive the network unit name from `NetworkName=`.** It is an easy mistake to make and systemd will not warn you about it: an `After=` on a unit that does not exist is silently ignored (the ordering is simply lost, nothing errors), and a `systemctl --user start` of a wrong name just fails with `Unit not found`. **Confirm the generated names on the target host** with the dry-run in the next paragraph, which prints the units that will actually be generated.

**What it gives you.**

- *No extra dependency at all.* If you have a recent Podman, you have Quadlet.
- *Real systemd supervision.* `systemctl --user status litellm`, `journalctl --user -u litellm`, `Restart=`, `RestartSec=`, `StartLimitIntervalSec=`/`StartLimitBurst=`, `TimeoutStartSec=`, `TimeoutStopSec=` — all of it is standard and all of it composes with the rest of the machine's units.
- *Boots with the machine, properly.* `[Install] WantedBy=default.target multi-user.target` plus `loginctl enable-linger $USER` for rootless.
- *Automatic dependency injection.* Because `litellm-postgres.container` says `Network=litellm.network` and `Volume=litellm-pgdata.volume:/var/lib/postgresql/data`, Quadlet emits `Requires=` and `After=` on the corresponding generated units by itself. Hand-writing them is unnecessary and error-prone.
- *`Notify=healthy`.* On Podman 5.0+, this makes the generated unit `Type=notify` and withholds the systemd READY notification until the container's healthcheck passes. A downstream unit ordered `After=` it therefore gets a genuine health gate. This is the single most valuable thing Quadlet offers this stack and it is strictly better than what the k3s manifest does (see §5.2).
- *A dry-run validator.* `QUADLET_UNIT_DIRS=<dir> /usr/lib/systemd/system-generators/podman-system-generator --user --dryrun` prints the units that would be generated, without touching the system.

**What it costs.**

- *A new file format to learn*, and one with a nasty failure mode: **an unrecognised key makes Quadlet silently skip the file entirely.** You do not get a parse error; you get `Unit litellm.service not found` at `systemctl start` time. The dry-run above is the only reliable way to catch this.
- *Version sensitivity.* `Notify=healthy` needs Podman 5.0+; `Memory=` as a native `[Container]` key needs 5.5.0+; there is no `[Container]` CPU key at all, so `PodmanArgs=--cpus=` is mandatory. This repo uses `PodmanArgs=--memory=1g` / `--cpus=1.0` for exactly this portability reason.
- *Sharp edges around defaults.* Quadlet does **not** emit `Restart=` for `.container` units, so a unit without an explicit `[Service] Restart=` inherits systemd's default of `Restart=no` and a crashed container simply stays down. Every `.container` in this repo hand-writes `Restart=always` and `RestartSec=10`.
- *You cannot `systemctl enable` a Quadlet unit.* The generated services are transient. Enablement is expressed only through `[Install]`, applied by the generator at daemon-reload.
- *Rootless prerequisites.* cgroup v2 is required. The `cpu` and `cpuset` controllers are not delegated to user slices by default, so `--cpus` can be silently ineffective until you add `Delegate=memory pids cpu cpuset` via `/etc/systemd/system/user@.service.d/delegate.conf`.
- *No variable interpolation with failure semantics.* `HealthCmd=` is written verbatim into the generated `ExecStart=`, where `$POSTGRES_USER` would be expanded *by systemd* against the service environment. This is why `HealthCmd=pg_isready -U litellm -d litellm` hardcodes both names.

**What it cannot do.** It cannot be pasted into Portainer. It has no equivalent of Compose's `${VAR:?message}` guard. And it is per-host configuration, not a portable artefact.

### 2.3 `podman kube play`

**How it works.** `podman kube play` consumes a subset of Kubernetes YAML — `Pod`, `Deployment`, `DaemonSet`, `Job`, `PersistentVolumeClaim`, `ConfigMap`, `Secret` — and creates Podman pods and containers from it. It is the option that looks most attractive from where this repo starts, because the k3s manifests already exist.

**What it gives you.**

- *Near-zero translation effort for the shape of the workload.* Container images, args, env, ports, volume mounts and probes largely carry over.
- *One artefact for two targets.* In principle the same YAML feeds both `kubectl apply` and `podman kube play`.
- *`podman kube generate`* can round-trip existing containers back into YAML, which is genuinely useful for exploration.
- *It can be wired to systemd*, via a Quadlet `.kube` unit.

**What it costs — and this is the decisive part.**

- **It silently ignores fields it does not implement.** This is the core objection. `podman kube play` does not reject a manifest containing constructs it cannot honour; it applies what it understands and moves on. For *these specific manifests* that is a long list: `strategy: RollingUpdate` with `maxUnavailable: 0`/`maxSurge: 1` is meaningless without a controller; `replicas` beyond 1 has no scheduler behind it; `resources.requests` are scheduler hints with no local analogue; `volumeClaimTemplates` and the `local-path` `storageClassName` have no provisioner; the headless `Service` with `clusterIP: None` is not created as anything, because Podman has no Service object. A reader who deploys `05-cloudflared.yaml` through it gets a second tunnel connector with no warning at all. The failure mode is not an error message — it is a deployment that looks like the k3s one and is not.
- **There is no reconciliation.** `podman kube play` is a one-shot imperative apply. There is no controller watching for drift, no `kubectl apply` idempotence in the Kubernetes sense, no self-healing. It is `kubectl apply` with the entire control plane removed — which is precisely the part of `kubectl apply` that gives it its value.
- **Its systemd story is worse than Quadlet's.** A `.kube` Quadlet unit gives you exactly one systemd unit for the whole manifest. You cannot restart just the proxy, you cannot give the database its own `TimeoutStopSec=90` for a clean checkpoint, you cannot give the two containers different `StartLimitBurst` values, and per-container journal identity is muddied. With `.container` units you get one supervised unit per container, which is what a single-node operator actually wants.
- **Secret handling is awkward.** k8s `Secret` objects are base64, not encrypted; feeding them through `podman kube play` means either committing base64 secrets or maintaining a separate out-of-band mechanism. Both Podman paths here use a `0600` env file outside the git tree instead.
- **Everything in one blob.** The k3s manifests are five files for good reasons; a `kube play` version collapses the operational granularity that a single-node deployment benefits from.

**Where it is genuinely the right answer:** when you want a scratch, throwaway local reproduction of a cluster workload for development, and you are willing to accept "approximately the same" as good enough. That is a real and useful use case. It is not this one.

### 2.4 Comparison

| Dimension | podman-compose | Quadlet | `podman kube play` |
|---|---|---|---|
| Ships with Podman | No (external package) | **Yes** | Yes |
| Config language | Compose YAML | systemd unit files | Kubernetes YAML |
| Lifecycle owner | The CLI tool / engine restart policy | **systemd** | One-shot apply (or one `.kube` unit) |
| Starts at boot | Only via a hand-written wrapper unit | **Native (`[Install]` + linger)** | Via a `.kube` unit |
| Per-container `systemctl` / journal | No | **Yes** | No — one unit for the whole manifest |
| Health-gated ordering | `condition: service_healthy` (Podman ≥ 4.6, podman-compose ≥ 1.3) | **`Notify=healthy` (Podman 5.0+)**, or the shipped wait unit | Probe fields partially honoured |
| Fails loudly on a missing secret | **Yes (`${VAR:?msg}`)** | No (env file is read as-is) | No |
| Fails loudly on an unsupported field | Mostly yes | **No — silently skips the whole file** | **No — silently ignores the field** |
| Reconciliation / drift correction | None | None (systemd restarts, does not reconcile) | None |
| Portainer-deployable | **Yes** | No | No |
| Reuses the existing k3s YAML | No | No | **Yes** |
| Familiar to the team | **Yes** | Partly | Yes |
| Resource limits | `mem_limit` / `cpus` | `PodmanArgs=--memory` / `--cpus` | `limits` honoured, `requests` dropped |
| Best at | Portability, team familiarity, Portainer | Production single-node, boot-time, supervision | Throwaway local repro of a cluster workload |

### 2.5 Why this repo ships the first two and not the third

**Compose is shipped because it is the low-friction path.** It matches four sibling repositories, it drops into Portainer, and every operator here can already read it. For a homelab or a developer box it is the right default, and its `${VAR:?}` guards catch the single most common deployment mistake — a missing key — before a container ever starts.

**Quadlet is shipped because it is the correct production answer on a single node.** It is part of Podman, it makes systemd the supervisor rather than bolting a supervisor on top, it survives reboots by design, and `Notify=healthy` gives a health gate that is strictly stronger than the k3s manifest's own `nc -z` init container.

**`podman kube play` is not shipped, and the reason is not that it is a bad tool.** It is a good tool aimed at a different problem. The objection is specific: for *this* manifest set, the fields it would silently drop are the fields that encode the operational intent. `maxUnavailable: 0` is the whole point of the rolling-update strategy. `volumeClaimTemplates` + `local-path` is the whole point of the StatefulSet. The headless Service is what makes `litellm-postgres` resolve. `resources.requests` are what make the workload schedulable. Shipping a file that appears to preserve all of that while preserving none of it would be worse than shipping nothing, because it would look right. Add the absence of reconciliation — which removes the only thing that made the declarative model worth its verbosity — and the weaker systemd integration, and the case closes. If you want the k3s YAML to keep meaning what it says, keep it on k3s. If you want a single-node deployment, write single-node artefacts that are honest about being single-node.

---

## 3. The full k3s → Podman construct mapping

Every construct in `k8s/00-namespace.yaml` through `k8s/05-cloudflared.yaml`. "—" means no analogue exists.

### 3.1 Cluster-level and structural

| Kubernetes construct | podman-compose (`docker-compose.yml`) | Quadlet (`quadlet/`) |
|---|---|---|
| `kind: Namespace` (`litellm`) | Compose project name (Portainer-derived; no top-level `name:` key) | — (no namespace concept; unit filenames and the `litellm-` prefix provide the grouping) |
| Namespace labels `app.kubernetes.io/name`, `app.kubernetes.io/part-of` | `labels:` on services | `Label=app.kubernetes.io/part-of=woow-litellm`, `Label=io.woowtech.stack=litellm-gw` |
| `kind: Deployment` (`litellm`) | `services: litellm:` | `quadlet/litellm.container` → `litellm.service` |
| `kind: StatefulSet` (`litellm-postgres`) | `services: postgres:` (`container_name: litellm-postgres`) | `quadlet/litellm-postgres.container` → `litellm-postgres.service` |
| `replicas: 1` | One container per service (implicit) | One container per unit (implicit) |
| `strategy: RollingUpdate`, `maxUnavailable: 0`, `maxSurge: 1` | — | — |
| `serviceName: litellm-postgres` (StatefulSet governing Service) | `container_name` + network alias | `ContainerName=litellm-postgres` + `PodmanArgs=--network-alias=postgres` |
| `selector` / `matchLabels` / pod template labels | — (no controller to select with; `labels:` are metadata only) | — (`Label=` is metadata only) |
| Pod (shared network namespace) | Not used — two independent containers on one bridge network | Not used — could be a `.pod`, deliberately not, so each container is separately supervisable |
| `kind: Service` type `ClusterIP` (`litellm`, port 4000) | `ports: "${LITELLM_PORT:-4000}:4000"` | `PublishPort=127.0.0.1:4000:4000` |
| `kind: Service` headless (`clusterIP: None`, postgres) | Bridge-network DNS: aliases `[litellm-postgres, postgres]`, `expose: "5432"`, no `ports:` | aardvark-dns on `litellm-net`: `ContainerName=litellm-postgres` + `PodmanArgs=--network-alias=postgres`, no `PublishPort=` |
| Cluster DNS (`kube-dns` / CoreDNS) | netavark + aardvark-dns on a user-defined bridge | netavark + aardvark-dns on `litellm-net` |
| Default `podman` network | n/a — Compose always creates a project network | Not used. The built-in `podman` network provides **no DNS**, so a user-defined network is mandatory for `litellm-postgres` to resolve |
| `kind: Ingress` / external exposure | Not present in the manifests; documentation only | Not present; documentation only |

### 3.2 Configuration and secrets

| Kubernetes construct | podman-compose | Quadlet |
|---|---|---|
| `kind: ConfigMap` (`litellm-config`, embedded copy of config.yaml) | Bind mount `./config/config.yaml:/app/config.yaml:ro,Z` — no copy, no drift | `Volume=%h/.config/litellm/config.yaml:/app/config.yaml:ro,Z` (installed by `install.sh`) |
| ConfigMap `items: [{key: config.yaml, path: config.yaml}]` (single-key projection) | Single-file bind mount achieves the same result | Single-file bind mount achieves the same result |
| `kind: Secret` (Opaque, `litellm-secrets`) | `${VAR:?}` interpolation, resolved on the host from `.env` (CLI) or Portainer's `stack.env`; no `env_file:` is declared | `EnvironmentFile=%h/.config/litellm/litellm.env` (mode `0600`, dir `0700`, outside the git tree) |
| `kind: Secret` (`litellm-postgres-secret`) | Same interpolation source | Same env file (a note in the unit explains how to split into `postgres.env` / `litellm.env`, since `EnvironmentFile=` is repeatable and order-preserving) |
| `kind: Secret` (`cloudflared-token`) | **Deliberately absent** | **Deliberately absent** |
| `envFrom: secretRef` | No direct analogue — each key is listed explicitly in `environment:` and filled by `${VAR:?}` interpolation from `.env` / `stack.env`. `env_file:` is deliberately **not** used, because it would hard-fail Portainer git deploys (no `.env` in the cloned tree) | `EnvironmentFile=` |
| `env:` with literal `value:` | `environment:` map | `Environment=KEY=VALUE` (repeatable) |
| `env:` with `valueFrom.secretKeyRef` | `${VAR}` interpolation from `.env` | — (no per-key secret indirection; the whole env file is loaded) |
| Secret base64 encoding | Plain `KEY=VALUE` | Plain `KEY=VALUE`. **No `${VAR}`, `${VAR:-x}`, `${VAR:?}`, `$(...)`, `export`, or trailing comments** — systemd's `EnvironmentFile` parser is not a shell |
| Missing-value behaviour | **Hard fail** via `${OPENROUTER_API_KEY:?...}` etc. | Silent empty value — the container starts and fails later. This is a real regression; the smoke test exists partly to catch it |

### 3.3 Storage

| Kubernetes construct | podman-compose | Quadlet |
|---|---|---|
| `volumeClaimTemplates` (`pgdata`) | Named volume `pgdata` (`driver: local`) | `quadlet/litellm-pgdata.volume` → `litellm-pgdata-volume.service`, `VolumeName=litellm-pgdata` |
| `accessModes: [ReadWriteOnce]` | — (single host; implicitly RWO) | — |
| `storageClassName: local-path` | `driver: local` | `Driver=local` |
| `resources.requests.storage: 5Gi` | — (no quota; the volume grows into the host filesystem) | — (same) |
| `volumeMounts` → `/var/lib/postgresql/data` | `pgdata:/var/lib/postgresql/data` | `Volume=litellm-pgdata.volume:/var/lib/postgresql/data` |
| ConfigMap volume → `/etc/litellm` (`readOnly: true`) | `./config/config.yaml:/app/config.yaml:ro,Z` | `%h/.config/litellm/config.yaml:/app/config.yaml:ro,Z` |
| `PGDATA=/var/lib/postgresql/data/pgdata` (subdir so the mount root can hold `lost+found`) | Identical `PGDATA` value | Identical `Environment=PGDATA=/var/lib/postgresql/data/pgdata` |
| SELinux relabelling | `:Z` (private label) | `:Z` (private label). `:z` is the shared variant — do not use it on a host-shared directory you care about |
| Volume dependency ordering | `volumes:` top-level block, created before services | Referencing `litellm-pgdata.volume` by **filename** makes Quadlet inject `Requires=`+`After=litellm-pgdata-volume.service` automatically |

### 3.4 Networking

| Kubernetes construct | podman-compose | Quadlet |
|---|---|---|
| Pod network / CNI (flannel) | `networks: litellm-network: driver: bridge` | `quadlet/litellm.network` (`NetworkName=litellm-net`, `Driver=bridge`) → `litellm-network.service` |
| Service DNS name `litellm-postgres` | Network alias `litellm-postgres` | `ContainerName=litellm-postgres` (aardvark-dns resolves the container name) |
| Legacy plain-compose hostname `postgres` | Network alias `postgres` | `PodmanArgs=--network-alias=postgres` (the native `NetworkAlias=` key exists only in Podman 5.2+, and an unknown key makes Quadlet skip the whole unit) |
| `containerPort: 4000` | `ports: "${LITELLM_PORT:-4000}:4000"` (all interfaces; a loopback-only alternative is commented in the file) | `PublishPort=127.0.0.1:4000:4000` (loopback only) |
| Postgres `port: 5432`, ClusterIP-internal only | `expose: ["5432"]`, **no `ports:`** | **No `PublishPort=`** |
| `NetworkPolicy` | — (not present in the manifests, and not expressible) | — |
| Subnet control | Docker/Podman default pool | Commented `#Subnet=10.89.42.0/24` / `#Gateway=10.89.42.1` in `litellm.network` |
| Network dependency ordering | Implicit in the Compose project | Referencing `litellm.network` by **filename** injects `Requires=`+`After=` on the generated network unit automatically |

### 3.5 Container runtime specification

| Kubernetes construct | podman-compose | Quadlet |
|---|---|---|
| `image: ghcr.io/berriai/litellm:v1.83.14-stable` | Same fully-qualified reference | `Image=ghcr.io/berriai/litellm:v1.83.14-stable` |
| `image: postgres:16-alpine` (short name) | Expanded to `docker.io/library/postgres:16-alpine` | `Image=docker.io/library/postgres:16-alpine` — short names depend on `registries.conf` search order and can prompt interactively, which fails in a non-interactive unit |
| `imagePullPolicy: IfNotPresent` | Default `podman` behaviour | Deliberately no `Pull=` key — `podman run` already defaults to `--pull=missing`, which is the same semantics, and omitting the key keeps the file parseable on older Podman |
| Image digest pinning | Not shipped | Commented `#Image=...@sha256:PASTE_DIGEST_HERE` with the two commands to obtain it. No digest is shipped because none was pulled or verified while writing this repo |
| `args: ["--config","/etc/litellm/config.yaml","--port","4000"]` | `command: ["--config","/app/config.yaml","--port","4000"]` | `Exec=--config /app/config.yaml --port 4000` |
| `command:` (ENTRYPOINT override) | Deliberately unset | Deliberately unset (`Entrypoint=` absent). The image ENTRYPOINT is `docker/prod_entrypoint.sh`, ending in `exec litellm "$@"`, so args must **never** be prefixed with the word `litellm` |
| `resources.limits.cpu` | `cpus: 1.0` / `cpus: 2.0` | `PodmanArgs=--cpus=1.0` / `--cpus=2.0` — there is **no `[Container]` CPU key** |
| `resources.limits.memory` | `mem_limit: 1g` / `mem_limit: 2g` | `PodmanArgs=--memory=1g` / `--memory=2g`; the native `Memory=` key exists only from Podman 5.5.0 |
| `resources.requests.*` | — | — (scheduler hints; meaningless on one host) |
| `securityContext` | Not set in the manifests | Not enabled, but `#NoNewPrivileges=true`, `#DropCapability=ALL`, `#AddCapability=CHOWN DAC_OVERRIDE FOWNER SETGID SETUID` are documented and commented out because they were never live-tested |
| Read-only root filesystem | Not set | `#ReadOnly=true` commented, with a note that it needs `LITELLM_MIGRATION_DIR`, `LITELLM_UI_PATH` and `LITELLM_ASSETS_PATH` redirected first |

### 3.6 Health, ordering and lifecycle

| Kubernetes construct | podman-compose | Quadlet |
|---|---|---|
| `initContainer wait-for-postgres` (`busybox:1.36`, `until nc -z litellm-postgres 5432`) | `depends_on: postgres: condition: service_healthy` | **`Notify=healthy`** on `litellm-postgres.container` (Podman 5.0+), plus `quadlet/litellm-wait-postgres.service` as the 4.x fallback |
| Postgres `readinessProbe` (`pg_isready`, 10/10/5) | `healthcheck: ["CMD-SHELL","pg_isready -U ${POSTGRES_USER:-litellm} -d ${POSTGRES_DB:-litellm}"]`, 10s/5s/5/10s | `HealthCmd=pg_isready -U litellm -d litellm`, `HealthInterval=10s`, `HealthTimeout=5s`, `HealthRetries=3`, `HealthStartPeriod=10s` |
| Postgres `livenessProbe` (same command, 30/15/5) | Collapsed into the one healthcheck | Collapsed into the one healthcheck (readiness timings kept, because that is the signal the ordering gate uses) |
| LiteLLM `readinessProbe` → `/health/readiness` (120/15/10/6) | Collapsed into the one healthcheck | **`HealthStartupCmd=`** against `/health/readiness`, `HealthStartupInterval=15s`, `HealthStartupTimeout=10s`, `HealthStartupRetries=40`, `HealthStartupSuccess=1` |
| LiteLLM `livenessProbe` → `/health/liveliness` (120/20/10/6) | `healthcheck:` python-urllib probe of `/health/liveliness`, 20s/10s/6, `start_period: 120s` | `HealthCmd=` python-urllib probe of `/health/liveliness`, `HealthInterval=20s`, `HealthTimeout=10s`, `HealthRetries=6`, `HealthStartPeriod=120s` |
| Probe mechanism (`httpGet`, run by the kubelet from outside) | Executed **inside** the container. The image has Python but **no curl**, hence a `python -c "import urllib.request,sys; ..."` one-liner | Same one-liner, same reason |
| kubelet restart on liveness failure | Engine restart policy | `HealthOnFailure=kill` (the value Podman documents as integrating best with systemd) plus `[Service] Restart=always` |
| `restartPolicy: Always` | `restart:` semantics | `[Service] Restart=always`, `RestartSec=10`. **Quadlet does not emit `Restart=` itself** — without this line the unit inherits `Restart=no` |
| CrashLoopBackOff | — | `StartLimitIntervalSec=300`, `StartLimitBurst=5` → the unit lands in `failed`, a visible and alertable state. The closest available analogue |
| Graceful termination / `terminationGracePeriodSeconds` | Compose stop timeout | `TimeoutStopSec=90` for Postgres (finish a checkpoint rather than be SIGKILLed into crash recovery), `TimeoutStopSec=60` for the proxy |
| Startup budget for a cold image pull | Compose has no equivalent knob | `TimeoutStartSec=300` (postgres) / `600` (litellm) — systemd's 90s default is shorter than a first-run pull on a slow link, and with `Notify=healthy` the unit stays `activating` until the healthcheck passes |
| Scheduling at boot | Wrapper unit required | `[Install] WantedBy=default.target multi-user.target` (`default.target` for rootless, `multi-user.target` for rootful; both listed so one file works in either location) + `loginctl enable-linger $USER` |

### 3.7 Application-level environment

| Kubernetes value | podman-compose | Quadlet |
|---|---|---|
| `STORE_MODEL_IN_DB: "True"` | `environment:` | `Environment=STORE_MODEL_IN_DB=True` |
| `LITELLM_MODE: "PRODUCTION"` | `environment:` | `Environment=LITELLM_MODE=PRODUCTION` |
| `LITELLM_LOG: "INFO"` | `environment:` | `Environment=LITELLM_LOG=INFO` |
| `DISABLE_SCHEMA_UPDATE: "true"` | `${DISABLE_SCHEMA_UPDATE:-false}` — **deliberately inverted**, see §5.1 | `Environment=DISABLE_SCHEMA_UPDATE=false` — same inversion |
| `DATABASE_URL` (from Secret) | `${DATABASE_URL:?}` | From the env file |
| `LITELLM_MASTER_KEY` (from Secret) | `${LITELLM_MASTER_KEY:?}` | From the env file |
| `LITELLM_SALT_KEY` (Secret, commented `SET ONCE, NEVER CHANGE`) | `${LITELLM_SALT_KEY:?}` | From the env file, with the same warning |
| `OPENROUTER_API_KEY` (from Secret) | `${OPENROUTER_API_KEY:?}` | From the env file |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `${POSTGRES_PASSWORD:?...}` etc. | From the env file |

> **`LITELLM_SALT_KEY` — set once, never rotate.** It is the key used to encrypt provider credentials stored in the database. If it is unset, LiteLLM silently falls back to `LITELLM_MASTER_KEY`, which means rotating the master key would then also destroy decryptability. Worse, a decryption failure is **non-blocking**: LiteLLM logs `Did your master_key/salt key change recently?` and returns `None`, so the proxy keeps running while quietly failing to read its own credentials. Rotate it and every credential encrypted in the database becomes permanently undecryptable. This is also why `scripts/backup.sh` warns that a `pg_dump` is ciphertext and useless without the salt key.

### 3.8 Constructs with no counterpart at all

| Kubernetes construct | Status |
|---|---|
| `kind: Deployment` (`cloudflared`), `TUNNEL_TOKEN`, `/ready` probe on port 2000 | **Deliberately omitted.** See §1.4 and §5.5 |
| Horizontal Pod Autoscaler, PodDisruptionBudget, anti-affinity, tolerations | Not in the manifests, and not expressible |
| RBAC, ServiceAccount | Not in the manifests. Podman's analogue is the Unix user the containers run as |
| Metrics-server, `kubectl top` | `podman stats` (subject to the rootless cgroup-delegation caveat) |
| `kubectl rollout undo` | — (redeploy the previous tag by hand) |

---

## 4. What does not map cleanly

Seven things genuinely do not translate. For each: what Kubernetes gives you, what Podman can and cannot do, and what this repo does about it.

### 4.1 Zero-downtime rolling updates

**Kubernetes gives you:** `strategy: RollingUpdate` with `maxUnavailable: 0` and `maxSurge: 1` — start the new pod, wait for its readiness probe, shift Service endpoints, then terminate the old pod. The client sees no gap.

**Podman can:** restart a container quickly. `systemctl --user restart litellm` with `TimeoutStopSec=60` is on the order of seconds.

**Podman cannot:** run two instances behind a virtual IP with health-gated endpoint switching. There is no Service abstraction, no endpoint controller, no readiness-gated cutover. Even if you started a second container, nothing would move traffic to it.

**Mitigation adopted:** none is pretended. Both paths accept a short restart window and say so. The `HealthStartupCmd=` / `start_period: 120s` settings make the restart *reliable* (the unit is not considered up until `/health/readiness` answers), not *invisible*. If zero-downtime updates are a requirement, that is a reason to stay on k3s — see §7.

### 4.2 Declarative reconciliation

**Kubernetes gives you:** a control loop. Delete a pod and the ReplicaSet recreates it. Edit a container by hand and the desired state wins. `kubectl apply` converges the cluster onto the manifest.

**Podman can:** restart a container that exits, via `[Service] Restart=always` and `HealthOnFailure=kill`. That is *supervision*.

**Podman cannot:** notice that a running container no longer matches its unit file, or that someone `podman rm`'d a volume, or that the image tag moved. There is no controller and no desired-state comparison. Editing a `.container` file changes nothing until `systemctl --user daemon-reload` followed by a restart.

**Mitigation adopted:** make the state explicit and checkable instead of self-correcting. `install.sh` and `uninstall.sh` are the apply/delete operations. `scripts/smoke-test.sh` runs seven counted checks (HTTP through the container's own Python, SQL through the postgres container's `psql`) so drift is *detected* even though it is not *corrected*. `StartLimitIntervalSec=`/`StartLimitBurst=` turn a persistently broken container into a `failed` unit that monitoring can see, rather than an infinite silent restart loop.

### 4.3 A real secret store

**Kubernetes gives you:** `kind: Secret` as a first-class object — separately RBAC-able, mountable per-key via `secretKeyRef`, and (with encryption-at-rest configured) not stored in plaintext in etcd. The manifests use three of them.

**Podman can:** read an `EnvironmentFile=`, and separately offers `podman secret` (which Quadlet exposes as `Secret=`).

**Podman cannot:** give you per-key indirection into an env file, encryption at rest without extra machinery, or any access control finer than Unix file permissions. `podman secret` in its default driver stores secrets unencrypted under the user's data directory, so it moves the problem rather than solving it.

**Mitigation adopted:** a single `0600` file in a `0700` directory, owned by the service user, living **outside the git tree** (`~/.config/litellm/litellm.env` for Quadlet; `.env` for compose, with `.gitignore` blocking `.env`, `.env.*`, `*.env`, `secrets/`, `*.key` and `*.pem` while allowing only `*.example` templates). Compose additionally gets `${VAR:?message}` guards that refuse to start on a missing secret; the Quadlet path has no equivalent and is documented as weaker on that specific point. Both paths deliberately avoid publishing the Postgres port, so the encrypted-credential store is not reachable from the host network at all. The env file format rules are strict for a reason — systemd's parser is not a shell, so `${VAR}`, `${VAR:-x}`, `$(...)`, `export` and trailing comments on value lines are all forbidden, and passwords should avoid `$` and `#` entirely (hex is suggested).

### 4.4 The three-probe model

**Kubernetes gives you:** startup, readiness and liveness probes as three independent mechanisms with three different consequences — startup suppresses the other two during boot, readiness controls traffic routing, liveness triggers a restart. The manifests use readiness and liveness on both workloads with different timings.

**Podman can:** run exactly **one** regular healthcheck per container, plus (Quadlet only) a separate *startup* healthcheck via the `HealthStartup*` keys.

**Podman cannot:** distinguish "unhealthy, stop sending traffic" from "unhealthy, restart me", because there is nothing routing traffic in the first place.

**Mitigation adopted:** readiness and liveness are collapsed into one check, and the choice of which timings to keep is deliberate:

- **Postgres** keeps the **readiness** timings (10s/10s/5s → `HealthInterval=10s`, `HealthTimeout=5s`, `HealthStartPeriod=10s`) because readiness is the signal the proxy's ordering gate depends on. The liveness timings (30/15/5) would have been too slow to gate on.
- **LiteLLM** splits them properly on the Quadlet path: `HealthStartupCmd=` polls `/health/readiness` (40 retries × 15s, success after 1) during boot, then the steady-state `HealthCmd=` polls `/health/liveliness` at 20s. That is closer to the Kubernetes semantics than compose can express, and it is why the Quadlet path is the recommended production one.
- Plain `/health` is used by **neither** path. It requires an authenticated key and it fires a real request at every configured model, which burns OpenRouter credits on every poll. `/health/liveliness` (canonical; `/health/liveness` is an alias) is unauthenticated and does not touch the database. `/health/readiness` is unauthenticated and *is* database-aware, returning 503 when the database is unreachable — which makes it correct for startup gating and wrong for steady-state liveness, since a transient database blip should not kill the proxy.

### 4.5 Scheduler-facing resource semantics

**Kubernetes gives you:** `requests` (what the scheduler reserves, and what determines QoS class and eviction order) and `limits` (the cgroup ceiling). The manifests set both: postgres `100m`/`256Mi` → `1`/`1Gi`; litellm `250m`/`512Mi` → `2`/`2Gi`.

**Podman can:** apply the limits, via `--memory` and `--cpus`.

**Podman cannot:** express requests at all — there is no scheduler to reserve anything from — and, when rootless, may not even apply the CPU limit, because the `cpu` and `cpuset` cgroup controllers are not delegated to user slices by default.

**Mitigation adopted:** limits are translated (`mem_limit`/`cpus` in compose; `PodmanArgs=--memory=`/`--cpus=` in Quadlet), requests are dropped with an explicit comment saying why, and the rootless delegation caveat is documented inline in `litellm-postgres.container` together with the fix (`/etc/systemd/system/user@.service.d/delegate.conf` containing `Delegate=memory pids cpu cpuset`, then log out and back in) and the way to verify it (`podman stats`). Memory and pids are delegated by default; cpu and cpuset are not.

### 4.6 Dynamic storage provisioning and identity

**Kubernetes gives you:** `volumeClaimTemplates` with `storageClassName: local-path` and `resources.requests.storage: 5Gi` — a provisioner creates a PV on demand, and StatefulSet identity binds that PV to pod ordinal 0 across rescheduling.

**Podman can:** create a named local volume.

**Podman cannot:** enforce a size quota, provision dynamically, or maintain a stable identity binding — because there is only ever one instance on one host, so identity is trivial and quota is the host filesystem.

**Mitigation adopted:** a named volume in both paths (`pgdata` in compose; `litellm-pgdata` via `quadlet/litellm-pgdata.volume`). The `5Gi` request is dropped — the volume grows into whatever the host filesystem allows, which is a real behavioural difference and is called out here rather than hidden. `PGDATA=/var/lib/postgresql/data/pgdata` is preserved byte-for-byte from the StatefulSet so the on-disk layout stays portable between the two deployments; the reason for the subdirectory is that the postgres entrypoint refuses to `initdb` into a non-empty directory and a mount root can carry `lost+found` or ownership artefacts.

### 4.7 Service discovery as an object

**Kubernetes gives you:** `Service` as a first-class, selector-driven object. The headless Service (`clusterIP: None`) gives `litellm-postgres` a stable DNS name independent of which pod backs it.

**Podman can:** resolve container names and network aliases via aardvark-dns, but **only on a user-defined network**.

**Podman cannot:** decouple the name from the container. The name *is* the container's name; there is no selector, no endpoint list, no indirection.

**Mitigation adopted:** the container is literally named `litellm-postgres` (`container_name:` in compose, `ContainerName=` in Quadlet), so the `DATABASE_URL` — `postgresql://litellm:...@litellm-postgres:5432/litellm` — resolves unchanged from the k3s secret. A second alias `postgres` is added in both paths so a `DATABASE_URL` copied from the older plain-compose file in the sibling repo also works. **The critical detail:** Podman's built-in `podman` network provides no DNS at all. Both paths therefore create a user-defined bridge network (`litellm-network` in compose, `litellm-net` via `quadlet/litellm.network`) and this is not optional — on the default network the proxy cannot find the database.

---

## 5. Where the Podman version deliberately differs from a naive translation

A mechanical translation of the manifests would have produced a working-looking stack with several latent defects. These are the places where this repo departs from the source on purpose, and the reasoning.

### 5.1 `DISABLE_SCHEMA_UPDATE` is inverted

**k3s sets `DISABLE_SCHEMA_UPDATE: "true"`**, with the comment `Proxy pods must not run DB migrations (restart / multi-replica safe)`. That is correct *for Kubernetes*: with a rolling-update strategy, several proxy pods can be alive at once, and concurrent `prisma migrate deploy` runs against one database is a recipe for a corrupted migration table. The k3s deployment's schema is established out of band.

**Both Podman paths set it to `false`** — `${DISABLE_SCHEMA_UPDATE:-false}` in compose, `Environment=DISABLE_SCHEMA_UPDATE=false` in `litellm.container`.

**Why.** On a single node with exactly one proxy container, the concurrency hazard that justified `true` does not exist. And a fresh Podman install starts with an **empty** database — if migrations are disabled, the very first boot has no schema and the proxy fails in a way that is confusing to diagnose. What `DISABLE_SCHEMA_UPDATE=true` actually does in LiteLLM v1.83.14-stable is swap `prisma migrate deploy` for a read-only `prisma migrate diff`, which reports drift and **creates nothing**. Setting it to `true` on a first boot therefore does not "skip an unnecessary step"; it guarantees failure.

**The honest caveat.** This is the single decision in this repo with the most residual uncertainty, and it is a genuine trade rather than a free win. Leaving it at `false` permanently means every container restart re-runs `migrate deploy`. That is idempotent by design and normally a no-op, but it does mean a container restart immediately after an image upgrade will apply schema changes automatically rather than at a moment you chose. Two hardening options are available and are documented rather than imposed:

- flip it to `true` in your `.env` after the first successful boot, accepting that you must then run migrations by hand before any image upgrade; or
- run a separate one-shot migration container using `--skip_server_startup`, which performs the migration and exits without starting the server, and keep the long-running proxy at `true`.

The repo ships `false` because it is the value that makes a clean install work on the first try, and the second-boot behaviour is safe. Neither alternative was live-tested.

### 5.2 Health-gated startup ordering replaces the init container — and improves on it

**k3s uses** an initContainer `wait-for-postgres` running `busybox:1.36` with `until nc -z litellm-postgres 5432; do echo "postgres not ready, sleeping 3s"; sleep 3; done`.

**This repo does not translate that literally**, even though it easily could have. `nc -z` goes green the instant a TCP listener binds — and Postgres binds its socket *during* `initdb`, before it will accept authenticated connections. On a first boot, the k3s init container can therefore pass while the database is still initialising. It is a TCP check standing in for a service check.

**What is used instead, in three layers:**

1. **Compose:** `depends_on: postgres: condition: service_healthy`, gated on the `pg_isready` healthcheck. Requires Podman ≥ 4.6 (for `podman wait --condition=healthy`) and podman-compose ≥ 1.3; on older versions this silently degrades to ordering only, which is exactly the race being avoided. Check your versions.
2. **Quadlet, Podman 5.0+:** `Notify=healthy` on `litellm-postgres.container`. Podman withholds the systemd READY notification until `pg_isready` passes, so `litellm-postgres.service` reports `active` only when the database genuinely answers. `litellm.container`'s `After=litellm-postgres.service` then gets a real health gate for free — no side-car, no extra image, no TCP-only race. This is strictly better than the k3s original.
3. **Quadlet, Podman 4.4–4.9:** `quadlet/litellm-wait-postgres.service`, a plain (non-Quadlet) oneshot unit. It runs a throwaway `postgres:16-alpine` container **on the same Podman network** executing `pg_isready -h litellm-postgres -p 5432 -U litellm -d litellm` in a **bounded** loop (60 attempts × 3s = 180s, then a loud non-zero exit rather than hanging forever), with `TimeoutStartSec=300`, `RemainAfterExit=yes`, and `ExecStartPre=`/`ExecStopPost=` cleanup of a stale container name. Because it reuses an image the stack already pulls, it adds no busybox to pin and patch; and because it goes over the network by hostname, it additionally proves that aardvark-dns resolves the exact name in `DATABASE_URL`. `litellm.container` pulls it in with `Wants=` rather than `Requires=`, so it is harmless whether or not it is installed.

Two further deliberate details: on Podman 4.x, `Notify=` accepts only `true`/`false`, and an invalid value makes Quadlet **fail to generate the unit at all** — the symptom is `Unit litellm-postgres.service not found`, which looks nothing like a config error. And `Notify=` is deliberately **not** set on `litellm.container` itself, citing podman issue #27290; the proxy's readiness is handled by `HealthStartupCmd=` instead.

> **Important:** the wait unit is a plain `.service` file and must be installed into `~/.config/systemd/user/`, **not** into `~/.config/containers/systemd/`. Quadlet reads only its own file types from its search paths, so a `.service` dropped there is silently ignored and you get `Unit not found`. `install.sh` places it correctly. It also rewrites the hardcoded `/usr/bin/podman` in `ExecStart=` to whatever `command -v podman` reports, because a systemd user unit's `PATH` is minimal.

### 5.3 A named volume, not a rootless bind mount

The obvious translation of a PVC is a host directory bind mount — `./pgdata:/var/lib/postgresql/data`. Both paths use a **named volume** instead.

**Why.** The `postgres:16-alpine` image drops privileges to UID 70. Under rootless Podman, in-container UID 70 maps through the user's subuid range to some high host UID. A bind-mounted host directory owned by your login user therefore appears inside the container as owned by `nobody`, and Postgres refuses to start with a permissions error that gives no hint about user namespaces. Named volumes are created inside Podman's own storage with the correct ownership already applied, so the problem does not arise.

The escape hatches exist if you must bind-mount — the `:U` mount option asks Podman to chown the source to the container's mapped UID, and `podman unshare chown -R 70:70 ./pgdata` does it manually — but both mutate host directory ownership in ways that surprise people later. A named volume is the safe default. `.gitignore` blocks `data/` and `pgdata/` anyway, in case anyone tries.

The config file *is* bind-mounted, because it is read-only, is not written by a dropped-privilege process, and eliminating the ConfigMap copy is the point. Both paths use `:ro,Z` — the uppercase `Z` applies a **private** SELinux label; `:z` (lowercase) applies a shared one and should not be used on a directory you care about.

### 5.4 The Postgres port is not published, in either path

The compose file has `expose: ["5432"]` and **no** `ports:`. The Quadlet unit has **no** `PublishPort=` and a long inline comment explaining the omission.

**Why.** On k3s this database sits behind a headless ClusterIP Service and is unreachable from outside the cluster. A naive compose translation habitually adds `ports: ["5432:5432"]` because that is what people do to make `psql` convenient — and on a bare mapping that binds all interfaces, exposing every virtual key, every budget row and every encrypted provider credential to the host and to the LAN. The proxy reaches Postgres over the internal bridge network; nothing on the host needs the port.

For ad-hoc access: `podman exec -it litellm-postgres psql -U litellm -d litellm`.

**A related asymmetry worth knowing about.** The two paths publish the *proxy* port differently. `docker-compose.yml` uses `"${LITELLM_PORT:-4000}:4000"`, which binds all interfaces (a loopback-only alternative is present but commented out), because a Portainer-deployed stack is usually meant to be reachable from the LAN. `quadlet/litellm.container` uses `PublishPort=127.0.0.1:4000:4000` — **loopback only** — because the Quadlet path is the production one and the safe default there is that nothing is reachable until you deliberately put a reverse proxy in front of it. This is intentional, not an oversight, but it does mean the two paths are not interchangeable without adjusting that one line.

### 5.5 cloudflared is omitted entirely

**k3s runs it:** `cloudflare/cloudflared:latest` with `args: [tunnel, --no-autoupdate, run]`, `TUNNEL_TOKEN` from a Secret, a liveness probe on `/ready` port 2000.

**This repo ships nothing.** No container, no unit, no service, no token field in any `.env.example`, no mention in `.gitignore` beyond the generic secret patterns.

**Why.** The k3s manifest's own header states that it **reuses the existing tunnel**, whose dashboard routing must stay untouched. A Cloudflare tunnel token identifies a *tunnel*, not a *connector*. Running a second connector with the same token registers an additional origin against the same tunnel, and Cloudflare then load-balances across both — meaning roughly half of production traffic would be routed to whatever machine this repo was installed on. That machine has a different database, different virtual keys and possibly a different config. It would not error; it would just serve wrong answers to half the requests, intermittently, in a way that is genuinely hard to diagnose from the Cloudflare side.

The compose file ends with a large `DELIBERATELY ABSENT: cloudflared` block saying exactly this. External access is documentation only: a reverse proxy (Caddy/nginx/Traefik) terminating TLS in front of `127.0.0.1:4000`, a Tailscale/WireGuard overlay, or a deliberate LAN port exposure — each with the warning that **the k3s tunnel token must never be reused here**. If a Cloudflare tunnel is genuinely wanted for this deployment, create a *new* tunnel with a *new* token and new hostname routing. That is out of scope for this repo.

### 5.6 One config file, mounted directly, at `/app/config.yaml`

k3s mounts the ConfigMap at `/etc/litellm` and passes `--config /etc/litellm/config.yaml`. **Both Podman paths use `/app/config.yaml`** — `command: ["--config","/app/config.yaml","--port","4000"]` in compose, `Exec=--config /app/config.yaml --port 4000` in Quadlet — mounting the single real file rather than a directory.

The path differs from k3s. That is fine: the path is arbitrary, it appears in exactly one place per path, and `/app` matches the image's own working directory convention. What matters is that there is now **one** copy of the config in the repository instead of two, so the `KEEP THIS IN SYNC` hazard in `k8s/03-litellm-config.yaml` is structurally eliminated rather than merely documented.

### 5.7 Arguments only — never override the entrypoint

Neither path sets `entrypoint:` or `Entrypoint=`, and neither prefixes the arguments with the word `litellm`. The image's ENTRYPOINT is `docker/prod_entrypoint.sh`, which ends in `exec litellm "$@"`, and its CMD is `["--port","4000"]`. So `command:` / `Exec=` supply *arguments to `litellm`*, exactly as the k3s `args:` field does. Writing `command: ["litellm","--config",...]` would produce `litellm litellm --config ...`, and overriding the entrypoint would skip the image's own startup work. This is easy to get wrong when translating between the two formats and is worth stating plainly.

### 5.8 Image references are fully qualified; digest pinning is offered but not shipped

`postgres:16-alpine` becomes `docker.io/library/postgres:16-alpine`. Short names resolve through the `registries.conf` search order and can prompt interactively — which fails silently inside a non-interactive systemd unit.

`16-alpine` is also a **moving tag**: two hosts installing this repo weeks apart can end up on different Postgres binaries against the same on-disk data directory. `litellm-postgres.container` therefore carries a commented digest-pinned image line and the two commands needed to produce the digest. **No digest is shipped**, because none was pulled or verified while writing this repo — inventing one would have been worse than leaving the tag.

Relatedly, `AutoUpdate=registry` is deliberately not set: it would silently replace the database engine underneath a live data directory. Updates should be deliberate.

---

## 6. Operational consequences

### 6.1 What you lose by leaving Kubernetes

**Zero-downtime updates.** `maxUnavailable: 0` and `maxSurge: 1` do not survive the move. Every image upgrade, config change or `systemctl restart` produces a short window in which requests fail. With `HealthStartPeriod=120s` and a `HealthStartupRetries=40 × 15s` budget, LiteLLM's own startup can take a while; the restart is reliable, not invisible. Clients need retry logic, or you need a maintenance window.

**Declarative reconciliation.** Nothing watches the deployment. A container removed by hand stays removed. A unit file edited without `daemon-reload` has no effect. Drift is detected — by `scripts/smoke-test.sh`, by `systemctl --user status`, by monitoring for `failed` units — but never corrected. The operating model shifts from "declare and let the controller converge" to "apply, then verify".

**A real secret store.** Unix file permissions on a `0600` env file, and nothing more. No RBAC, no per-key mounting, no encryption at rest, no audit trail of who read what.

**Horizontal scale and self-healing across failure domains.** One replica on one machine. If the machine dies, the service dies. There is no rescheduling, no node draining, no PodDisruptionBudget. A `Restart=always` unit handles a crashed process; it does not handle a crashed host.

**The Kubernetes ecosystem.** `kubectl logs -f --previous`, `kubectl rollout undo`, `kubectl top`, metrics-server, and anything expecting a Kubernetes API to talk to. The Podman equivalents (`journalctl --user -u litellm`, `podman stats`, `podman events`) are good but not the same, and the rootless cgroup caveat means `podman stats` may not show CPU limits at all.

**Resource guarantees.** `requests` are gone. Nothing reserves capacity for these containers; they compete with everything else on the box. On a machine that also runs other work, that is a real risk.

### 6.2 What you gain

**No control plane.** No API server, no etcd or SQLite datastore, no scheduler, no controller-manager, no kubelet, no CNI, no CSI provisioner. For a two-container workload that never moves, that is an enormous reduction in the number of things that can be broken, misconfigured, or in need of patching. It is also a large reduction in idle resource consumption — a meaningful difference on an edge box or a small VPS.

**The machine's own init system supervises the workload.** systemd already restarts failed services, orders startup, handles boot, collects logs and exposes status. Quadlet uses that instead of reimplementing it. `systemctl --user status litellm`, `journalctl --user -u litellm -f`, and `systemctl --user list-units 'litellm*'` are the whole operational interface, and every Linux administrator already knows them.

**Boots with the machine, for real.** `[Install] WantedBy=default.target multi-user.target` plus `loginctl enable-linger $USER` means the stack comes up after a power cut with no cron hack, no `@reboot`, no wrapper script.

**Rootless by default.** No root daemon anywhere in the picture. A container escape lands in an unprivileged user account. This is a genuine improvement over both a Docker daemon and a root-running kubelet, and it costs almost nothing for this workload — the only real friction is the UID-mapping issue that §5.3 designs around and the cgroup delegation caveat in §4.5.

**One copy of the configuration.** The ConfigMap duplication and its `KEEP THIS IN SYNC` warning are gone by construction.

**Far less to go wrong.** This is the honest summary. On a single node, most of what Kubernetes provides is machinery for problems that only exist when you have more than one node. Removing it removes the failure modes that come with it.

**Two supported ways in.** Compose for people who want to paste a stack into Portainer or match the sibling repos; Quadlet for people who want the machine to own the workload. The choice is documented rather than assumed.

---

## 7. When should you use which

### 7.1 Use Podman (this repo) when

- **It is a single node and will stay one.** Homelab server, edge appliance, NUC, developer workstation, small VPS. If the workload will never be scheduled onto a different machine, a scheduler is pure overhead.
- **Nobody is going to operate a cluster.** k3s is light, but it is still an API server, a datastore, and a set of controllers that need upgrading, backing up and debugging. If there is no one whose job that is, do not create the job.
- **Rootless containers are a stated requirement.** Podman gives you that without a privileged daemon.
- **Reboot survival matters more than update smoothness.** Quadlet plus lingering gives excellent boot behaviour and mediocre update behaviour. For a gateway used by a handful of internal clients, that is the right trade.
- **You want a small blast radius.** Two containers, two units, one env file, one volume.
- **Portainer is the management interface** — use the compose path specifically.

**Within this repo:** choose **compose** for development boxes, Portainer-managed hosts, and consistency with the sibling repositories. Choose **Quadlet** for anything you intend to leave running unattended — it is the production path, it boots properly, and it has the better health gate.

### 7.2 Stay on k3s (or move to a larger Kubernetes) when

- **You need more than one node**, whether for capacity or for survival of a single machine failure. Nothing in this repo addresses that, and no amount of Podman configuration will.
- **High availability is a requirement.** If a host reboot must not take the gateway down, you need a scheduler and multiple replicas.
- **Zero-downtime rolling updates are a requirement.** `maxUnavailable: 0`/`maxSurge: 1` has no Podman equivalent, and pretending otherwise would be dishonest. This is the single clearest dividing line.
- **You need horizontal scaling**, manual or automatic. One LiteLLM container has one process pool.
- **You already have a cluster and a team that runs it.** The marginal cost of one more Deployment in an existing cluster is close to zero; the marginal cost of one more bespoke single-node host is not.
- **Declarative reconciliation is part of your operating model** — GitOps, ArgoCD, Flux, or simply an expectation that `kubectl apply` converges reality onto the repository. Podman has no controller and this repo does not fake one.
- **You need real secret management, RBAC, network policy, or admission control.** These are Kubernetes features with no single-node analogue.

### 7.3 The dividing line, stated once

**If the answer to "what happens when this machine goes down?" must be anything other than "the service is down until it comes back", you need Kubernetes.** Everything else in this comparison is a preference. That one is a requirement, and it is not negotiable by configuration.

For the WOOWTECH gateway specifically: the production deployment stays on k3s, behind its existing Cloudflare tunnel, and this repository exists to run the same stack on machines where a cluster would be the wrong answer.

---

## 8. Verification, and what remains unverified

Nothing in this repository was executed against a live Podman host. Before trusting any single-node deployment built from it, run at least the following on the target machine:

| Check | Command |
|---|---|
| cgroup v2 present (Quadlet requires it) | `podman info --format '{{.Host.CgroupsVersion}}'` |
| Podman version vs `Notify=healthy` (needs 5.0+) | `podman --version` |
| **Which unit names Quadlet will actually generate** | `QUADLET_UNIT_DIRS=<repo>/quadlet /usr/lib/systemd/system-generators/podman-system-generator --user --dryrun` |
| Units are running | `systemctl --user list-units 'litellm*'` |
| Container health | `podman ps --format '{{.Names}}\t{{.Status}}'` |
| Rootless CPU limits are actually applied | `podman stats` |
| End-to-end function | `scripts/smoke-test.sh` |

The dry-run in the third row is the most important. Quadlet's silent-skip behaviour on an unrecognised key means a typo produces `Unit not found` rather than a parse error, and the dry-run is the only way to see the generated unit names before you start anything.

Specific items that are documented from behaviour rather than from testing, and should be treated as such:

- `Notify=healthy` semantics on Podman 5.x, and the exact failure mode on 4.x.
- The `postgres` alias is set with `PodmanArgs=--network-alias=postgres` rather than the native `NetworkAlias=` key, because `NetworkAlias=` only exists from Podman 5.2 and Quadlet skips an entire unit that contains a key it does not recognise. `PodmanArgs=` itself requires Podman 4.6+.
- Whether `depends_on: condition: service_healthy` is honoured by your podman-compose (needs ≥ 1.3 with Podman ≥ 4.6).
- The commented hardening options (`NoNewPrivileges=`, `DropCapability=`/`AddCapability=`, `ReadOnly=`) are commented out precisely because they were never tested. Apply them one at a time and check `podman logs` after each.
- The `DISABLE_SCHEMA_UPDATE=false` decision in §5.1, which is a reasoned trade rather than a verified outcome.
