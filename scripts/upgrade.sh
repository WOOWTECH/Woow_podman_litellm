#!/usr/bin/env bash
# scripts/upgrade.sh: move the gateway to the image versions this checkout pins, with an
# automatic rollback of the units when the new version fails its smoke test.
#
#   git pull && scripts/upgrade.sh [--no-backup]
#
#   1. snapshot the installed unit files and the install manifest
#   2. scripts/backup.sh: pg_dump + secrets.env, BEFORE anything changes (LiteLLM's Prisma
#      migrations are one-way)                     (skipped with --no-backup)
#   3. scripts/install.sh: pulls the new pinned images, re-renders the units, restarts what
#      changed (the first boot of a new version migrates the schema), runs tests/smoke.sh
#   4. on any failure: put the snapshot back, restart on the previous images (install never
#      deletes an image), re-run tests/smoke.sh, print how to restore the pre-upgrade dump,
#      exit 1
#
# SMOKE_FORCE_FAIL=1 makes step 3 fail on purpose to exercise the rollback.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
APP=litellm
UNITS=(litellm-postgres.service litellm.service)
KEEP_SNAPSHOTS=5

backup=1
while (($#)); do
  case $1 in
    --no-backup) backup=0 ;;
    -h | --help) sed -n '2,16p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
export QL_APP=$APP
ql_require_rootless
ql_require_user_systemd
STATE=${QL_STATE_ROOT:-$HOME/.local/state/woow-quadlet}/$APP
[[ -s $STATE/manifest ]] || ql_die "$APP is not installed (no $STATE/manifest); run scripts/install.sh"

# ---- 1. snapshot ------------------------------------------------------------------------------
snap=$STATE/rollback/$(date +%Y%m%d-%H%M%S)
(umask 077 && mkdir -p -- "$snap/files") || ql_die "cannot create $snap"
cp -p -- "$STATE/manifest" "$snap/manifest"
while read -r _ path; do
  [[ -f $path ]] || continue
  mkdir -p -- "$snap/files${path%/*}"
  cp -p -- "$path" "$snap/files$path"
done <"$STATE/manifest"
ql_info "snapshot of the installed units: $snap"
snaps=("$STATE"/rollback/*/) # timestamp names: glob order is age order
if ((${#snaps[@]} > KEEP_SNAPSHOTS)); then rm -rf -- "${snaps[@]:0:${#snaps[@]}-KEEP_SNAPSHOTS}"; fi

# ---- 2. backup (a dump before a possible schema migration) -----------------------------------
bdir=''
if ((backup)); then
  bdir=$("$REPO/scripts/backup.sh") || ql_die "backup failed; nothing was changed"
fi

rollback() {
  local path rel
  ql_warn "upgrade failed: rolling back to the snapshot $snap"
  if [[ -f $STATE/manifest ]]; then
    while read -r _ path; do
      grep -qF -- "  $path" "$snap/manifest" || rm -f -- "$path"
    done <"$STATE/manifest"
  fi
  while IFS= read -r -d '' rel; do
    rel=${rel#"$snap/files"}
    mkdir -p -- "${rel%/*}"
    cp -p -- "$snap/files$rel" "$rel"
  done < <(find "$snap/files" -type f -print0)
  cp -p -- "$snap/manifest" "$STATE/manifest"
  rm -f -- "$STATE/pending-restart"
  systemctl --user daemon-reload
  systemctl --user restart "${UNITS[@]}" || ql_die "rollback: restart failed; see journalctl --user -u litellm -n 100"
  ql_wait_container_healthy litellm-postgres 300 || ql_die "rollback: litellm-postgres is not healthy"
  ql_wait_container_healthy litellm 900 || ql_warn "rollback: litellm is not healthy yet"
  if env -u SMOKE_FORCE_FAIL "$REPO/tests/smoke.sh"; then
    ql_warn "rolled back; the previous version is running again"
  else
    ql_warn "rolled back, but tests/smoke.sh still fails"
  fi
  if [[ -n $bdir ]]; then
    ql_warn "if the new version migrated the database schema before it failed, restore the pre-upgrade dump:"
    printf '    %s/scripts/restore.sh %s --yes\n' "$REPO" "$bdir" >&2
  fi
  exit 1
}

# ---- 3. install + smoke (install.sh runs tests/smoke.sh) ------------------------------------
if ! "$REPO/scripts/install.sh"; then rollback; fi
ql_info "upgrade complete; rollback snapshot kept in $snap${bdir:+, pre-upgrade backup in $bdir}"
