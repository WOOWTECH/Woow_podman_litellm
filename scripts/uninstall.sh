#!/usr/bin/env bash
# scripts/uninstall.sh: remove the LiteLLM gateway's Quadlet units. Keeps data by default.
#
#   scripts/uninstall.sh                   stop + remove the units; keep the litellm-pgdata
#                                          volume, the network, the secrets, the images, the env
#                                          file and config.yaml (a re-install adopts them)
#   scripts/uninstall.sh --purge [--yes]   also delete the volume, the network and the five
#                                          secrets, after a final backup (pg_dump + secrets.env)
#                                          to ~/backups/litellm/. The ONLY way this repo deletes
#                                          data.
#   scripts/uninstall.sh --purge-images    also remove the two pinned images, each only when no
#                                          container and no other installed unit uses it
#   scripts/uninstall.sh --dry-run         report what would be removed
#
# Never deleted here: ~/.config/litellm/litellm.env (delete it yourself after --purge).
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"

# ---- per-repo settings -----------------------------------------------------------------
APP=litellm
VOLUME=litellm-pgdata
BACKUP_DIR=$HOME/backups/$APP
QDIR=${QL_QUADLET_DIR:-$HOME/.config/containers/systemd}
# ------------------------------------------------------------------------------------------

purge=0 yes=0 purge_images=0
while (($#)); do
  case $1 in
    --purge) purge=1 ;;
    --purge-images) purge_images=1 ;;
    --yes) yes=1 ;;
    --dry-run) export QL_DRY_RUN=1 ;;
    -h | --help) sed -n '2,16p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
export QL_APP=$APP
DRY=${QL_DRY_RUN:-0}
ql_require_rootless
ql_lock "$APP"
# the images the installed (or, when not installed, the repo's) units pin
mapfile -t images < <(sed -n 's/^Image=//p' "$QDIR"/litellm*.container "$REPO"/quadlet/*.container 2>/dev/null | sort -u)

if ((purge)); then
  if ((!yes)) && [[ $DRY != 1 ]]; then
    [[ -t 0 ]] || ql_die "--purge deletes the database volume, the network and the secrets; add --yes to confirm non-interactively"
    read -r -p "Type '$APP' to delete the LiteLLM database (keys, teams, spend), network and secrets: " answer
    [[ $answer == "$APP" ]] || ql_die "aborted; nothing was deleted"
  fi
  if [[ $DRY != 1 ]] && podman volume exists "$VOLUME"; then
    if [[ $(podman inspect --format '{{.State.Status}}' litellm-postgres 2>/dev/null || true) == running ]]; then
      "$REPO/scripts/backup.sh" --dest "$BACKUP_DIR" >/dev/null || ql_die "final backup failed; nothing was deleted"
    else
      ql_warn "litellm-postgres is not running: exporting the volume instead of a pg_dump"
      ql_backup_volume "$VOLUME" "$BACKUP_DIR" >/dev/null
      if podman secret exists litellm-salt-key >/dev/null 2>&1; then
        (umask 077 && printf 'LITELLM_SALT_KEY=%s\n' "$(podman secret inspect --showsecret --format '{{.SecretData}}' litellm-salt-key)" \
          >"$BACKUP_DIR/secrets-$(date +%Y%m%d-%H%M%S).env")
      fi
    fi
    ql_info "final backup written under $BACKUP_DIR (it holds the salt key: keep it private)"
  fi
  ql_uninstall_units "$APP" --purge
else
  ql_uninstall_units "$APP"
fi

if ((purge_images)); then
  for img in "${images[@]}"; do
    [[ -n $img ]] || continue
    users=$(podman ps -a --filter "ancestor=$img" --format '{{.Names}}' 2>/dev/null || true)
    if [[ -n $users ]]; then ql_warn "keeping image $img: used by ${users//$'\n'/ }"; continue; fi
    if grep -lx "Image=$img" "$QDIR"/*.container 2>/dev/null | grep -q .; then
      ql_warn "keeping image $img: another installed unit uses it ($(grep -lx "Image=$img" "$QDIR"/*.container | tr '\n' ' '))"
      continue
    fi
    podman image exists "$img" || continue
    if [[ $DRY == 1 ]]; then ql_info "[dry-run] would remove image $img"; continue; fi
    if podman rmi "$img" >/dev/null 2>&1; then ql_info "removed image $img"; else ql_warn "could not remove image $img"; fi
  done
fi
