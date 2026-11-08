#!/usr/bin/env bash
# scripts/install.sh: install or update the LiteLLM gateway (litellm + litellm-postgres) as
# rootless Quadlet units (podman >= 4.9, systemd --user, linger). Idempotent: an unchanged
# re-run restarts nothing.
#
#   scripts/install.sh [--port N] [--bind ADDR] [--set KEY=VALUE]... [--no-pull]
#                      [--no-start] [--dry-run] [--yes]
#
#   --port N          publish port (LITELLM_PORT, default 4000); saved in the env file
#   --bind ADDR       publish address (LITELLM_BIND, default 127.0.0.1); saved in the env file
#   --set KEY=VALUE   set any key of config/litellm.env.example in the env file
#   --no-pull         never pull; both pinned images must already exist
#   --no-start        install the files and daemon-reload only
#   --dry-run         render, validate and report what would change; change nothing. Works
#                     without an env file (renders the example) and is deterministic
#   --yes             accepted for symmetry with the other scripts (nothing to confirm)
#
# Per-host values live in ~/.config/litellm/litellm.env (0600), created from
# config/litellm.env.example on the first run. Keys become podman secrets:
#   litellm-postgres-password   random; Postgres reads it as a file (POSTGRES_PASSWORD_FILE)
#   litellm-database-url        derived from the password on every run
#   litellm-master-key          generated once (sk-...), or imported from LITELLM_MASTER_KEY
#   litellm-salt-key            generated once, or imported from LITELLM_SALT_KEY; NEVER replaced
#   litellm-ui-password        generated once or imported; independent of the API master key
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"

# ---- per-repo settings -----------------------------------------------------------------
APP=litellm
ENV_FILE=$HOME/.config/$APP/$APP.env
EXAMPLE=$REPO/config/$APP.env.example
PODMAN_MIN=4.9
PG_CONTAINER=litellm-postgres
LL_CONTAINER=litellm
PG_UNIT=litellm-postgres.service
LL_UNIT=litellm.service
UNITS=(litellm-network.service litellm-pgdata-volume.service "$PG_UNIT" "$LL_UNIT" litellm-backup.timer)
VOLUME=litellm-pgdata
QDIR=${QL_QUADLET_DIR:-$HOME/.config/containers/systemd}
# ------------------------------------------------------------------------------------------

usage() { sed -n '2,26p' "$0"; }
sets=() no_pull=0 no_start=0
while (($#)); do
  case $1 in
    --port) sets+=("LITELLM_PORT=${2:?--port needs a value}"); shift ;;
    --bind) sets+=("LITELLM_BIND=${2:?--bind needs a value}"); shift ;;
    --set) sets+=("${2:?--set needs KEY=VALUE}"); shift ;;
    --no-pull) no_pull=1 ;;
    --no-start) no_start=1 ;;
    --dry-run) export QL_DRY_RUN=1 ;;
    --yes) ;;
    -h | --help) usage; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
export QL_APP=$APP
DRY=${QL_DRY_RUN:-0}

secret_exists() { podman secret exists "$1" >/dev/null 2>&1; }
# rand_alnum N: [A-Za-z0-9] from /dev/urandom (no pipe that can SIGPIPE under pipefail)
rand_alnum() {
  local s='' c
  while ((${#s} < $1)); do
    c=$(head -c 256 /dev/urandom | base64 -w0) || return 1
    s+=${c//[!A-Za-z0-9]/}
  done
  printf '%s' "${s:0:$1}"
}

# ---- 1. host preflight ---------------------------------------------------------------------
ql_preflight "$PODMAN_MIN"
command -v curl >/dev/null 2>&1 || ql_die "curl not found (sudo apt-get install curl)"
ql_enable_linger
ql_lock "$APP"

WORK=$(mktemp -d "${TMPDIR:-/tmp}/$APP-install.XXXXXX")
ql_cleanup work rm -rf "$WORK"

# ---- 2. per-host settings (D2: rendered from the env file at install time) -----------------
ql_env_ensure "$EXAMPLE" "$ENV_FILE"
# Settings are staged in a private copy and saved only after every check below passed, so
# a rejected --port/--bind/--set never lands in the env file. A dry run never saves them.
envsrc=$WORK/$APP.env
if [[ -f $ENV_FILE ]]; then cp -- "$ENV_FILE" "$envsrc"; else cp -- "$EXAMPLE" "$envsrc"; QL_ENV_CREATED=1; fi
chmod 600 "$envsrc"
setenv() { QL_DRY_RUN=0 ql_env_set "$envsrc" "$1" "$2"; }
[[ $QL_ENV_CREATED != 1 ]] || ql_info "created $ENV_FILE with defaults; edit it and re-run to change them"
for kv in "${sets[@]}"; do
  [[ $kv == *=* ]] || ql_die "--set wants KEY=VALUE, got '$kv'"
  grep -q "^${kv%%=*}=" "$EXAMPLE" || ql_die "--set: ${kv%%=*} is not a setting of ${EXAMPLE##*/}"
  case ${kv%%=*} in *_KEY|*_PASSWORD) ql_die "--set ${kv%%=*}: put credentials in $ENV_FILE (0600), not on the command line" ;; esac
  setenv "${kv%%=*}" "${kv#*=}"
done
if ! grep -q '^LITELLM_UI_USERNAME=' "$envsrc"; then setenv LITELLM_UI_USERNAME admin; fi
if ! grep -q '^LITELLM_UI_PASSWORD=' "$envsrc"; then setenv LITELLM_UI_PASSWORD ''; fi
ql_env_load "$envsrc"

BIND=$(ql_env_get LITELLM_BIND)
PORT=$(ql_env_get LITELLM_PORT)
ql_assert_match LITELLM_BIND "$BIND" '(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])){3}'
ql_assert_match LITELLM_PORT "$PORT" '[1-9][0-9]{0,4}'
((PORT <= 65535)) || ql_die "LITELLM_PORT=$PORT is not a TCP port"
ql_assert_match LITELLM_LOG "$(ql_env_get LITELLM_LOG)" 'DEBUG|INFO|WARNING|ERROR|CRITICAL'
ql_assert_match LITELLM_UI_USERNAME "$(ql_env_get LITELLM_UI_USERNAME)" '[A-Za-z0-9._-]+'
if [[ $BIND == 0.0.0.0 ]]; then
  ql_warn "LITELLM_BIND=0.0.0.0: the /v1 API and the Admin UI are reachable from every network; prefer a reverse proxy"
elif [[ $BIND != 127.0.0.1 ]]; then
  ql_warn "LITELLM_BIND=$BIND: the /v1 API and the Admin UI are reachable from that network, not only from this host"
fi

MASTER_IN=$(ql_env_get LITELLM_MASTER_KEY '')
SALT_IN=$(ql_env_get LITELLM_SALT_KEY '')
UI_IN=$(ql_env_get LITELLM_UI_PASSWORD '')
if [[ -n $MASTER_IN && ! $MASTER_IN =~ ^sk-[^[:space:]]{8,}$ ]]; then
  ql_die "LITELLM_MASTER_KEY in $ENV_FILE must start with sk- and have at least 8 more characters"
fi
if [[ -n $UI_IN && ${#UI_IN} -lt 12 ]]; then ql_die "LITELLM_UI_PASSWORD must have at least 12 characters"; fi
if [[ -n $SALT_IN ]] && secret_exists litellm-salt-key; then
  current=$(podman secret inspect --showsecret --format '{{.SecretData}}' litellm-salt-key) || ql_die "cannot read secret litellm-salt-key"
  if [[ $current != "$SALT_IN" ]]; then
    current=''
    ql_die "LITELLM_SALT_KEY in $ENV_FILE differs from the existing secret litellm-salt-key. The salt key is never replaced (it would make every stored provider credential unreadable). Blank it in the env file to keep the secret."
  fi
  current=''
fi
if podman volume exists "$VOLUME" >/dev/null 2>&1; then
  mountpoint=$(podman volume inspect --format '{{.Mountpoint}}' "$VOLUME")
  pg_version=$(cat "$mountpoint/pgdata/PG_VERSION" 2>/dev/null || true)
  [[ -z $pg_version || $pg_version == 17 ]] || ql_die "existing PostgreSQL volume is version $pg_version; PostgreSQL 17 cannot start on it. Use the dump-and-restore procedure in docs/alignment.md on a fresh host or Podman user"
  if ! secret_exists litellm-postgres-password; then
    ql_die "volume $VOLUME exists but the secret litellm-postgres-password does not: the database password is unknown. See README \"Troubleshooting\" (recreate the secret, then scripts/rotate-secrets.sh --db)."
  fi
  if [[ -z $SALT_IN ]] && ! secret_exists litellm-salt-key; then
    ql_die "volume $VOLUME exists but the secret litellm-salt-key does not. Put the old key into LITELLM_SALT_KEY in $ENV_FILE (from the backup's secrets.env) and re-run; a new salt key would make every stored provider credential unreadable."
  fi
fi

# ---- 3. legacy guards ------------------------------------------------------------------------
ql_check_container_collision "$PG_CONTAINER" "$PG_UNIT"
ql_check_container_collision "$LL_CONTAINER" "$LL_UNIT"
# The port must be free, unless our running container is the one already publishing it.
published=$(sed -n 's/^PublishPort=//p' "$QDIR/$LL_CONTAINER.container" 2>/dev/null || true)
if [[ $published != "$BIND:$PORT:4000" || $(systemctl --user is-active "$LL_UNIT" 2>/dev/null || true) != active ]] \
  && command -v ss >/dev/null 2>&1 && [[ -n $(ss -ltnH "sport = :$PORT" 2>/dev/null || true) ]]; then
  ql_die "port $PORT is already in use on this host (ss -ltnp 'sport = :$PORT'); pick another with --port"
fi

# ---- 4. render the units and validate them against the podman 4.9.3 generator -------------
# ql_dryrun keeps the generator's stdout (the units, which echo their own comments) apart
# from its stderr (the diagnostics). The old quadlet/install.sh grepped both together for
# "error|failed" and failed on valid units about 2 runs in 3.
mkdir -p "$WORK/src" "$WORK/out/config"
cp -p "$REPO"/quadlet/*.container "$REPO"/quadlet/*.volume "$REPO"/quadlet/*.network "$WORK/src/"
ql_render "$WORK/src" "$envsrc" "$REPO/quadlet/render-vars" "$WORK/out"
cp -p "$REPO/quadlet/litellm-backup.timer" "$WORK/out/"
printf '[Unit]\nDescription=Backup LiteLLM PostgreSQL\nRequires=litellm-postgres.service\nAfter=litellm-postgres.service\n[Service]\nType=oneshot\nExecStart=%s/scripts/backup.sh --daily\n' "$REPO" >"$WORK/out/litellm-backup.service"
install -m 644 -- "$REPO/config/config.yaml" "$WORK/out/config/config.yaml"
ql_dryrun "$WORK/out" --verify --ref-dir "$QDIR" || ql_die "the rendered units failed the dry-run; nothing was installed"
for f in "$WORK/out"/*; do
  u=$(ql_unit_for "$f")
  [[ -z $u ]] || ql_check_unit_shadow "$u" "$APP"
done

# Every check passed: only now do new --port/--bind/--set values reach the env file.
if [[ $DRY != 1 ]] && ! cmp -s -- "$envsrc" "$ENV_FILE"; then
  install -m 600 -- "$envsrc" "$ENV_FILE" || ql_die "cannot update $ENV_FILE"
  ql_info "saved the new settings in $ENV_FILE"
fi

# ---- 5. images and secrets, before any unit changes (a pull never runs inside a start timeout)
if [[ $DRY != 1 && $no_pull == 0 ]]; then
  old_image=$(podman image inspect --format '{{.Id}}' localhost/woow-litellm:1.104.0-0.3.16 2>/dev/null || true)
  podman build -t localhost/woow-litellm:1.104.0-0.3.16 -f "$REPO/image/Dockerfile" "$REPO/image" \
    || ql_die "LiteLLM image build failed; nothing was installed"
  new_image=$(podman image inspect --format '{{.Id}}' localhost/woow-litellm:1.104.0-0.3.16)
  [[ $new_image == "$old_image" ]] || image_changed=1
fi
if ((no_pull)); then
  mapfile -t images < <(sed -n 's/^Image=//p' "$WORK/out"/*.container)
  for img in "${images[@]}"; do
    podman image exists "$img" || ql_die "image $img is not present and --no-pull was given"
  done
else
  ql_pull_images "$WORK/out"
fi

# Generated and derived values reach ql_secret_ensure as shell variables (env:NAME), which
# it reads by name and pipes to `podman secret create -`: never argv, never printed.
restart_ll=${image_changed:-0}
ql_secret_ensure litellm-postgres-password random:32
if secret_exists litellm-postgres-password; then
  pw=$(podman secret inspect --showsecret --format '{{.SecretData}}' litellm-postgres-password) \
    || ql_die "cannot read secret litellm-postgres-password"
  # shellcheck disable=SC2034 # read by name (env:LITELLM_DATABASE_URL)
  LITELLM_DATABASE_URL="postgresql://litellm:$pw@litellm-postgres:5432/litellm"
  pw=''
  QL_SECRET_CHANGED=0
  ql_secret_ensure litellm-database-url env:LITELLM_DATABASE_URL --update
  ((QL_SECRET_CHANGED == 0)) || restart_ll=1
  unset LITELLM_DATABASE_URL
else
  ql_info "[dry-run] would derive secret litellm-database-url from litellm-postgres-password"
fi
if [[ -n $MASTER_IN ]]; then
  QL_SECRET_CHANGED=0
  ql_secret_ensure litellm-master-key env:LITELLM_MASTER_KEY --update
  ((QL_SECRET_CHANGED == 0)) || restart_ll=1
elif ! secret_exists litellm-master-key; then
  # shellcheck disable=SC2034 # read by name (env:LITELLM_MASTER_GEN)
  LITELLM_MASTER_GEN="sk-$(rand_alnum 48)"
  ql_secret_ensure litellm-master-key env:LITELLM_MASTER_GEN
  unset LITELLM_MASTER_GEN
else
  ql_secret_ensure litellm-master-key random:48 # exists: only recorded for --purge
fi
if [[ -n $SALT_IN ]]; then
  ql_secret_ensure litellm-salt-key env:LITELLM_SALT_KEY # never --update: compared above
elif ! secret_exists litellm-salt-key; then
  # shellcheck disable=SC2034 # read by name (env:LITELLM_SALT_GEN)
  LITELLM_SALT_GEN="sk-$(rand_alnum 48)"
  ql_secret_ensure litellm-salt-key env:LITELLM_SALT_GEN
  unset LITELLM_SALT_GEN
else
  ql_secret_ensure litellm-salt-key random:48 # exists: only recorded for --purge
fi
if [[ -n $UI_IN ]]; then
  QL_SECRET_CHANGED=0
  ql_secret_ensure litellm-ui-password env:LITELLM_UI_PASSWORD --update
  ((QL_SECRET_CHANGED == 0)) || restart_ll=1
elif ! secret_exists litellm-ui-password; then
  LITELLM_UI_GEN=$(rand_alnum 48)
  ql_secret_ensure litellm-ui-password env:LITELLM_UI_GEN
  unset LITELLM_UI_GEN
else
  ql_secret_ensure litellm-ui-password random:48
fi
MASTER_IN='' SALT_IN='' UI_IN=''

# ---- 6. install changed files, then start / restart only what changed -----------------------
changed=$(ql_install_files "$WORK/out" "$APP" --prune)
[[ -z $changed ]] || ql_info "changed: $(tr '\n' ' ' <<<"$changed")"
if [[ $DRY == 1 ]]; then
  ql_info "dry-run complete; nothing was changed"
  exit 0
fi
if grep -qx 'config/config.yaml' <<<"$changed"; then restart_ll=1; fi
((restart_ll == 0)) || ql_mark_changed "$APP" "$LL_UNIT"
if ! podman network exists litellm-net >/dev/null 2>&1; then ql_mark_changed "$APP" litellm-network.service; fi
if ! podman volume exists "$VOLUME" >/dev/null 2>&1; then ql_mark_changed "$APP" litellm-pgdata-volume.service; fi
if ((no_start)); then
  systemctl --user daemon-reload
  ql_info "installed; not started (--no-start). Start with: systemctl --user start ${UNITS[*]}"
  exit 0
fi
systemctl --user reset-failed "$PG_UNIT" "$LL_UNIT" 2>/dev/null || true
ql_apply_units "$APP" "${UNITS[@]}"

# ---- 7. health and smoke -----------------------------------------------------------------------
ql_wait_container_healthy "$PG_CONTAINER" 300 \
  || ql_die "$PG_CONTAINER did not become healthy; see: journalctl --user -u $PG_UNIT -n 100"
# The first boot runs the Prisma migrations: the startup probe allows up to 10 minutes.
ql_wait_container_healthy "$LL_CONTAINER" 900 \
  || ql_die "$LL_CONTAINER did not become healthy; see: journalctl --user -u $LL_UNIT -n 100"
ql_wait_http "http://$BIND:$PORT/health/readiness" '200' 120 \
  || ql_die "http://$BIND:$PORT/health/readiness did not answer 200"
"$REPO/tests/smoke.sh" || ql_die "tests/smoke.sh failed; see the output above"

cat >&2 <<EOF

LiteLLM is installed and healthy.

  API          http://$BIND:$PORT/v1   (loopback: put a reverse proxy or tunnel on this host in front)
  Admin UI     http://$BIND:$PORT/ui   user $(ql_env_get LITELLM_UI_USERNAME), password secret:
               podman secret inspect --showsecret --format '{{.SecretData}}' litellm-ui-password
  Salt key     never rotated; scripts/backup.sh writes it to secrets.env: keep a copy OFF this host
  Keys         you may now blank LITELLM_*_KEY in $ENV_FILE (the secrets stay)
  Logs         journalctl --user -u $LL_UNIT -u $PG_UNIT -f
  Settings     $ENV_FILE (edit, then re-run $0)

EOF
