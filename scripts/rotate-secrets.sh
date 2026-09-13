#!/usr/bin/env bash
# scripts/rotate-secrets.sh: rotate the gateway's credentials.
#
#   scripts/rotate-secrets.sh --db        new database password: ALTER ROLE inside the running
#                                         Postgres container, then replace the secrets
#                                         litellm-postgres-password and litellm-database-url and
#                                         restart the proxy
#   scripts/rotate-secrets.sh --master    new master key (the /v1 admin credential and the Admin
#                                         UI password): replace litellm-master-key and restart
#                                         the proxy. EVERY CLIENT that uses the master key must
#                                         be updated; virtual keys stored in the database are not
#                                         affected
#   scripts/rotate-secrets.sh --salt      refused, with an explanation
#
#   --yes    do not ask for confirmation
#
# New values are generated from /dev/urandom, go to podman through stdin and are never printed,
# never in argv and never written to a file. Each rotation ends with tests/smoke.sh.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
APP=litellm
PG=litellm-postgres
LL_UNIT=litellm.service

what='' yes=0
while (($#)); do
  case $1 in
    --db | --master | --salt) what=${1#--} ;;
    --yes) yes=1 ;;
    -h | --help) sed -n '2,19p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
[[ -n $what ]] || ql_die "say what to rotate: --db, --master (or --salt for the explanation)"
export QL_APP=$APP
ql_require_rootless
ql_require_user_systemd

if [[ $what == salt ]]; then
  cat >&2 <<'EOF'
The salt key is NOT rotatable.

LiteLLM encrypts every provider credential it stores in Postgres with a key derived from
LITELLM_SALT_KEY. Replacing it does not re-encrypt anything: the existing rows become
permanently unreadable, and LiteLLM does not fail loudly about it - models simply stop working
and the log asks "Did your master_key/salt key change recently?".

If you must change it: export what you need, delete the stored credentials, rotate the secret
and re-enter every provider credential by hand. There is no supported path in this repo.
EOF
  exit 2
fi

ql_lock "$APP"
rand_alnum() {
  local s='' c
  while ((${#s} < $1)); do
    c=$(head -c 256 /dev/urandom | base64 -w0) || return 1
    s+=${c//[!A-Za-z0-9]/}
  done
  printf '%s' "${s:0:$1}"
}
confirm() {
  ((!yes)) || return 0
  [[ -t 0 ]] || ql_die "$1; add --yes to confirm non-interactively"
  read -r -p "$1. Type '$APP' to continue: " answer
  [[ $answer == "$APP" ]] || ql_die "aborted; nothing was changed"
}

if [[ $what == db ]]; then
  [[ $(podman inspect --format '{{.State.Status}}' "$PG" 2>/dev/null || true) == running ]] \
    || ql_die "$PG is not running; start it with: systemctl --user start litellm-postgres.service"
  confirm "This changes the database password of role litellm and restarts the proxy"
  old=$(podman secret inspect --showsecret --format '{{.SecretData}}' litellm-postgres-password 2>/dev/null || true)
  new=$(rand_alnum 32)
  # The SQL goes in on stdin (printf is a shell builtin), so the password never reaches argv.
  printf "ALTER ROLE litellm WITH PASSWORD '%s';\n" "$new" \
    | podman exec -i "$PG" psql -U litellm -d litellm -v ON_ERROR_STOP=1 -q \
    || ql_die "ALTER ROLE failed; nothing was changed"
  # shellcheck disable=SC2034 # read by name (env:LITELLM_PG_NEW / env:LITELLM_DATABASE_URL)
  LITELLM_PG_NEW=$new
  ql_secret_ensure litellm-postgres-password env:LITELLM_PG_NEW --replace \
    || ql_die "the role password was changed but the secret was not: re-run this script"
  # shellcheck disable=SC2034 # read by name
  LITELLM_DATABASE_URL="postgresql://litellm:$new@litellm-postgres:5432/litellm"
  ql_secret_ensure litellm-database-url env:LITELLM_DATABASE_URL --replace
  unset LITELLM_PG_NEW LITELLM_DATABASE_URL
  new=''
  ql_info "restarting $LL_UNIT with the new DATABASE_URL"
  systemctl --user restart "$LL_UNIT"
  ql_wait_container_healthy litellm 900 || ql_die "litellm did not become healthy after the rotation"
  if [[ -n $old ]]; then
    if printf '%s\n' "$old" | podman exec -i "$PG" sh -c 'read -r p; PGPASSWORD=$p psql -h 127.0.0.1 -U litellm -d litellm -tAc "select 1"' >/dev/null 2>&1; then
      ql_warn "the OLD password still authenticates over TCP: check the ALTER ROLE above"
    else
      ql_info "the old password no longer authenticates"
    fi
    old=''
  fi
elif [[ $what == master ]]; then
  confirm "This changes the master key: every client using it, and the Admin UI login, must be updated"
  # shellcheck disable=SC2034 # read by name (env:LITELLM_MASTER_NEW)
  LITELLM_MASTER_NEW="sk-$(rand_alnum 48)"
  ql_secret_ensure litellm-master-key env:LITELLM_MASTER_NEW --replace
  unset LITELLM_MASTER_NEW
  ql_info "restarting $LL_UNIT with the new master key"
  systemctl --user restart "$LL_UNIT"
  ql_wait_container_healthy litellm 900 || ql_die "litellm did not become healthy after the rotation"
  ql_warn "read the new key with: podman secret inspect --showsecret --format '{{.SecretData}}' litellm-master-key"
fi

"$REPO/tests/smoke.sh" || ql_die "tests/smoke.sh failed after the rotation"
ql_info "rotation complete ($what)"
