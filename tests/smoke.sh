#!/usr/bin/env bash
# tests/smoke.sh: post-install checks of a running LiteLLM gateway. Read-only and free: no
# completion is requested, so nothing is billed upstream.
#
#   tests/smoke.sh            exit 0 only when every check passes
#   SMOKE_FORCE_FAIL=1 ...    fail on purpose (exercises scripts/upgrade.sh's rollback)
#
# Reads bind/port from ~/.config/litellm/litellm.env. The master key is read from its podman
# secret into a variable and handed to curl on stdin (-K -): never printed, never in argv.
# shellcheck source-path=SCRIPTDIR
set -uo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=../scripts/lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
export QL_LOG_PREFIX=smoke
APP=litellm
ENV_FILE=$HOME/.config/$APP/$APP.env

pass=0 fail=0
ok() { printf '  PASS  %s\n' "$*"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n' "$*"; fail=$((fail + 1)); }
check() { # check <description> <command...>
  local d=$1
  shift
  if "$@"; then ok "$d"; else bad "$d"; fi
}

[[ -f $ENV_FILE ]] || { echo "smoke: $ENV_FILE not found (not installed?)" >&2; exit 1; }
ql_env_load "$ENV_FILE"
BIND=$(ql_env_get LITELLM_BIND)
PORT=$(ql_env_get LITELLM_PORT)
BASE=http://$BIND:$PORT

code() { curl -s -o /dev/null -w '%{http_code}' -m 15 "$@" 2>/dev/null || true; }
health() { podman inspect --format '{{.State.Health.Status}}' "$1" 2>/dev/null; }
# with_key <curl args...>: the Authorization header comes from stdin, not argv
with_key() {
  local key
  key=$(podman secret inspect --showsecret --format '{{.SecretData}}' litellm-master-key 2>/dev/null) || return 1
  printf 'header = "Authorization: Bearer %s"\n' "$key" | curl -s -m 15 -K - "$@" 2>/dev/null
}

echo "== LiteLLM at $BASE"
check "unit litellm-postgres.service is active" systemctl --user is-active --quiet litellm-postgres.service
check "unit litellm.service is active" systemctl --user is-active --quiet litellm.service
check "litellm-postgres health is healthy" test "$(health litellm-postgres)" = healthy
check "litellm health is healthy" test "$(health litellm)" = healthy
check "GET /health/liveliness -> 200" test "$(code "$BASE/health/liveliness")" = 200
check "GET /health/readiness -> 200 (database connected)" test "$(code "$BASE/health/readiness")" = 200
check "GET /v1/models without a key -> 401" test "$(code "$BASE/v1/models")" = 401

models=$(with_key "$BASE/v1/models" || true)
mapfile -t want < <(sed -n 's/^[[:space:]]*- model_name:[[:space:]]*//p' "$REPO/config/config.yaml")
got=$(grep -oE '"id"[[:space:]]*:[[:space:]]*"[^"]*"' <<<"$models" | sed -E 's/.*"([^"]*)"$/\1/' | sort | tr '\n' ' ')
missing=()
for m in "${want[@]}"; do [[ " $got" == *" $m "* ]] || missing+=("$m"); done
check "GET /v1/models with the master key lists the ${#want[@]} config.yaml models (got: ${got:-nothing})" \
  test "${#want[@]}" -gt 0 -a "${#missing[@]}" -eq 0

listeners=$(ss -ltnH "sport = :$PORT" 2>/dev/null | awk '{print $4}' | sort -u | tr '\n' ' ')
check "port $PORT listens only on $BIND:$PORT (got: ${listeners:-none})" test "${listeners% }" = "$BIND:$PORT"

# Least privilege: Postgres sees its password only as a file, and none of the proxy's keys.
pg_env=$(podman inspect --format '{{range .Config.Env}}{{println .}}{{end}}' litellm-postgres 2>/dev/null | cut -d= -f1)
check "litellm-postgres env has POSTGRES_PASSWORD_FILE" grep -qx POSTGRES_PASSWORD_FILE <<<"$pg_env"
check "litellm-postgres env has no POSTGRES_PASSWORD, OPENROUTER_API_KEY or LITELLM_*" \
  test "$(grep -cE '^(POSTGRES_PASSWORD|OPENROUTER_API_KEY|LITELLM_.*)$' <<<"$pg_env")" = 0
units=$(systemctl --user cat litellm.service litellm-postgres.service 2>/dev/null || true)
check "no plaintext password, key or credentialed URL in the units" \
  test "$(grep -ciE '(PASSWORD|MASTER_KEY|SALT_KEY|API_KEY|DATABASE_URL)=|://[^:/@ ]+:[^@ ]+@' <<<"$units")" = 0

if [[ ${SMOKE_FORCE_FAIL:-0} == 1 ]]; then bad "SMOKE_FORCE_FAIL=1 (forced failure)"; fi
echo "== $pass passed, $fail failed"
((fail == 0))
