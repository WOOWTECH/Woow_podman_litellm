#!/usr/bin/env bash
# =============================================================================
# WOOWTECH / Woow_podman_litellm - scripts/smoke-test.sh
# -----------------------------------------------------------------------------
# Post-deploy verification for the LiteLLM gateway stack running under Podman.
# 部署後驗證腳本：檢查 Podman 上的 LiteLLM 閘道堆疊是否真的可用。
#
# Works for BOTH deployment paths:
#   * compose  - docker-compose.yml driven by podman-compose / `podman compose`
#   * quadlet  - rootless Podman Quadlet systemd units under ~/.config/containers/systemd/
# The path is auto-detected (see detect_mode) or forced with --mode.
#
# DESIGN NOTES (read before editing)
# ---------------------------------
# * Dependency-light on purpose. It needs only: bash, podman, and the standard
#   coreutils. It does NOT need curl, jq, python or psql on the HOST.
#   - Every HTTP call is made by `podman exec`-ing the LiteLLM container's own
#     Python (the LiteLLM image ships python but NOT curl - this is why the k3s
#     acceptance suite is written in urllib, and why we do the same here).
#   - Every SQL query is made by `podman exec`-ing the Postgres container's psql.
# * The master key is read from the environment file (.env / litellm.env), never
#   from the command line, and it is NEVER printed, echoed or placed in argv.
#   It is handed to the container through a stdin-fed 0600 temp file which is
#   removed on exit (see push_master_key / cleanup).
# * NOTHING here was executed against a real Podman host. Treat every check as
#   "documented behaviour", and read a failure as "investigate", not "impossible".
#
# EXIT CODES / 結束代碼
#   0  every check passed        / 全數通過
#   1  at least one check failed / 至少一項失敗
#   2  usage or environment error (cannot even start) / 參數或環境錯誤
# =============================================================================

set -euo pipefail

# --- constants ---------------------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

PODMAN="${PODMAN:-podman}"
LITELLM_CTR="${LITELLM_CONTAINER:-litellm}"
PG_CTR="${POSTGRES_CONTAINER:-litellm-postgres}"

# In-container path used to hand the master key to python without argv exposure.
CTR_KEYFILE="/tmp/.litellm-smoke-key"

# --- colours (only when stdout is a terminal) --------------------------------
if [ -t 1 ]; then
  C_GREEN=$'\033[32m'; C_RED=$'\033[31m'; C_YEL=$'\033[33m'
  C_DIM=$'\033[2m';    C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
  C_GREEN=''; C_RED=''; C_YEL=''; C_DIM=''; C_BOLD=''; C_OFF=''
fi

# --- counters ----------------------------------------------------------------
CHECK_NO=0
PASS_N=0
FAIL_N=0
TOTAL_CHECKS=7

# --- bilingual output helpers ------------------------------------------------
hdr()  { printf '\n%s%s%s\n%s%s%s\n' "$C_BOLD" "$1" "$C_OFF" "$C_DIM" "$2" "$C_OFF"; }
note() { printf '  %s[NOTE]%s %s\n         %s%s%s\n' "$C_DIM" "$C_OFF" "$1" "$C_DIM" "$2" "$C_OFF"; }
warn() { printf '  %s[WARN]%s %s\n         %s\n' "$C_YEL" "$C_OFF" "$1" "$2"; }
die()  { printf '\n%s[ERROR]%s %s\n        %s\n\n' "$C_RED" "$C_OFF" "$1" "$2" >&2; exit 2; }

pass() {
  PASS_N=$((PASS_N + 1))
  printf '  %s[PASS]%s %d/%d %s\n             %s%s%s\n' \
    "$C_GREEN" "$C_OFF" "$CHECK_NO" "$TOTAL_CHECKS" "$1" "$C_DIM" "$2" "$C_OFF"
}
fail() {
  FAIL_N=$((FAIL_N + 1))
  printf '  %s[FAIL]%s %d/%d %s\n             %s\n' \
    "$C_RED" "$C_OFF" "$CHECK_NO" "$TOTAL_CHECKS" "$1" "$2"
  if [ -n "${3:-}" ]; then
    printf '             %s-> %s%s\n' "$C_DIM" "$3" "$C_OFF"
  fi
}
begin_check() { CHECK_NO=$((CHECK_NO + 1)); }

usage() {
  cat <<'EOF'
Usage / 用法:
  scripts/smoke-test.sh [--mode compose|quadlet|auto] [--env-file PATH] [-h|--help]

  --mode      Force the deployment path instead of auto-detecting.
              強制指定部署方式，不自動偵測。
  --env-file  Explicit path to the environment file holding LITELLM_MASTER_KEY.
              明確指定含有 LITELLM_MASTER_KEY 的環境檔路徑。

Environment overrides / 環境變數覆寫:
  PODMAN              podman binary (default: podman)
  LITELLM_CONTAINER   proxy container name    (default: litellm)
  POSTGRES_CONTAINER  database container name (default: litellm-postgres)

The master key is ALWAYS read from the environment file. It is never accepted on
the command line and never printed.
主金鑰一律從環境檔讀取，不接受命令列傳入，也絕不會被印出。
EOF
}

# --- argument parsing --------------------------------------------------------
MODE="auto"
ENV_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --mode)      MODE="${2:-}"; shift 2 ;;
    --mode=*)    MODE="${1#*=}"; shift ;;
    --env-file)  ENV_FILE="${2:-}"; shift 2 ;;
    --env-file=*) ENV_FILE="${1#*=}"; shift ;;
    -h|--help)   usage; exit 0 ;;
    *) die "Unknown argument: $1" "未知的參數：$1" ;;
  esac
done
case "$MODE" in
  auto|compose|quadlet) : ;;
  *) die "--mode must be one of: auto, compose, quadlet" "--mode 只能是 auto、compose 或 quadlet" ;;
esac

command -v "$PODMAN" >/dev/null 2>&1 \
  || die "podman not found in PATH." "找不到 podman，請確認已安裝並在 PATH 中。"

# =============================================================================
# Deployment-path detection
# 部署方式偵測
# -----------------------------------------------------------------------------
# Both paths use the SAME container names (litellm / litellm-postgres), so the
# container name alone cannot tell them apart. Discriminators, in order:
#   1. Quadlet injects Environment=PODMAN_SYSTEMD_UNIT=%n into every generated
#      unit, so a Quadlet-managed container carries PODMAN_SYSTEMD_UNIT in its
#      config env.
#   2. compose implementations stamp a project label
#      (io.podman.compose.project / com.docker.compose.project).
#   3. Fall back to asking systemd whether litellm.service is active.
# =============================================================================
detect_mode() {
  local env_blob="" label_blob=""
  env_blob="$("$PODMAN" inspect "$LITELLM_CTR" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null || true)"
  label_blob="$("$PODMAN" inspect "$LITELLM_CTR" --format '{{range $k,$v := .Config.Labels}}{{$k}}={{$v}}{{println}}{{end}}' 2>/dev/null || true)"

  if printf '%s' "$env_blob" | grep -q '^PODMAN_SYSTEMD_UNIT='; then
    printf 'quadlet'; return 0
  fi
  if printf '%s' "$label_blob" | grep -qiE '^(io\.podman|com\.docker)\.compose\.project='; then
    printf 'compose'; return 0
  fi
  if systemctl --user is-active --quiet litellm.service 2>/dev/null \
     || systemctl is-active --quiet litellm.service 2>/dev/null; then
    printf 'quadlet'; return 0
  fi
  # Last resort: a repo-local docker-compose.yml implies the compose path.
  if [ -f "${REPO_ROOT}/docker-compose.yml" ]; then
    printf 'compose'; return 0
  fi
  printf 'unknown'
}

if [ "$MODE" = "auto" ]; then
  MODE="$(detect_mode)"
  if [ "$MODE" = "unknown" ]; then
    MODE="compose"
    warn "Could not detect the deployment path; assuming 'compose'. Use --mode to override." \
         "無法偵測部署方式，先假設為 compose。可用 --mode 強制指定。"
  fi
fi

# =============================================================================
# Environment file resolution + secret reading
# 環境檔定位與機密讀取
# -----------------------------------------------------------------------------
# We deliberately do NOT `source` the env file: sourcing executes arbitrary
# shell, and a stray backtick in a password would run as a command. We parse
# KEY=VALUE lines instead.
# 刻意不使用 source 載入環境檔：source 會執行任意 shell 指令，
# 密碼中若含有反引號就會被當成指令執行。改為逐行解析 KEY=VALUE。
# =============================================================================
default_env_files() {
  local xdg="${XDG_CONFIG_HOME:-$HOME/.config}"
  if [ "$MODE" = "quadlet" ]; then
    printf '%s\n' "${xdg}/litellm/litellm.env" "${REPO_ROOT}/.env"
  else
    printf '%s\n' "${REPO_ROOT}/.env" "${xdg}/litellm/litellm.env"
  fi
}

if [ -z "$ENV_FILE" ]; then
  ENV_FILE="${LITELLM_ENV_FILE:-}"
fi
if [ -z "$ENV_FILE" ]; then
  while IFS= read -r candidate; do
    [ -f "$candidate" ] && { ENV_FILE="$candidate"; break; }
  done < <(default_env_files)
fi

# read_env KEY -> prints the value (or empty). Never logs it.
read_env() {
  local key="$1"
  [ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ] || return 0
  # Last definition wins, matching how shells and podman --env-file behave.
  sed -n -E "s/^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=[[:space:]]*(.*)$/\2/p" "$ENV_FILE" \
    | tail -n 1 \
    | sed -E 's/[[:space:]]*(#.*)?$//; s/\r$//; s/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/'
}

MASTER_KEY="$(read_env LITELLM_MASTER_KEY || true)"
PG_USER="$(read_env POSTGRES_USER || true)"; PG_USER="${PG_USER:-litellm}"
PG_DB="$(read_env POSTGRES_DB || true)";     PG_DB="${PG_DB:-litellm}"
LITELLM_PORT="$(read_env LITELLM_PORT || true)"; LITELLM_PORT="${LITELLM_PORT:-4000}"

# =============================================================================
# Master key handoff: stdin -> 0600 file inside the container.
# 主金鑰傳遞：透過 stdin 寫入容器內的 0600 檔案。
# The key never appears in argv (visible via `ps`), never in the host env, and
# never in this script's output. The file is removed by the EXIT trap.
# =============================================================================
KEY_PUSHED=0
cleanup() {
  if [ "$KEY_PUSHED" = "1" ]; then
    "$PODMAN" exec "$LITELLM_CTR" rm -f "$CTR_KEYFILE" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

push_master_key() {
  [ -n "$MASTER_KEY" ] || return 1
  if printf '%s' "$MASTER_KEY" \
      | "$PODMAN" exec -i "$LITELLM_CTR" sh -c "umask 077; cat > ${CTR_KEYFILE}" >/dev/null 2>&1; then
    KEY_PUSHED=1
    return 0
  fi
  return 1
}

# =============================================================================
# HTTP helper - runs inside the LiteLLM container using its bundled Python.
# HTTP 輔助函式 - 於 LiteLLM 容器內以內建 Python 執行。
# Prints: "<status><TAB><body first 600 chars, newlines collapsed>"
# status 0 means the request could not be made at all.
# =============================================================================
http_get() {
  local path="$1" auth="${2:-no}"
  "$PODMAN" exec -i "$LITELLM_CTR" python - "$path" "$auth" <<'PY' 2>/dev/null || printf '0\tEXEC_ERROR'
import os, sys, urllib.error, urllib.request

path = sys.argv[1]
auth = sys.argv[2]
key = ""
if auth == "yes":
    try:
        with open("/tmp/.litellm-smoke-key", "r") as fh:
            key = fh.read().strip()
    except Exception:
        # Fallback: the container already has the key in its own environment.
        key = os.environ.get("LITELLM_MASTER_KEY", "")

req = urllib.request.Request("http://localhost:4000" + path,
                             headers={"Accept": "application/json"})
if key:
    req.add_header("Authorization", "Bearer " + key)
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        status, raw = r.getcode(), r.read().decode("utf-8", "replace")
except urllib.error.HTTPError as e:
    status, raw = e.code, e.read().decode("utf-8", "replace")
except Exception as e:            # noqa: BLE001 - surface as a failed check
    status, raw = 0, "REQUEST_ERROR: %s" % e
sys.stdout.write("%d\t%s" % (status, " ".join(raw.split())[:600]))
PY
}

# Lists the model_name values returned by /v1/models, one per line, after a
# first line of "STATUS <code>". Parsing is done by python, not by grep.
http_models() {
  "$PODMAN" exec -i "$LITELLM_CTR" python - <<'PY' 2>/dev/null || printf 'STATUS 0\n'
import json, os, sys, urllib.error, urllib.request

key = ""
try:
    with open("/tmp/.litellm-smoke-key", "r") as fh:
        key = fh.read().strip()
except Exception:
    key = os.environ.get("LITELLM_MASTER_KEY", "")

req = urllib.request.Request("http://localhost:4000/v1/models",
                             headers={"Accept": "application/json"})
if key:
    req.add_header("Authorization", "Bearer " + key)
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        status, raw = r.getcode(), r.read().decode("utf-8", "replace")
except urllib.error.HTTPError as e:
    status, raw = e.code, e.read().decode("utf-8", "replace")
except Exception:                 # noqa: BLE001
    status, raw = 0, ""
print("STATUS %d" % status)
try:
    for m in json.loads(raw).get("data", []):
        if isinstance(m, dict) and m.get("id"):
            print(m["id"])
except Exception:                 # noqa: BLE001
    pass
PY
}

# =============================================================================
# psql helper - runs inside the Postgres container.
# psql 輔助函式 - 於 Postgres 容器內執行。
# The official postgres image allows local socket connections with `trust`, so
# no password is needed; we still try `--user postgres` as a fallback in case
# the exec user cannot reach the socket directory.
# =============================================================================
psql_q() {
  local sql="$1"
  "$PODMAN" exec "$PG_CTR" psql -U "$PG_USER" -d "$PG_DB" -tAc "$sql" 2>/dev/null \
    || "$PODMAN" exec --user postgres "$PG_CTR" psql -U "$PG_USER" -d "$PG_DB" -tAc "$sql" 2>/dev/null \
    || return 1
}

# =============================================================================
# Banner
# =============================================================================
printf '%s' "$C_BOLD"
cat <<'EOF'
=============================================================================
 Woow_podman_litellm - smoke test / 冒煙測試
=============================================================================
EOF
printf '%s' "$C_OFF"
printf '  Deployment path / 部署方式 : %s%s%s\n' "$C_BOLD" "$MODE" "$C_OFF"
if [ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ]; then
  printf '  Environment file / 環境檔  : %s\n' "$ENV_FILE"
else
  printf '  Environment file / 環境檔  : %s(not found / 找不到)%s\n' "$C_YEL" "$C_OFF"
fi
printf '  Containers / 容器          : %s , %s\n' "$LITELLM_CTR" "$PG_CTR"
printf '\n'

if [ -z "$MASTER_KEY" ]; then
  warn "LITELLM_MASTER_KEY was not found in the environment file - check 6 will fail." \
       "環境檔中找不到 LITELLM_MASTER_KEY，第 6 項檢查將會失敗。"
fi

hdr "Running checks / 開始檢查" "7 checks, in dependency order / 共 7 項，依相依順序執行"

# -----------------------------------------------------------------------------
# 1. Both containers exist AND are running.
#    兩個容器都存在且正在執行。
# -----------------------------------------------------------------------------
begin_check
RUNNING="$("$PODMAN" ps --format '{{.Names}}' 2>/dev/null || true)"
missing=""
for ctr in "$LITELLM_CTR" "$PG_CTR"; do
  printf '%s\n' "$RUNNING" | grep -qx -- "$ctr" || missing="${missing} ${ctr}"
done
if [ -z "$missing" ]; then
  pass "Both containers are running (${LITELLM_CTR}, ${PG_CTR})" \
       "兩個容器皆在執行中（${LITELLM_CTR}、${PG_CTR}）"
else
  fail "Container(s) not running:${missing}" \
       "下列容器未執行：${missing}" \
       "compose: podman-compose ps  |  quadlet: systemctl --user status litellm.service"
fi

# -----------------------------------------------------------------------------
# 2. Postgres healthcheck reports 'healthy'.
#    Postgres 健康檢查回報 healthy。
#    We read .State.Health.Status first; if it is not yet healthy we trigger one
#    on-demand probe with `podman healthcheck run` and re-read it.
# -----------------------------------------------------------------------------
health_status() {
  "$PODMAN" inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null || printf 'unknown'
}

begin_check
PG_HEALTH="$(health_status "$PG_CTR")"
if [ "$PG_HEALTH" != "healthy" ] && [ "$PG_HEALTH" != "none" ]; then
  "$PODMAN" healthcheck run "$PG_CTR" >/dev/null 2>&1 || true
  PG_HEALTH="$(health_status "$PG_CTR")"
fi
case "$PG_HEALTH" in
  healthy)
    pass "Postgres healthcheck is healthy (pg_isready)" \
         "Postgres 健康檢查為 healthy（pg_isready）" ;;
  none)
    fail "Postgres container has no healthcheck defined" \
         "Postgres 容器未定義健康檢查" \
         "compose: healthcheck: block  |  quadlet: HealthCmd= in litellm-postgres.container" ;;
  *)
    fail "Postgres healthcheck status = ${PG_HEALTH}" \
         "Postgres 健康檢查狀態為 ${PG_HEALTH}" \
         "podman inspect --format '{{json .State.Health}}' ${PG_CTR}" ;;
esac

# -----------------------------------------------------------------------------
# 3. LiteLLM healthcheck reports 'healthy'.
#    LiteLLM 健康檢查回報 healthy。
#    NOTE: on the Quadlet path the container uses a two-phase check - a startup
#    check against /health/readiness, then a steady-state check against
#    /health/liveliness. Podman reports "starting" until the startup check
#    succeeds, which can legitimately take up to ~10 minutes on a cold volume.
# -----------------------------------------------------------------------------
begin_check
LL_HEALTH="$(health_status "$LITELLM_CTR")"
if [ "$LL_HEALTH" != "healthy" ] && [ "$LL_HEALTH" != "none" ]; then
  "$PODMAN" healthcheck run "$LITELLM_CTR" >/dev/null 2>&1 || true
  LL_HEALTH="$(health_status "$LITELLM_CTR")"
fi
case "$LL_HEALTH" in
  healthy)
    pass "LiteLLM healthcheck is healthy" \
         "LiteLLM 健康檢查為 healthy" ;;
  starting)
    fail "LiteLLM healthcheck is still 'starting'" \
         "LiteLLM 健康檢查仍在 starting 狀態" \
         "First boot runs Prisma migrations; wait and re-run. / 首次啟動會執行 Prisma 遷移，請稍候再試。" ;;
  none)
    fail "LiteLLM container has no healthcheck defined" \
         "LiteLLM 容器未定義健康檢查" \
         "compose: healthcheck: block  |  quadlet: HealthCmd= in litellm.container" ;;
  *)
    fail "LiteLLM healthcheck status = ${LL_HEALTH}" \
         "LiteLLM 健康檢查狀態為 ${LL_HEALTH}" \
         "podman inspect --format '{{json .State.Health}}' ${LITELLM_CTR}" ;;
esac

# Hand the master key to the container now that we know it is up.
if [ -n "$MASTER_KEY" ]; then
  push_master_key || warn \
    "Could not stage the master key inside the container; falling back to its own env." \
    "無法將主金鑰寫入容器暫存檔，改用容器自身的環境變數。"
fi

# -----------------------------------------------------------------------------
# 4. GET /health/liveliness returns 200.
#    Process-alive probe only - it does NOT touch the database.
#    僅檢查行程存活，不會碰資料庫。
#    (Spelling really is "liveliness"; "/health/liveness" is a documented alias.)
# -----------------------------------------------------------------------------
begin_check
RESP="$(http_get /health/liveliness no || true)"
LIVE_STATUS="${RESP%%$'\t'*}"
if [ "$LIVE_STATUS" = "200" ]; then
  pass "GET /health/liveliness -> 200" \
       "GET /health/liveliness 回傳 200"
else
  fail "GET /health/liveliness -> ${LIVE_STATUS}" \
       "GET /health/liveliness 回傳 ${LIVE_STATUS}" \
       "$(printf '%s' "${RESP#*$'\t'}" | cut -c1-160)"
fi

# -----------------------------------------------------------------------------
# 5. GET /health/readiness returns 200.
#    THIS ONE TOUCHES THE DATABASE. LiteLLM returns 503 here when a DATABASE_URL
#    is configured but Postgres is unreachable, and the JSON body carries a "db"
#    field of "connected" / "disconnected" / "Not connected".
#    這一項會實際連線資料庫：DATABASE_URL 有設定但 Postgres 不可達時會回傳 503。
#    We assert on the STATUS CODE (per upstream guidance), and merely report the
#    db field as extra context.
# -----------------------------------------------------------------------------
begin_check
RESP="$(http_get /health/readiness no || true)"
READY_STATUS="${RESP%%$'\t'*}"
READY_BODY="${RESP#*$'\t'}"
DB_FIELD="$(printf '%s' "$READY_BODY" | sed -n -E 's/.*"db"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/p')"
if [ "$READY_STATUS" = "200" ]; then
  pass "GET /health/readiness -> 200 (db=${DB_FIELD:-unreported})" \
       "GET /health/readiness 回傳 200（db=${DB_FIELD:-未回報}）"
  if [ -n "$DB_FIELD" ] && [ "$DB_FIELD" != "connected" ]; then
    warn "Readiness is 200 but db field is '${DB_FIELD}'." \
         "readiness 為 200，但 db 欄位是 '${DB_FIELD}'。"
  fi
else
  fail "GET /health/readiness -> ${READY_STATUS} (db=${DB_FIELD:-unreported})" \
       "GET /health/readiness 回傳 ${READY_STATUS}（db=${DB_FIELD:-未回報}）" \
       "$(printf '%s' "$READY_BODY" | cut -c1-160)"
fi

# -----------------------------------------------------------------------------
# 6. GET /v1/models with the master key returns a NON-EMPTY model list that
#    contains every model_name declared in config/config.yaml.
#    以主金鑰呼叫 /v1/models，需回傳非空清單，且包含 config.yaml 宣告的所有模型。
#    Models added later through the Admin UI (STORE_MODEL_IN_DB=True) appear as
#    EXTRA entries; extras are fine, missing ones are not.
# -----------------------------------------------------------------------------
begin_check
MODELS_OUT="$(http_models || true)"
MODELS_STATUS="$(printf '%s\n' "$MODELS_OUT" | sed -n '1s/^STATUS //p')"
LISTED="$(printf '%s\n' "$MODELS_OUT" | tail -n +2 | sed '/^$/d')"
LISTED_N="$(printf '%s' "$LISTED" | grep -c . || true)"

CONFIG_YAML="${REPO_ROOT}/config/config.yaml"
EXPECTED=""
if [ -f "$CONFIG_YAML" ]; then
  # Only uncommented "- model_name: X" entries. Commented examples start with #.
  EXPECTED="$(sed -n -E 's/^[[:space:]]*-[[:space:]]*model_name:[[:space:]]*"?([^"#[:space:]]+)"?.*/\1/p' "$CONFIG_YAML" || true)"
fi

if [ "$MODELS_STATUS" != "200" ]; then
  fail "GET /v1/models -> ${MODELS_STATUS:-0}" \
       "GET /v1/models 回傳 ${MODELS_STATUS:-0}" \
       "401/403 usually means LITELLM_MASTER_KEY in the env file does not match the running proxy. / 401 或 403 通常代表環境檔中的主金鑰與執行中的代理不符。"
elif [ "${LISTED_N:-0}" -eq 0 ]; then
  fail "GET /v1/models -> 200 but the model list is EMPTY" \
       "GET /v1/models 回傳 200，但模型清單是空的" \
       "Check model_list in config/config.yaml and the mounted config path. / 請檢查 config/config.yaml 的 model_list 與掛載路徑。"
else
  MISSING=""
  if [ -n "$EXPECTED" ]; then
    while IFS= read -r want; do
      [ -n "$want" ] || continue
      printf '%s\n' "$LISTED" | grep -qx -- "$want" || MISSING="${MISSING} ${want}"
    done <<< "$EXPECTED"
  fi
  if [ -z "$MISSING" ]; then
    pass "GET /v1/models -> 200, ${LISTED_N} model(s), all config.yaml models present" \
         "GET /v1/models 回傳 200，共 ${LISTED_N} 個模型，config.yaml 中的模型全數存在"
    note "Listed: $(printf '%s' "$LISTED" | tr '\n' ' ')" \
         "已列出的模型如上。"
  else
    fail "GET /v1/models -> 200 but these config.yaml models are missing:${MISSING}" \
         "GET /v1/models 回傳 200，但缺少下列 config.yaml 模型：${MISSING}" \
         "Listed: $(printf '%s' "$LISTED" | tr '\n' ' ')"
  fi
fi

# -----------------------------------------------------------------------------
# 7. The database actually contains the LiteLLM schema.
#    資料庫確實建立了 LiteLLM 的資料表。
#    This is the check that catches the classic first-boot mistake of copying
#    DISABLE_SCHEMA_UPDATE=true from the k3s manifests onto an EMPTY volume:
#    the proxy then never runs `prisma migrate deploy`, no tables are created,
#    and every DB-backed feature silently fails.
#    這一項可抓出最典型的首次啟動錯誤：把 k3s 的 DISABLE_SCHEMA_UPDATE=true
#    照抄到全新空白磁碟區，導致 Prisma 遷移從未執行、資料表從未建立。
# -----------------------------------------------------------------------------
begin_check
TOTAL_TABLES="$(psql_q "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" || true)"
LITELLM_TABLES="$(psql_q "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE 'LiteLLM%';" || true)"
TOTAL_TABLES="$(printf '%s' "${TOTAL_TABLES:-}" | tr -d '[:space:]')"
LITELLM_TABLES="$(printf '%s' "${LITELLM_TABLES:-}" | tr -d '[:space:]')"

if [ -z "$TOTAL_TABLES" ]; then
  fail "Could not query Postgres (psql failed inside ${PG_CTR})" \
       "無法查詢 Postgres（在 ${PG_CTR} 內執行 psql 失敗）" \
       "podman exec -it ${PG_CTR} psql -U ${PG_USER} -d ${PG_DB}"
elif [ "${LITELLM_TABLES:-0}" -gt 0 ]; then
  pass "Database schema present: ${LITELLM_TABLES} LiteLLM_* table(s), ${TOTAL_TABLES} total in public" \
       "資料庫結構已建立：LiteLLM_* 資料表 ${LITELLM_TABLES} 張，public schema 共 ${TOTAL_TABLES} 張"
else
  fail "No LiteLLM_* tables found (public schema has ${TOTAL_TABLES} table(s))" \
       "找不到任何 LiteLLM_* 資料表（public schema 共 ${TOTAL_TABLES} 張）" \
       "Most likely DISABLE_SCHEMA_UPDATE=true was set against an empty volume. Set it to false and restart. / 最可能是對空白磁碟區設定了 DISABLE_SCHEMA_UPDATE=true，請改為 false 後重啟。"
fi

# -----------------------------------------------------------------------------
# Informational: is the published port reachable from the host?
# 附註資訊：主機端是否連得到已發佈的埠？
# Uses bash's /dev/tcp built-in so no curl/nc dependency is introduced.
# This is NOT a counted check - the compose path publishes on all interfaces
# while the Quadlet path publishes on 127.0.0.1 only, and either is valid.
# -----------------------------------------------------------------------------
if (exec 3<>"/dev/tcp/127.0.0.1/${LITELLM_PORT}") 2>/dev/null; then
  exec 3>&- 2>/dev/null || true
  note "Host port 127.0.0.1:${LITELLM_PORT} accepts TCP connections." \
       "主機埠 127.0.0.1:${LITELLM_PORT} 可接受 TCP 連線。"
else
  note "Host port 127.0.0.1:${LITELLM_PORT} did not accept a TCP connection (informational only)." \
       "主機埠 127.0.0.1:${LITELLM_PORT} 無法建立 TCP 連線（僅供參考，不列入計分）。"
fi

# =============================================================================
# Summary / 總結
# =============================================================================
printf '\n'
printf '%s-----------------------------------------------------------------------------%s\n' "$C_DIM" "$C_OFF"
if [ "$FAIL_N" -eq 0 ]; then
  printf ' %sRESULT: %d/%d checks passed.%s\n' "$C_GREEN" "$PASS_N" "$TOTAL_CHECKS" "$C_OFF"
  printf ' %s結果：%d/%d 項檢查通過。%s\n' "$C_GREEN" "$PASS_N" "$TOTAL_CHECKS" "$C_OFF"
  printf '%s-----------------------------------------------------------------------------%s\n\n' "$C_DIM" "$C_OFF"
  exit 0
else
  printf ' %sRESULT: %d passed, %d FAILED (of %d).%s\n' "$C_RED" "$PASS_N" "$FAIL_N" "$TOTAL_CHECKS" "$C_OFF"
  printf ' %s結果：通過 %d 項，失敗 %d 項（共 %d 項）。%s\n' "$C_RED" "$PASS_N" "$FAIL_N" "$TOTAL_CHECKS" "$C_OFF"
  printf '%s-----------------------------------------------------------------------------%s\n\n' "$C_DIM" "$C_OFF"
  exit 1
fi
