#!/usr/bin/env bash
# =============================================================================
# WOOWTECH / Woow_podman_litellm - scripts/backup.sh
# -----------------------------------------------------------------------------
# Creates a timestamped, self-describing backup archive containing:
#   * a pg_dump of the LiteLLM database (PostgreSQL custom format)
#   * a copy of config/config.yaml
#   * a MANIFEST.txt describing the stack the backup came from
# 建立帶時間戳記的備份壓縮檔，內含資料庫傾印、config.yaml 以及描述檔。
#
# Works for BOTH deployment paths (compose and Quadlet); the path is only used
# to locate the environment file and to label the manifest, because both paths
# use the same container names.
# 兩種部署方式（compose 與 Quadlet）皆適用。
#
# =============================================================================
# !!!!!!!!!!!!!!!!!!!!!!!!!!!  READ THIS  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
#
# THE DUMP IS NOT SELF-SUFFICIENT. IT IS USELESS WITHOUT LITELLM_SALT_KEY.
# 這份傾印檔本身並不完整：沒有 LITELLM_SALT_KEY 就毫無用處。
#
# LiteLLM encrypts every provider credential it stores in Postgres (OpenRouter
# keys, callback credentials, MCP env vars) with a key derived from
# LITELLM_SALT_KEY. The dump therefore contains only CIPHERTEXT. Restoring this
# archive onto a stack whose LITELLM_SALT_KEY differs from the one that was in
# use when the dump was taken produces a database whose stored credentials can
# never be decrypted again - LiteLLM logs a decryption error and silently
# returns None, so the failure is quiet and easy to miss.
#
# Therefore:
#   * BACK UP LITELLM_SALT_KEY SEPARATELY, in a password manager or a secrets
#     vault - somewhere durable, and somewhere that is NOT this git repository
#     and NOT this backup archive.
#   * LITELLM_SALT_KEY is set ONCE and NEVER rotated. Rotating it has the same
#     effect as losing it.
#   * This script deliberately does NOT copy .env / litellm.env into the archive.
#     Backups get copied around; secrets should not ride along with them.
#
# 中文重點：
#   * LiteLLM 會用 LITELLM_SALT_KEY 加密所有存進 Postgres 的供應商憑證，
#     傾印檔裡只有密文。
#   * 請把 LITELLM_SALT_KEY 另外妥善保管（密碼管理器／金鑰保險庫），
#     絕對不要放進這個 git 倉庫，也不要放進備份壓縮檔。
#   * LITELLM_SALT_KEY 只能設定一次、永不輪替；輪替等同於遺失。
#   * 本腳本刻意不會把 .env / litellm.env 放進備份檔。
#
# What IS stored is a truncated SHA-256 FINGERPRINT of the salt key (16 hex
# chars, domain-separated). A fingerprint is not the key and cannot be turned
# back into one; it exists solely so restore.sh can refuse to restore into a
# stack with a different salt key.
# 備份檔中只會存放主鹽金鑰的截斷雜湊指紋（16 個十六進位字元），
# 指紋無法還原成金鑰，僅用於讓 restore.sh 偵測金鑰是否被更換。
# =============================================================================
#
# EXIT CODES / 結束代碼
#   0  backup written successfully / 備份成功
#   1  backup failed               / 備份失敗
#   2  usage or environment error  / 參數或環境錯誤
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

PODMAN="${PODMAN:-podman}"
LITELLM_CTR="${LITELLM_CONTAINER:-litellm}"
PG_CTR="${POSTGRES_CONTAINER:-litellm-postgres}"
BACKUP_DIR="${BACKUP_DIR:-${REPO_ROOT}/backups}"

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
  scripts/backup.sh [--mode compose|quadlet|auto] [--env-file PATH]
                    [--out DIR] [-h|--help]

  --mode      Force the deployment path instead of auto-detecting.
              強制指定部署方式，不自動偵測。
  --env-file  Environment file to read POSTGRES_USER / POSTGRES_DB /
              LITELLM_SALT_KEY from.
              指定要讀取的環境檔。
  --out DIR   Output directory (default: <repo>/backups).
              輸出目錄（預設為 <repo>/backups）。

Produces: <out>/litellm-backup-YYYYmmdd-HHMMSS.tar.gz (mode 0600)
產出：<out>/litellm-backup-YYYYmmdd-HHMMSS.tar.gz（權限 0600）

REMINDER / 提醒: the archive contains credentials encrypted with
LITELLM_SALT_KEY. Back that key up separately and securely - never in this repo.
壓縮檔內的憑證是用 LITELLM_SALT_KEY 加密的，請另外安全保管該金鑰。
EOF
}

# --- arguments ---------------------------------------------------------------
MODE="auto"
ENV_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --mode)       MODE="${2:-}"; shift 2 ;;
    --mode=*)     MODE="${1#*=}"; shift ;;
    --env-file)   ENV_FILE="${2:-}"; shift 2 ;;
    --env-file=*) ENV_FILE="${1#*=}"; shift ;;
    --out)        BACKUP_DIR="${2:-}"; shift 2 ;;
    --out=*)      BACKUP_DIR="${1#*=}"; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) die "Unknown argument: $1" "未知的參數：$1" 2 ;;
  esac
done
case "$MODE" in auto|compose|quadlet) : ;; *)
  die "--mode must be one of: auto, compose, quadlet" "--mode 只能是 auto、compose 或 quadlet" 2 ;;
esac

command -v "$PODMAN" >/dev/null 2>&1 \
  || die "podman not found in PATH." "找不到 podman，請確認已安裝並在 PATH 中。" 2
command -v tar >/dev/null 2>&1 \
  || die "tar not found in PATH." "找不到 tar。" 2

# --- deployment-path detection (same discriminators as smoke-test.sh) --------
detect_mode() {
  local env_blob label_blob
  env_blob="$("$PODMAN" inspect "$LITELLM_CTR" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null || true)"
  label_blob="$("$PODMAN" inspect "$LITELLM_CTR" --format '{{range $k,$v := .Config.Labels}}{{$k}}={{$v}}{{println}}{{end}}' 2>/dev/null || true)"
  printf '%s' "$env_blob"   | grep -q '^PODMAN_SYSTEMD_UNIT='                        && { printf 'quadlet'; return; }
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

# --- environment file --------------------------------------------------------
# Parsed line by line, never `source`d (a backtick in a password must not run).
# 逐行解析，絕不 source（密碼中的反引號不可被執行）。
if [ -z "$ENV_FILE" ]; then ENV_FILE="${LITELLM_ENV_FILE:-}"; fi
if [ -z "$ENV_FILE" ]; then
  XDG="${XDG_CONFIG_HOME:-$HOME/.config}"
  if [ "$MODE" = "quadlet" ]; then
    for c in "${XDG}/litellm/litellm.env" "${REPO_ROOT}/.env"; do
      [ -f "$c" ] && { ENV_FILE="$c"; break; }
    done
  else
    for c in "${REPO_ROOT}/.env" "${XDG}/litellm/litellm.env"; do
      [ -f "$c" ] && { ENV_FILE="$c"; break; }
    done
  fi
fi

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

# --- salt-key fingerprint ----------------------------------------------------
# A truncated, domain-separated SHA-256 of the salt key. NOT the key. Used only
# so restore.sh can detect "you are restoring into a stack with a different
# salt key" and refuse.
# 主鹽金鑰的截斷雜湊（含網域分隔字串）。這不是金鑰本身，
# 僅供 restore.sh 判斷「還原目標的鹽金鑰不同」並拒絕執行。
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
SALT_FP="$(salt_fingerprint "$SALT_KEY")"

# =============================================================================
printf '%s' "$C_BOLD"
cat <<'EOF'
=============================================================================
 Woow_podman_litellm - backup / 備份
=============================================================================
EOF
printf '%s' "$C_OFF"
printf '  Deployment path / 部署方式 : %s\n' "$MODE"
printf '  Environment file / 環境檔  : %s\n' "${ENV_FILE:-<none / 無>}"
printf '  Database / 資料庫          : %s@%s (%s)\n' "$PG_USER" "$PG_CTR" "$PG_DB"
printf '  Output dir / 輸出目錄      : %s\n' "$BACKUP_DIR"

if [ "$SALT_FP" = "unknown" ]; then
  warn "LITELLM_SALT_KEY could not be read - the manifest will not carry a fingerprint." \
       "無法讀取 LITELLM_SALT_KEY，描述檔將不會含有指紋。"
  warn "restore.sh will then be unable to verify the salt key and will only warn." \
       "屆時 restore.sh 無法驗證鹽金鑰，只能發出警告。"
fi

# --- preflight ---------------------------------------------------------------
step "Preflight / 前置檢查" "Confirming the database container is up. 確認資料庫容器已啟動。"
"$PODMAN" ps --format '{{.Names}}' 2>/dev/null | grep -qx -- "$PG_CTR" \
  || die "Postgres container '${PG_CTR}' is not running." \
         "Postgres 容器 '${PG_CTR}' 未在執行中。" 1
ok "Postgres container is running." "Postgres 容器正在執行。"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR" 2>/dev/null || true

TS="$(date -u +%Y%m%d-%H%M%S)"
STAGE_NAME="litellm-backup-${TS}"
ARCHIVE="${BACKUP_DIR}/${STAGE_NAME}.tar.gz"

WORKDIR="$(mktemp -d)"
STAGE="${WORKDIR}/${STAGE_NAME}"
mkdir -p "$STAGE"
chmod 700 "$WORKDIR" "$STAGE"
cleanup() { rm -rf -- "$WORKDIR"; }
trap cleanup EXIT INT TERM

# --- 1. pg_dump --------------------------------------------------------------
# Custom format (-Fc): compressed, and pg_restore can reorder/select from it.
# --no-owner / --no-privileges keep the dump portable across users and roles.
# 使用自訂格式（-Fc）：已壓縮，且 pg_restore 可選擇性還原。
step "1/3 Dumping the database / 傾印資料庫" \
     "podman exec ${PG_CTR} pg_dump -Fc -U ${PG_USER} -d ${PG_DB}"
if ! "$PODMAN" exec "$PG_CTR" \
      pg_dump -U "$PG_USER" -d "$PG_DB" --format=custom --no-owner --no-privileges \
      > "${STAGE}/database.dump" 2>"${WORKDIR}/pg_dump.err"; then
  printf '%s\n' "$(cat "${WORKDIR}/pg_dump.err" 2>/dev/null || true)" >&2
  die "pg_dump failed." "pg_dump 執行失敗。" 1
fi

DUMP_BYTES="$(wc -c < "${STAGE}/database.dump" | tr -d '[:space:]')"
[ "${DUMP_BYTES:-0}" -gt 0 ] || die "pg_dump produced an empty file." "pg_dump 產生了空檔案。" 1
# PostgreSQL custom-format dumps begin with the magic string "PGDMP".
if [ "$(head -c 5 "${STAGE}/database.dump")" != "PGDMP" ]; then
  warn "Dump does not start with the expected PGDMP magic - inspect it before trusting it." \
       "傾印檔開頭不是預期的 PGDMP 標記，請先檢查再使用。"
fi
ok "Database dumped (${DUMP_BYTES} bytes)." "資料庫傾印完成（${DUMP_BYTES} 位元組）。"

# Record how many LiteLLM tables were captured - a dump with zero LiteLLM_*
# tables means the schema was never created (classic DISABLE_SCHEMA_UPDATE=true
# on an empty volume) and the backup is not worth much.
# 記錄擷取到的 LiteLLM 資料表數量；若為 0，代表結構從未建立，這份備份意義不大。
TABLE_COUNT="$("$PODMAN" exec "$PG_CTR" psql -U "$PG_USER" -d "$PG_DB" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE 'LiteLLM%';" \
  2>/dev/null | tr -d '[:space:]' || true)"
if [ -n "$TABLE_COUNT" ] && [ "$TABLE_COUNT" = "0" ]; then
  warn "The database contains NO LiteLLM_* tables. Backing up an empty schema." \
       "資料庫中沒有任何 LiteLLM_* 資料表，這份備份的內容是空的結構。"
fi

# --- 2. config -------------------------------------------------------------
step "2/3 Capturing configuration / 收集設定檔" "config/config.yaml"
CONFIG_SRC="${REPO_ROOT}/config/config.yaml"
if [ -f "$CONFIG_SRC" ]; then
  cp -- "$CONFIG_SRC" "${STAGE}/config.yaml"
  ok "config/config.yaml copied." "已複製 config/config.yaml。"
else
  warn "config/config.yaml not found at ${CONFIG_SRC} - archive will omit it." \
       "在 ${CONFIG_SRC} 找不到 config/config.yaml，壓縮檔將不包含它。"
fi

# --- 3. manifest + archive ---------------------------------------------------
step "3/3 Writing manifest and archive / 寫入描述檔並打包" "$ARCHIVE"

image_of() { "$PODMAN" inspect "$1" --format '{{.ImageName}}' 2>/dev/null || printf 'unknown'; }

cat > "${STAGE}/MANIFEST.txt" <<EOF
# Woow_podman_litellm backup manifest
# 備份描述檔。restore.sh 會解析此檔案。
#
# This file contains NO secrets. salt_key_fingerprint is a truncated,
# domain-separated SHA-256 of LITELLM_SALT_KEY; it cannot be reversed into the
# key and exists only so restore.sh can detect a salt-key mismatch.
# 本檔案不含任何機密。salt_key_fingerprint 為 LITELLM_SALT_KEY 的截斷雜湊，
# 無法還原成金鑰，僅供 restore.sh 偵測金鑰是否不一致。
backup_format=1
created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
created_by=scripts/backup.sh
deploy_mode=${MODE}
host=$(uname -n 2>/dev/null || printf 'unknown')
podman_version=$("$PODMAN" --version 2>/dev/null | tr -d '\r' || printf 'unknown')
litellm_container=${LITELLM_CTR}
litellm_image=$(image_of "$LITELLM_CTR")
postgres_container=${PG_CTR}
postgres_image=$(image_of "$PG_CTR")
postgres_user=${PG_USER}
postgres_db=${PG_DB}
litellm_tables=${TABLE_COUNT:-unknown}
dump_file=database.dump
dump_format=custom
dump_bytes=${DUMP_BYTES}
config_file=$( [ -f "${STAGE}/config.yaml" ] && printf 'config.yaml' || printf 'none' )
salt_key_fingerprint=${SALT_FP}
EOF

chmod 600 "${STAGE}/MANIFEST.txt" "${STAGE}/database.dump" 2>/dev/null || true
[ -f "${STAGE}/config.yaml" ] && chmod 600 "${STAGE}/config.yaml" 2>/dev/null || true

tar -czf "$ARCHIVE" -C "$WORKDIR" "$STAGE_NAME"
chmod 600 "$ARCHIVE"

# --- size --------------------------------------------------------------------
SIZE_H="$(du -h -- "$ARCHIVE" 2>/dev/null | cut -f1 || true)"
SIZE_B="$(wc -c < "$ARCHIVE" | tr -d '[:space:]')"
ok "Archive written: ${ARCHIVE}" "壓縮檔已寫入：${ARCHIVE}"
ok "Size / 大小: ${SIZE_H:-?} (${SIZE_B} bytes / 位元組)" "檔案權限已設為 0600。"

printf '\n%s' "$C_BOLD"
cat <<'EOF'
-----------------------------------------------------------------------------
 REMEMBER / 請記住
-----------------------------------------------------------------------------
EOF
printf '%s' "$C_OFF"
cat <<'EOF'
 * This archive contains provider credentials ENCRYPTED with LITELLM_SALT_KEY.
   Without that key the archive cannot restore a working gateway.
   本壓縮檔中的供應商憑證是以 LITELLM_SALT_KEY 加密的；
   沒有該金鑰就無法還原出可用的閘道。

 * Back LITELLM_SALT_KEY up SEPARATELY and SECURELY (password manager / vault).
   Never commit it, never put it in this archive, never rotate it.
   請將 LITELLM_SALT_KEY 另外安全保管，切勿提交進版本庫、
   切勿放進壓縮檔、切勿輪替。

 * The archive also contains every virtual key row and spend record. Treat the
   .tar.gz itself as a secret: it is chmod 0600 and backups/ is git-ignored.
   壓縮檔也含有所有虛擬金鑰與花費紀錄，請視同機密看待。

 * Restore with / 還原指令:
     scripts/restore.sh <this-file>.tar.gz
EOF
printf '\n'
exit 0
