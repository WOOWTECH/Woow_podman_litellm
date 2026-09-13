#!/usr/bin/env bash
# scripts/backup.sh: back up the LiteLLM gateway into a new directory.
#
#   scripts/backup.sh [--dest DIR] [--no-secrets]
#
#   --dest DIR      parent directory (default ~/backups/litellm); a <timestamp>/ subdirectory is
#                   created in it and printed on stdout
#   --no-secrets    do not write secrets.env (then keep LITELLM_SALT_KEY somewhere else!)
#
# Contents (every file 0600, the directory 0700):
#   litellm-<ts>.dump (+ .sha256)   pg_dump -Fc of the litellm database (keys, teams, users,
#                                   spend, and the provider credentials ENCRYPTED with the salt)
#   secrets.env                     LITELLM_SALT_KEY and LITELLM_MASTER_KEY, plain text
#   salt.fingerprint                truncated sha256 of the salt key (not the key)
#   litellm.env, config.yaml        copies of the host settings and the model list
#
# THE DUMP IS USELESS WITHOUT LITELLM_SALT_KEY: every stored provider credential in it is
# ciphertext. Copy the whole directory (or at least secrets.env) OFF this host.
# Restore with scripts/restore.sh <dir>.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
APP=litellm
ENV_FILE=$HOME/.config/$APP/$APP.env
PG=litellm-postgres

dest=$HOME/backups/$APP secrets=1
while (($#)); do
  case $1 in
    --dest) dest=${2:?--dest needs a directory}; shift ;;
    --no-secrets) secrets=0 ;;
    -h | --help) sed -n '2,20p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
ql_require_rootless
[[ $(podman inspect --format '{{.State.Status}}' "$PG" 2>/dev/null || true) == running ]] \
  || ql_die "$PG is not running; start it (systemctl --user start litellm-postgres.service) and re-run"

ts=$(date +%Y%m%d-%H%M%S)
base=$dest/$ts out=$dest/$ts n=2
while [[ -e $out ]]; do out=$base-$n; n=$((n + 1)); done
(umask 077 && mkdir -p -- "$out") || ql_die "cannot create $out"
chmod 700 "$out"

dump=$out/litellm-$ts.dump
if ! (umask 077 && podman exec "$PG" pg_dump -U litellm -d litellm --format=custom --no-owner --no-privileges >"$dump.partial"); then
  rm -f -- "$dump.partial"
  ql_die "pg_dump failed; see: podman logs --tail 50 $PG"
fi
[[ -s $dump.partial ]] || { rm -f -- "$dump.partial"; ql_die "pg_dump produced an empty file"; }
mv -f -- "$dump.partial" "$dump"
(cd -- "$out" && umask 077 && sha256sum -- "${dump##*/}" >"${dump##*/}.sha256")
ql_info "database dump: $dump ($(du -h -- "$dump" | cut -f1))"

secret() { podman secret inspect --showsecret --format '{{.SecretData}}' "$1" 2>/dev/null; }
(
  umask 077
  if salt=$(secret litellm-salt-key); then
    printf 'sha256:%s\n' "$(printf 'woow-litellm-salt-v1:%s' "$salt" | sha256sum | cut -c1-16)" >"$out/salt.fingerprint"
  else
    ql_warn "secret litellm-salt-key not found: no fingerprint written"
  fi
  if ((secrets)); then
    {
      printf '# LiteLLM secrets from %s at %s. KEEP PRIVATE, keep a copy off this host.\n' "$(hostname)" "$ts"
      [[ -z ${salt:-} ]] || printf 'LITELLM_SALT_KEY=%s\n' "$salt"
      if master=$(secret litellm-master-key); then printf 'LITELLM_MASTER_KEY=%s\n' "$master"; fi
    } >"$out/secrets.env"
  fi
)
if [[ -f $ENV_FILE ]]; then install -m 600 -- "$ENV_FILE" "$out/${ENV_FILE##*/}"; fi
if [[ -f $HOME/.config/$APP/config.yaml ]]; then install -m 600 -- "$HOME/.config/$APP/config.yaml" "$out/config.yaml"; fi

if ((secrets)); then
  ql_warn "$out/secrets.env holds LITELLM_SALT_KEY and LITELLM_MASTER_KEY in plain text (0600)."
  ql_warn "Without the salt key the dump's provider credentials can never be decrypted: copy the directory OFF this host."
else
  ql_warn "--no-secrets: this backup does not contain LITELLM_SALT_KEY; the dump is only restorable with the salt key kept elsewhere."
fi
ql_info "backup complete: $out"
printf '%s\n' "$out"
