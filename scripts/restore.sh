#!/usr/bin/env bash
# =============================================================================
# WOOWTECH / Woow_podman_litellm - scripts/restore.sh
# -----------------------------------------------------------------------------
# Restores a backup archive produced by scripts/backup.sh into the running
# stack. THIS IS A DESTRUCTIVE OPERATION: the target database is dropped and
# recreated before the dump is loaded.
# 將 scripts/backup.sh 產生的備份還原到執行中的堆疊。
# 這是破壞性操作：目標資料庫會先被刪除並重建，然後才載入傾印檔。
#
# Works for BOTH deployment paths (compose and Quadlet). The path decides how
# the proxy is stopped and started; the database work is identical.
# 兩種部署方式（compose 與 Quadlet）皆適用。
#
# SEQUENCE / 流程
#   1. Parse and validate the archive, read its MANIFEST.txt
#   2. Compare the archive's LITELLM_SALT_KEY fingerprint with the target's
#      -> REFUSE on a confirmed mismatch (see the salt-key section below)
#   3. Confirmation prompt (type RESTORE)
#   4. Stop the litellm proxy ONLY - Postgres stays up, it is the restore target
#   5. DROP DATABASE ... WITH (FORCE) + CREATE DATABASE
#   6. pg_restore the dump
#   7. Start the litellm proxy again and report
#
# =============================================================================
# !!!!!!!!!!!!!!!!!!!!!!!!  THE SALT KEY RULE  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
#
# LiteLLM encrypts every provider credential it writes to Postgres with a key
# derived from LITELLM_SALT_KEY. A dump therefore contains only CIPHERTEXT.
# Restoring a dump into a stack whose LITELLM_SALT_KEY differs from the one in
# use when the dump was taken leaves you with a database full of credentials
# that can NEVER be decrypted again. LiteLLM does not crash on this - it logs a
# decryption error and returns None - so the damage is silent.
#
# 還原時若目標的 LITELLM_SALT_KEY 與備份當時不同，資料庫中的憑證將永遠無法解密。
# LiteLLM 不會因此崩潰，只會記錄解密錯誤並回傳 None，因此問題極容易被忽略。
#
# This script therefore compares the salt-key FINGERPRINT recorded in the
# archive's MANIFEST.txt against the fingerprint of the target's current
# LITELLM_SALT_KEY:
#   * fingerprints match          -> proceed
#   * fingerprints differ         -> REFUSE (override with --force-salt-mismatch)
#   * either fingerprint unknown  -> proceed only after a prominent warning and
#                                    an explicit confirmation
# =============================================================================
#
# EXIT CODES / 結束代碼
#   0  restore completed        / 還原完成
#   1  restore failed or refused / 還原失敗或被拒絕
#   2  usage or environment error / 參數或環境錯誤
#   3  aborted by the operator   / 操作者取消
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

PODMAN="${PODMAN:-podman}"
LITELLM_CTR="${LITELLM_CONTAINER:-litellm}"
PG_CTR="${POSTGRES_CONTAINER:-litellm-postgres}"
# --single-transaction: on an empty target this makes the restore all-or-nothing,
# so a failure leaves a clean empty database rather than a half-restored one.
PG_RESTORE_ARGS="${PG_RESTORE_ARGS:---no-owner --no-privileges --single-transaction}"

if [ -t 1 ]; then
  C_GREEN=$'\033[32m'; C_RED=$'\033[31m'; C_YEL=$'\033[33m'
  C_DIM=$'\033[2m';    C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
  C_GREEN=''; C_RED=''; C_YEL=''; C_DIM=''; C_BOLD=''; C_OFF=''
fi

step() { printf '\n%s==>%s %s\n    %s%s%s\n' "$C_BOLD" "$C_OFF" "$1" "$C_DIM" "$2" "$C_OFF"; }
ok()   { printf '  %s[ OK ]%s %s\n         %s%s%s\n' "$C_GREEN" "$C_OFF" "$1" "$C_DIM" "$2" "$C_OFF"; }
warn() { printf '  %s[WARN]%s %s\n         %s\n' "$C_YEL" "$C_OFF" "$1" "$2"; }
die()  { printf '\n%s[ERROR]%s %s\n        %s\n\n' "$C_RED" "$C_OFF" "$1" "$2" >&2; exit "${3:-1}"; }

usage() {
  cat <<'EOF'
Usage / 用法:
  scripts/restore.sh <backup.tar.gz> [options]

Options / 選項:
  --mode compose|quadlet|auto  Force the deployment path. 強制指定部署方式。
  --env-file PATH              Environment file to read POSTGRES_* /
                               LITELLM_SALT_KEY from. 指定環境檔。
  --restore-config             Also overwrite config/config.yaml from the
                               archive (OFF by default).
                               一併從壓縮檔覆寫 config/config.yaml（預設關閉）。
  --force-salt-mismatch        Proceed even when the archive's salt-key
                               fingerprint differs from the target's.
                               DANGEROUS - stored credentials become
                               permanently undecryptable.
                               即使鹽金鑰指紋不同也強制執行（危險）。
  --yes                        Skip the interactive confirmation prompt.
                               略過互動確認。
  -h, --help                   This help. 顯示說明。

DESTRUCTIVE / 破壞性操作:
  The target database is DROPPED and recreated. Everything currently in it -
  virtual keys, spend history, UI-managed models - is destroyed.
  目標資料庫會被刪除並重建，其中現有的虛擬金鑰、花費紀錄、
  由 UI 管理的模型設定全部會消失。
EOF
}

# --- arguments ---------------------------------------------------------------
ARCHIVE=""
MODE="auto"
ENV_FILE=""
RESTORE_CONFIG=0
FORCE_SALT=0
ASSUME_YES=0

while [ $# -gt 0 ]; do
  case "$1" in
    --mode)       MODE="${2:-}"; shift 2 ;;
    --mode=*)     MODE="${1#*=}"; shift ;;
    --env-file)   ENV_FILE="${2:-}"; shift 2 ;;
    --env-file=*) ENV_FILE="${1#*=}"; shift ;;
    --restore-config)      RESTORE_CONFIG=1; shift ;;
    --force-salt-mismatch) FORCE_SALT=1; shift ;;
    --yes|-y)     ASSUME_YES=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    -*) die "Unknown option: $1" "未知的選項：$1" 2 ;;
    *)  [ -z "$ARCHIVE" ] || die "Only one archive may be given." "只能指定一個備份檔。" 2
        ARCHIVE="$1"; shift ;;
  esac
done

[ -n "$ARCHIVE" ] || { usage; printf '\n'; die "No backup archive given." "未指定備份檔。" 2; }
[ -f "$ARCHIVE" ] || die "Backup archive not found: ${ARCHIVE}" "找不到備份檔：${ARCHIVE}" 2
ARCHIVE="$(cd -- "$(dirname -- "$ARCHIVE")" && pwd)/$(basename -- "$ARCHIVE")"

case "$MODE" in auto|compose|quadlet) : ;; *)
  die "--mode must be one of: auto, compose, quadlet" "--mode 只能是 auto、compose 或 quadlet" 2 ;;
esac

command -v "$PODMAN" >/dev/null 2>&1 || die "podman not found in PATH." "找不到 podman。" 2
command -v tar     >/dev/null 2>&1 || die "tar not found in PATH."    "找不到 tar。"   2

# --- deployment-path detection (same discriminators as smoke-test.sh) --------
detect_mode() {
  local env_blob label_blob
  env_blob="$("$PODMAN" inspect "$LITELLM_CTR" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null || true)"
  label_blob="$("$PODMAN" inspect "$LITELLM_CTR" --format '{{range $k,$v := .Config.Labels}}{{$k}}={{$v}}{{println}}{{end}}' 2>/dev/null || true)"
  printf '%s' "$env_blob"   | grep -q '^PODMAN_SYSTEMD_UNIT='                          && { printf 'quadlet'; return; }
  printf '%s' "$label_blob" | grep -qiE '^(io\.podman|com\.docker)\.compose\.project=' && { printf 'compose'; return; }
  if systemctl --user is-active --quiet litellm.service 2>/dev/null \
     || systemctl is-active --quiet litellm.service 2>/dev/null; then printf 'quadlet'; return; fi
  [ -f "${REPO_ROOT}/docker-compose.yml" ] && { printf 'compose'; return; }
  printf 'unknown'
}
[ "$MODE" = "auto" ] && MODE="$(detect_mode)"
[ "$MODE" = "unknown" ] && { MODE="compose"; warn \
  "Could not detect the deployment path; assuming 'compose'." \
  "無法偵測部署方式，先假設為 compose。"; }

# systemd scope for the Quadlet path: user units are the documented default,
# but the same unit files can be installed rootful under /etc/containers/systemd.
# Quadlet 路徑的 systemd 範圍：預設為使用者單元，但同樣的單元檔也可安裝為系統單元。
SYSTEMCTL_SCOPE=""
if [ "$MODE" = "quadlet" ]; then
  if systemctl --user cat litellm.service >/dev/null 2>&1; then
    SYSTEMCTL_SCOPE="--user"
  elif systemctl cat litellm.service >/dev/null 2>&1; then
    SYSTEMCTL_SCOPE=""
  else
    warn "litellm.service is not known to systemd; falling back to plain podman stop/start." \
         "systemd 中找不到 litellm.service，改用 podman stop/start。"
    MODE="compose"
  fi
fi

# --- environment file --------------------------------------------------------
if [ -z "$ENV_FILE" ]; then ENV_FILE="${LITELLM_ENV_FILE:-}"; fi
if [ -z "$ENV_FILE" ]; then
  XDG="${XDG_CONFIG_HOME:-$HOME/.config}"
  if [ "$MODE" = "quadlet" ]; then
    for c in "${XDG}/litellm/litellm.env" "${REPO_ROOT}/.env"; do [ -f "$c" ] && { ENV_FILE="$c"; break; }; done
  else
    for c in "${REPO_ROOT}/.env" "${XDG}/litellm/litellm.env"; do [ -f "$c" ] && { ENV_FILE="$c"; break; }; done
  fi
fi

# Parsed, never sourced. 逐行解析，絕不 source。
read_env() {
  local key="$1"
  [ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ] || return 0
  sed -n -E "s/^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=[[:space:]]*(.*)$/\2/p" "$ENV_FILE" \
    | tail -n 1 \
    | sed -E 's/[[:space:]]*(#.*)?$//; s/\r$//; s/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/'
}

PG_USER="$(read_env POSTGRES_USER || true)"; PG_USER="${PG_USER:-litellm}"
PG_DB="$(read_env POSTGRES_DB || true)";     PG_DB="${PG_DB:-litellm}"
SALT_KEY="$(read_env LITELLM_SALT_KEY || true)"

salt_fingerprint() {
  local key="$1" hash=""
  [ -n "$key" ] || { printf 'unknown'; return; }
  if command -v sha256sum >/dev/null 2>&1; then
    hash="$(printf 'woow-litellm-salt-v1:%s' "$key" | sha256sum | cut -d' ' -f1)"
  elif command -v shasum >/dev/null 2>&1; then
    hash="$(printf 'woow-litellm-salt-v1:%s' "$key" | shasum -a 256 | cut -d' ' -f1)"
  elif command -v openssl >/dev/null 2>&1; then
    hash="$(printf 'woow-litellm-salt-v1:%s' "$key" | openssl dgst -sha256 -r | cut -d' ' -f1)"
  else
    printf 'unknown'; return
  fi
  printf 'sha256:%s' "${hash:0:16}"
}
TARGET_FP="$(salt_fingerprint "$SALT_KEY")"

# =============================================================================
printf '%s' "$C_BOLD"
cat <<'EOF'
=============================================================================
 Woow_podman_litellm - restore / 還原
=============================================================================
EOF
printf '%s' "$C_OFF"
printf '  Archive / 備份檔           : %s\n' "$ARCHIVE"
printf '  Deployment path / 部署方式 : %s\n' "$MODE"
printf '  Environment file / 環境檔  : %s\n' "${ENV_FILE:-<none / 無>}"
printf '  Target database / 目標資料庫: %s@%s (%s)\n' "$PG_USER" "$PG_CTR" "$PG_DB"

# --- 1. unpack and read the manifest ----------------------------------------
step "1/7 Inspecting the archive / 檢查備份檔" "tar -xzf into a private temp dir"
WORKDIR="$(mktemp -d)"; chmod 700 "$WORKDIR"
cleanup() { rm -rf -- "$WORKDIR"; }
trap cleanup EXIT INT TERM

tar -xzf "$ARCHIVE" -C "$WORKDIR" \
  || die "Could not extract the archive." "無法解壓縮備份檔。" 1

MANIFEST="$(find "$WORKDIR" -maxdepth 3 -name MANIFEST.txt -type f | head -n 1)"
[ -n "$MANIFEST" ] || die \
  "MANIFEST.txt not found in the archive - is this a scripts/backup.sh archive?" \
  "壓縮檔中找不到 MANIFEST.txt，這是由 scripts/backup.sh 產生的備份嗎？" 1
STAGE="$(dirname -- "$MANIFEST")"

manifest_get() {
  sed -n -E "s/^${1}=(.*)$/\1/p" "$MANIFEST" | tail -n 1 | tr -d '\r'
}

BK_CREATED="$(manifest_get created_utc)"
BK_MODE="$(manifest_get deploy_mode)"
BK_DB="$(manifest_get postgres_db)"
BK_USER="$(manifest_get postgres_user)"
BK_IMAGE="$(manifest_get litellm_image)"
BK_TABLES="$(manifest_get litellm_tables)"
BK_FP="$(manifest_get salt_key_fingerprint)"
DUMP_FILE="${STAGE}/$(manifest_get dump_file)"

[ -f "$DUMP_FILE" ] || die "Dump file missing from the archive." "壓縮檔中缺少傾印檔。" 1

printf '\n'
printf '  Backup taken / 備份時間    : %s\n' "${BK_CREATED:-unknown}"
printf '  Backup deploy mode / 方式  : %s\n' "${BK_MODE:-unknown}"
printf '  Backup database / 資料庫   : %s (%s)\n' "${BK_DB:-unknown}" "${BK_USER:-unknown}"
printf '  Backup LiteLLM image / 映像: %s\n' "${BK_IMAGE:-unknown}"
printf '  LiteLLM tables in dump     : %s\n' "${BK_TABLES:-unknown}"
ok "Archive looks structurally valid." "備份檔結構看起來正常。"

if [ -n "$BK_DB" ] && [ "$BK_DB" != "$PG_DB" ]; then
  warn "Archive database name '${BK_DB}' differs from the target '${PG_DB}'." \
       "備份的資料庫名稱 '${BK_DB}' 與目標 '${PG_DB}' 不同。"
fi

# --- 2. salt-key gate --------------------------------------------------------
step "2/7 Salt-key check / 鹽金鑰檢查" \
     "LITELLM_SALT_KEY decrypts every stored provider credential."

SALT_UNVERIFIED=0
if [ -n "$BK_FP" ] && [ "$BK_FP" != "unknown" ] && [ "$TARGET_FP" != "unknown" ]; then
  if [ "$BK_FP" = "$TARGET_FP" ]; then
    ok "Salt-key fingerprint matches (${TARGET_FP})." \
       "鹽金鑰指紋一致（${TARGET_FP}）。"
  else
    printf '\n%s' "$C_RED"
    cat <<'EOF'
#############################################################################
# SALT KEY MISMATCH - REFUSING TO RESTORE
# 鹽金鑰不一致 - 拒絕還原
#############################################################################
EOF
    printf '%s' "$C_OFF"
    printf '  archive fingerprint / 備份指紋 : %s\n' "$BK_FP"
    printf '  target  fingerprint / 目標指紋 : %s\n\n' "$TARGET_FP"
    cat <<'EOF'
  The LITELLM_SALT_KEY currently configured on this host is NOT the one that
  was in use when this backup was taken. Every provider credential inside the
  dump is encrypted with the OLD key, and this stack would never be able to
  decrypt them. LiteLLM would not report an error loudly: it logs a decryption
  failure and returns None, so models simply stop working.

  本主機目前設定的 LITELLM_SALT_KEY 與備份當時的金鑰不同。
  傾印檔中的所有供應商憑證都是以「舊」金鑰加密，此堆疊將永遠無法解密。
  LiteLLM 不會明顯報錯，只會記錄解密失敗並回傳 None，模型就這樣默默失效。

  WHAT TO DO / 該怎麼做:
    1. Recover the original LITELLM_SALT_KEY from wherever you backed it up
       and put it back into the environment file, then re-run this script.
       從你的金鑰備份處取回原本的 LITELLM_SALT_KEY，寫回環境檔後重新執行。
    2. Only if the original key is genuinely lost, re-run with
       --force-salt-mismatch and accept that every stored provider credential
       must be re-entered by hand through the Admin UI afterwards.
       若原金鑰確實已遺失，才使用 --force-salt-mismatch，
       並接受之後必須在 Admin UI 手動重新輸入所有供應商憑證。
EOF
    if [ "$FORCE_SALT" -eq 1 ]; then
      printf '\n'
      warn "--force-salt-mismatch was given; continuing anyway." \
           "已指定 --force-salt-mismatch，仍將繼續執行。"
      SALT_UNVERIFIED=1
    else
      printf '\n'
      exit 1
    fi
  fi
else
  SALT_UNVERIFIED=1
  printf '\n%s' "$C_YEL"
  cat <<'EOF'
#############################################################################
# SALT KEY COULD NOT BE VERIFIED
# 無法驗證鹽金鑰
#############################################################################
EOF
  printf '%s' "$C_OFF"
  printf '  archive fingerprint / 備份指紋 : %s\n' "${BK_FP:-missing / 缺少}"
  printf '  target  fingerprint / 目標指紋 : %s\n\n' "$TARGET_FP"
  cat <<'EOF'
  This script cannot confirm that the LITELLM_SALT_KEY configured here is the
  same one that was in use when the backup was taken.

  IF IT IS DIFFERENT, every provider credential in the restored database will
  be PERMANENTLY UNDECRYPTABLE. The failure is silent - LiteLLM logs a
  decryption error and returns None rather than crashing.

  無法確認此處設定的 LITELLM_SALT_KEY 與備份當時是否相同。
  若不同，還原後資料庫中的所有供應商憑證將「永久無法解密」，
  而且不會有明顯錯誤，只會默默失效。

  Verify by hand before continuing / 請先手動確認再繼續。
EOF
  printf '\n'
fi

# --- 3. confirmation ---------------------------------------------------------
step "3/7 Confirmation / 確認" "This DESTROYS the current database. 這會摧毀目前的資料庫。"
printf '  The following will be DROPPED and rebuilt from the archive:\n'
printf '  下列內容將被刪除並以備份重建：\n'
printf '    - database %s on container %s\n' "$PG_DB" "$PG_CTR"
printf '    - all virtual keys, spend history and UI-managed models it contains\n'
printf '      其中所有虛擬金鑰、花費紀錄與由 UI 管理的模型設定\n'
if [ "$RESTORE_CONFIG" -eq 1 ]; then
  printf '    - %s (overwritten from the archive / 由備份覆寫)\n' "${REPO_ROOT}/config/config.yaml"
fi
printf '\n'

if [ "$ASSUME_YES" -eq 1 ]; then
  warn "--yes given; skipping the interactive prompt." "已指定 --yes，略過互動確認。"
  if [ "$SALT_UNVERIFIED" -eq 1 ]; then
    warn "Proceeding with an UNVERIFIED salt key at your own risk." \
         "在鹽金鑰未經驗證的情況下繼續，風險自負。"
  fi
else
  PROMPT_SRC="/dev/stdin"
  [ -r /dev/tty ] && PROMPT_SRC="/dev/tty"
  printf '  Type %sRESTORE%s to proceed, anything else to abort.\n' "$C_BOLD" "$C_OFF"
  printf '  輸入 %sRESTORE%s 以繼續，其他任何輸入皆會中止。\n' "$C_BOLD" "$C_OFF"
  printf '  > '
  ANSWER=""
  read -r ANSWER < "$PROMPT_SRC" || true
  if [ "$ANSWER" != "RESTORE" ]; then
    printf '\n'
    printf '  %sAborted by operator. Nothing was changed.%s\n' "$C_YEL" "$C_OFF"
    printf '  %s操作者已取消，未變更任何內容。%s\n\n' "$C_YEL" "$C_OFF"
    exit 3
  fi
fi

# --- 4. stop the proxy (NOT Postgres) ---------------------------------------
# Postgres must stay running: it is the restore target. Only the LiteLLM proxy
# is stopped, so it cannot hold connections open or write while we swap the DB.
# Postgres 必須保持執行（它就是還原目標），只停止 LiteLLM 代理，
# 避免它在我們抽換資料庫時佔用連線或寫入資料。
step "4/7 Stopping the LiteLLM proxy / 停止 LiteLLM 代理" \
     "Postgres stays up - it is the restore target. Postgres 保持執行。"

stop_litellm() {
  if [ "$MODE" = "quadlet" ]; then
    # shellcheck disable=SC2086
    systemctl $SYSTEMCTL_SCOPE stop litellm.service
  else
    "$PODMAN" stop "$LITELLM_CTR" >/dev/null
  fi
}
start_litellm() {
  if [ "$MODE" = "quadlet" ]; then
    # shellcheck disable=SC2086
    systemctl $SYSTEMCTL_SCOPE start litellm.service
  else
    "$PODMAN" start "$LITELLM_CTR" >/dev/null
  fi
}

if "$PODMAN" ps --format '{{.Names}}' 2>/dev/null | grep -qx -- "$LITELLM_CTR"; then
  stop_litellm || die "Failed to stop the LiteLLM proxy." "停止 LiteLLM 代理失敗。" 1
  ok "LiteLLM proxy stopped." "LiteLLM 代理已停止。"
else
  ok "LiteLLM proxy was not running." "LiteLLM 代理原本就未執行。"
fi

"$PODMAN" ps --format '{{.Names}}' 2>/dev/null | grep -qx -- "$PG_CTR" \
  || die "Postgres container '${PG_CTR}' is not running - cannot restore." \
         "Postgres 容器 '${PG_CTR}' 未執行，無法還原。" 1

# --- 5. drop + recreate ------------------------------------------------------
# DROP DATABASE ... WITH (FORCE) terminates any remaining backends. It requires
# PostgreSQL 13+, which postgres:16-alpine satisfies. We connect to the
# maintenance database 'postgres' because you cannot drop the database you are
# currently connected to.
# WITH (FORCE) 會強制中斷殘留連線，需 PostgreSQL 13 以上（postgres:16 符合）。
# 必須連到維護資料庫 postgres，因為無法刪除自己正連著的資料庫。
step "5/7 Recreating the database / 重建資料庫" \
     "DROP DATABASE \"${PG_DB}\" WITH (FORCE); CREATE DATABASE \"${PG_DB}\";"

psql_admin() {
  "$PODMAN" exec "$PG_CTR" psql -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 -tAc "$1" 2>&1 \
    || "$PODMAN" exec --user postgres "$PG_CTR" psql -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 -tAc "$1" 2>&1
}

if ! DROP_OUT="$(psql_admin "DROP DATABASE IF EXISTS \"${PG_DB}\" WITH (FORCE);")"; then
  printf '%s\n' "$DROP_OUT" >&2
  start_litellm || true
  die "DROP DATABASE failed - the proxy has been restarted, nothing was restored." \
      "DROP DATABASE 失敗，代理已重新啟動，未進行任何還原。" 1
fi
if ! CREATE_OUT="$(psql_admin "CREATE DATABASE \"${PG_DB}\" OWNER \"${PG_USER}\";")"; then
  printf '%s\n' "$CREATE_OUT" >&2
  die "CREATE DATABASE failed - the database is now MISSING. Recreate it manually before starting the proxy." \
      "CREATE DATABASE 失敗，資料庫目前不存在，請先手動重建再啟動代理。" 1
fi
ok "Database ${PG_DB} recreated (empty)." "資料庫 ${PG_DB} 已重建（目前為空）。"

# --- 6. restore --------------------------------------------------------------
step "6/7 Restoring the dump / 還原傾印檔" \
     "pg_restore ${PG_RESTORE_ARGS}"

RESTORE_RC=0
# shellcheck disable=SC2086
"$PODMAN" exec -i "$PG_CTR" \
  pg_restore -U "$PG_USER" -d "$PG_DB" $PG_RESTORE_ARGS \
  < "$DUMP_FILE" > "${WORKDIR}/pg_restore.log" 2>&1 || RESTORE_RC=$?

if [ "$RESTORE_RC" -ne 0 ]; then
  printf '\n%s---- pg_restore output / pg_restore 輸出 ----%s\n' "$C_DIM" "$C_OFF"
  tail -n 40 "${WORKDIR}/pg_restore.log" || true
  printf '%s--------------------------------------------%s\n' "$C_DIM" "$C_OFF"
  warn "pg_restore exited with code ${RESTORE_RC}." "pg_restore 結束代碼為 ${RESTORE_RC}。"
  warn "Retry hint: PG_RESTORE_ARGS='--no-owner --no-privileges' scripts/restore.sh ..." \
       "重試建議：設定 PG_RESTORE_ARGS='--no-owner --no-privileges' 後重跑（關閉單一交易模式）。"
fi

TABLES="$("$PODMAN" exec "$PG_CTR" psql -U "$PG_USER" -d "$PG_DB" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE 'LiteLLM%';" \
  2>/dev/null | tr -d '[:space:]' || true)"

if [ -n "$TABLES" ] && [ "$TABLES" -gt 0 ] 2>/dev/null; then
  ok "Restore verified: ${TABLES} LiteLLM_* table(s) present." \
     "還原已驗證：資料庫中有 ${TABLES} 張 LiteLLM_* 資料表。"
  RESTORE_OK=1
else
  warn "No LiteLLM_* tables found after restore." "還原後找不到任何 LiteLLM_* 資料表。"
  RESTORE_OK=0
fi

# --- optional config restore -------------------------------------------------
if [ "$RESTORE_CONFIG" -eq 1 ]; then
  if [ -f "${STAGE}/config.yaml" ]; then
    mkdir -p "${REPO_ROOT}/config"
    cp -- "${STAGE}/config.yaml" "${REPO_ROOT}/config/config.yaml"
    ok "config/config.yaml overwritten from the archive." "已從備份覆寫 config/config.yaml。"
    warn "On the Quadlet path the proxy reads ~/.config/litellm/config.yaml - copy it there too." \
         "Quadlet 路徑讀取的是 ~/.config/litellm/config.yaml，請一併複製過去。"
  else
    warn "Archive contains no config.yaml; nothing to restore." "備份中沒有 config.yaml，無需還原。"
  fi
else
  if [ -f "${STAGE}/config.yaml" ]; then
    printf '  %s[NOTE]%s The archive also contains config.yaml. Pass --restore-config to apply it.\n' "$C_DIM" "$C_OFF"
    printf '         %s備份中也含有 config.yaml，加上 --restore-config 才會套用。%s\n' "$C_DIM" "$C_OFF"
  fi
fi

# --- 7. restart --------------------------------------------------------------
step "7/7 Restarting the LiteLLM proxy / 重新啟動 LiteLLM 代理" \
     "First readiness can take a while - the proxy reconnects and validates. 首次就緒需要一些時間。"
start_litellm || die "Failed to start the LiteLLM proxy." "啟動 LiteLLM 代理失敗。" 1
ok "LiteLLM proxy started." "LiteLLM 代理已啟動。"

# =============================================================================
printf '\n%s-----------------------------------------------------------------------------%s\n' "$C_DIM" "$C_OFF"
if [ "${RESTORE_OK:-0}" -eq 1 ] && [ "$RESTORE_RC" -eq 0 ]; then
  printf ' %sRESTORE COMPLETE.%s\n' "$C_GREEN" "$C_OFF"
  printf ' %s還原完成。%s\n' "$C_GREEN" "$C_OFF"
  FINAL_RC=0
else
  printf ' %sRESTORE FINISHED WITH PROBLEMS - review the output above.%s\n' "$C_YEL" "$C_OFF"
  printf ' %s還原過程有問題，請檢視上方輸出。%s\n' "$C_YEL" "$C_OFF"
  FINAL_RC=1
fi
printf '%s-----------------------------------------------------------------------------%s\n' "$C_DIM" "$C_OFF"
printf '\n Next / 接下來:\n'
printf '   scripts/smoke-test.sh        # verify the restored stack / 驗證還原後的堆疊\n'
if [ "$SALT_UNVERIFIED" -eq 1 ]; then
  printf '\n'
  printf ' %sThe salt key was not verified. If stored provider credentials do not work,%s\n' "$C_YEL" "$C_OFF"
  printf ' %sthe LITELLM_SALT_KEY differs from the backup and they must be re-entered.%s\n' "$C_YEL" "$C_OFF"
  printf ' %s鹽金鑰未經驗證。若供應商憑證無法使用，代表金鑰與備份不同，必須重新輸入。%s\n' "$C_YEL" "$C_OFF"
fi
printf '\n'
exit "$FINAL_RC"
