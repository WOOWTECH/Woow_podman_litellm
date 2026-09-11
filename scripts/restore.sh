#!/usr/bin/env bash
# scripts/restore.sh: restore a scripts/backup.sh directory into the installed gateway.
#
#   scripts/restore.sh <backup_dir> [--with-secrets] [--yes]
#
#   --with-secrets   also take LITELLM_SALT_KEY and LITELLM_MASTER_KEY from <backup_dir>/secrets.env
#                    (needed when restoring onto another host or a fresh install: the dump's
#                    provider credentials only decrypt with the salt key they were written with)
#   --yes            do not ask for confirmation
#
# Checks the dump's sha256 and the salt-key fingerprint, stops litellm.service, DROPS and
# recreates the litellm database, pg_restores the dump, starts litellm.service again and runs
# tests/smoke.sh. Postgres keeps running. Install the units first (scripts/install.sh).
# Without --with-secrets it refuses to restore a dump whose salt key differs from this host's.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
APP=litellm
PG=litellm-postgres
LL_UNIT=litellm.service

dir='' with_secrets=0 yes=0
while (($#)); do
  case $1 in
    --with-secrets) with_secrets=1 ;;
    --yes) yes=1 ;;
    -h | --help) sed -n '2,14p' "$0"; exit 0 ;;
    -*) ql_die "unknown option $1 (see --help)" ;;
    *) [[ -z $dir ]] || ql_die "one backup directory only"; dir=$1 ;;
  esac
  shift
done
[[ -n $dir && -d $dir ]] || ql_die "usage: scripts/restore.sh <backup_dir> [--with-secrets] [--yes]"
export QL_APP=$APP
ql_require_rootless
ql_require_user_systemd
ql_lock "$APP"

shopt -s nullglob
dumps=("$dir"/litellm-*.dump)
((${#dumps[@]} == 1)) || ql_die "expected exactly one litellm-*.dump in $dir, found ${#dumps[@]}"
dump=${dumps[0]}
if [[ -f $dump.sha256 ]]; then
  (cd -- "$dir" && sha256sum -c --quiet -- "${dump##*/}.sha256") || ql_die "checksum mismatch for $dump"
else
  ql_warn "no $dump.sha256; restoring without a checksum"
fi
[[ $(systemctl --user show -p LoadState --value "$LL_UNIT" 2>/dev/null) == loaded ]] \
  || ql_die "$LL_UNIT is not installed; run scripts/install.sh first"
[[ $(podman inspect --format '{{.State.Status}}' "$PG" 2>/dev/null || true) == running ]] \
  || ql_die "$PG is not running; start it with: systemctl --user start litellm-postgres.service"

fingerprint() { printf 'sha256:%s' "$(printf 'woow-litellm-salt-v1:%s' "$1" | sha256sum | cut -c1-16)"; }
current_salt=$(podman secret inspect --showsecret --format '{{.SecretData}}' litellm-salt-key 2>/dev/null || true)
target_fp=''
[[ -z $current_salt ]] || target_fp=$(fingerprint "$current_salt")
current_salt=''
backup_fp=$(cat -- "$dir/salt.fingerprint" 2>/dev/null || true)

if ((with_secrets)); then
  [[ -f $dir/secrets.env ]] || ql_die "--with-secrets: $dir/secrets.env not found"
  ql_env_load "$dir/secrets.env"
  [[ -n ${QL_ENV[LITELLM_SALT_KEY]:-} ]] || ql_die "$dir/secrets.env has no LITELLM_SALT_KEY"
  if [[ -n $backup_fp && $(fingerprint "${QL_ENV[LITELLM_SALT_KEY]}") != "$backup_fp" ]]; then
    ql_die "secrets.env does not match salt.fingerprint in $dir; refusing"
  fi
elif [[ -z $backup_fp ]]; then
  ql_warn "the backup has no salt.fingerprint: cannot check that its salt key matches this host's"
elif [[ $backup_fp != "$target_fp" ]]; then
  ql_die "the backup was made with a different LITELLM_SALT_KEY (backup $backup_fp, this host ${target_fp:-none}). Its provider credentials would be unreadable here. Re-run with --with-secrets to take the backup's salt key."
fi

if ((!yes)); then
  [[ -t 0 ]] || ql_die "restore DROPS the litellm database; add --yes to confirm non-interactively"
  read -r -p "DROP the litellm database and restore ${dump##*/}? Type '$APP' to continue: " answer
  [[ $answer == "$APP" ]] || ql_die "aborted; nothing was changed"
fi

psql_admin() { podman exec -i "$PG" psql -U litellm -d postgres -v ON_ERROR_STOP=1 -q; }

ql_info "stopping $LL_UNIT (Postgres keeps running)"
systemctl --user stop "$LL_UNIT"
printf '%s\n' 'DROP DATABASE IF EXISTS litellm WITH (FORCE);' 'CREATE DATABASE litellm OWNER litellm;' | psql_admin \
  || ql_die "could not recreate the litellm database; $LL_UNIT stays stopped (see: podman logs $PG)"
rc=0
podman exec -i "$PG" pg_restore -U litellm -d litellm --no-owner --no-privileges --single-transaction <"$dump" || rc=$?
((rc == 0)) || ql_warn "pg_restore exited $rc; see above. The database may be incomplete"
tables=$(printf '%s\n' "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE 'LiteLLM%';" \
  | podman exec -i "$PG" psql -U litellm -d litellm -tA 2>/dev/null | tr -d '[:space:]' || true)
[[ ${tables:-0} -gt 0 ]] || ql_die "no LiteLLM_* tables after the restore; $LL_UNIT stays stopped"
ql_info "restored ${tables} LiteLLM_* table(s) from ${dump##*/}"

if ((with_secrets)); then
  ql_warn "replacing litellm-salt-key with the backup's key: it belongs to the database just restored"
  ql_secret_ensure litellm-salt-key env:LITELLM_SALT_KEY --update
  if [[ -n ${QL_ENV[LITELLM_MASTER_KEY]:-} ]]; then ql_secret_ensure litellm-master-key env:LITELLM_MASTER_KEY --update; fi
fi

systemctl --user start "$LL_UNIT"
ql_wait_container_healthy litellm 900 || ql_die "litellm did not become healthy after the restore"
"$REPO/tests/smoke.sh" || ql_die "tests/smoke.sh failed after the restore"
ql_info "restore complete"
