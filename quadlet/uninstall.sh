#!/usr/bin/env bash
# =============================================================================
# uninstall.sh - remove the WOOWTECH LiteLLM gateway Quadlet installation.
#
# 移除 WOOWTECH LiteLLM 閘道器的 Podman Quadlet 安裝。
#
# Repo: https://github.com/WOOWTECH/Woow_podman_litellm
#
# DEFAULT BEHAVIOUR IS NON-DESTRUCTIVE / 預設不會刪除任何資料
#   Running this script with no flags:
#     - stops litellm.service, litellm-wait-postgres.service,
#       litellm-postgres.service and the generated volume/network units
#     - removes the four Quadlet unit files from ~/.config/containers/systemd/
#     - removes litellm-wait-postgres.service from ~/.config/systemd/user/
#     - runs `systemctl --user daemon-reload`
#     - removes the (now unused) podman network
#
#   It DOES NOT touch:
#     - the podman volume  litellm-pgdata   <- YOUR DATABASE
#     - ~/.config/litellm/config.yaml
#     - ~/.config/litellm/litellm.env       <- YOUR SECRETS
#     - the pulled container images
#     - the user's linger setting
#
#   Re-running quadlet/install.sh after a plain uninstall therefore brings the
#   gateway back with every virtual key, team, budget and spend row intact.
#
# THE DESTRUCTIVE FLAGS / 具破壞性的參數
#   --purge-data     also `podman volume rm litellm-pgdata`. IRREVERSIBLE.
#   --purge-config   also delete ~/.config/litellm/ (config.yaml + litellm.env).
#   --purge-images   also `podman rmi` the two images this stack pulls.
#
# WHY LOSING THE VOLUME IS WORSE THAN IT LOOKS
#   LiteLLM encrypts provider credentials stored in the database with
#   LITELLM_SALT_KEY. The database also holds every virtual key hash, team,
#   budget and spend log. There is no export in this repo - if you need a
#   backup, take one BEFORE running --purge-data:
#     podman exec litellm-postgres pg_dump -U litellm -d litellm > backup.sql
#   (.gitignore already excludes *.sql and backups/ so a dump cannot be
#   committed by accident.)
#
# NOTHING IN THIS REPO HAS BEEN LIVE-TESTED against a real Podman host.
# 本套件未在實際 Podman 主機上執行測試。
# =============================================================================
set -euo pipefail

# -----------------------------------------------------------------------------
# Paths - MUST stay in sync with install.sh
# -----------------------------------------------------------------------------
XDG_CONFIG="${XDG_CONFIG_HOME:-${HOME}/.config}"
QUADLET_DIR="${XDG_CONFIG}/containers/systemd"
USER_UNIT_DIR="${XDG_CONFIG}/systemd/user"
APP_DIR="${XDG_CONFIG}/litellm"

# Quadlet unit FILES (what we delete from disk).
QUADLET_UNIT_FILES=(litellm.container litellm-postgres.container litellm-pgdata.volume litellm.network)
PLAIN_UNIT_FILES=(litellm-wait-postgres.service)

# GENERATED systemd SERVICE names (what we stop), in reverse dependency order.
#   litellm.container          -> litellm.service                 (.container: no suffix)
#   litellm-postgres.container -> litellm-postgres.service
#   litellm-pgdata.volume      -> litellm-pgdata-volume.service   (.volume: -volume suffix)
#   litellm.network            -> litellm-network.service         (.network: -network suffix, from the FILENAME, not NetworkName=)
SERVICES_STOP_ORDER=(
  litellm.service
  litellm-wait-postgres.service
  litellm-postgres.service
  litellm-pgdata-volume.service
  litellm-network.service
)

# Podman resources. These names come from ContainerName=/VolumeName=/NetworkName=
# in the unit files - NOT the "systemd-" prefixed defaults.
CONTAINERS=(litellm litellm-postgres litellm-wait-postgres)
NETWORK_NAME="litellm-net"
VOLUME_NAME="litellm-pgdata"
IMAGES=(ghcr.io/berriai/litellm:v1.83.14-stable docker.io/library/postgres:16-alpine)

# -----------------------------------------------------------------------------
# Options
# -----------------------------------------------------------------------------
PURGE_DATA=0
PURGE_CONFIG=0
PURGE_IMAGES=0
DISABLE_LINGER=0
ASSUME_YES=0

# -----------------------------------------------------------------------------
# Output helpers - same bilingual shape as install.sh.
# -----------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_INFO=$'\033[36m'; C_OK=$'\033[32m'
  C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_STEP=$'\033[1;35m'
  C_DANGER=$'\033[1;37;41m'
else
  C_RESET=""; C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""; C_STEP=""; C_DANGER=""
fi

STEP_N=0
STEP_TOTAL=6
step() { STEP_N=$((STEP_N + 1)); printf '%s\n[%d/%d] %s\n      %s%s\n' \
         "$C_STEP" "$STEP_N" "$STEP_TOTAL" "$1" "$2" "$C_RESET"; }
info() { printf '%s  ->  %s\n      %s%s\n' "$C_INFO" "$1" "$2" "$C_RESET"; }
ok()   { printf '%s  OK  %s\n      %s%s\n' "$C_OK"   "$1" "$2" "$C_RESET"; }
warn() { printf '%sWARN  %s\n      %s%s\n' "$C_WARN" "$1" "$2" "$C_RESET" >&2; }
die()  { printf '%sFAIL  %s\n      %s%s\n' "$C_ERR"  "$1" "$2" "$C_RESET" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: uninstall.sh [OPTIONS]

  (no options)         Stop the stack and remove the unit files. KEEPS the
                       database volume, the config and the env file.
                       停止服務並移除 unit 檔，但保留資料庫、設定與金鑰檔。

  --purge-data         ALSO destroy the Postgres volume "litellm-pgdata".
                       IRREVERSIBLE - every virtual key, team, budget and spend
                       record is lost. Prompts for typed confirmation.
                       同時刪除 Postgres 資料卷，無法復原，需輸入確認字串。

  --purge-config       ALSO delete ~/.config/litellm/ (config.yaml AND the env
                       file containing your API keys, master key and salt key).
                       同時刪除設定目錄，包含 API 金鑰與 salt key。

  --purge-images       ALSO remove the two container images this stack pulls.
                       同時移除本套件使用的容器映像檔。

  --disable-linger     ALSO run `loginctl disable-linger $USER`. Only do this if
                       NO other rootless service of yours needs to survive
                       logout - linger is a per-user, not per-app, setting.
                       同時關閉 linger（會影響該使用者的所有無 root 服務）。

  -y, --yes            Skip the interactive prompt. The loud warning banner is
                       still printed and a 10-second abort window is still given
                       whenever --purge-data is in effect.
                       略過互動確認，但仍會顯示警告並保留 10 秒中止時間。

  -h, --help           Show this help.

BEFORE --purge-data, take a backup:
  podman exec litellm-postgres pg_dump -U litellm -d litellm > backup.sql
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge-data)     PURGE_DATA=1; shift ;;
    --purge-config)   PURGE_CONFIG=1; shift ;;
    --purge-images)   PURGE_IMAGES=1; shift ;;
    --disable-linger) DISABLE_LINGER=1; shift ;;
    -y|--yes)         ASSUME_YES=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    *)                usage >&2; die "Unknown option: $1" "未知的參數：$1" ;;
  esac
done

# -----------------------------------------------------------------------------
# Preflight
# -----------------------------------------------------------------------------
if [[ "$(id -u)" -eq 0 ]]; then
  warn "Running as root. This script targets the ROOTLESS installation under \$HOME." \
       "偵測到 root 身分。本腳本針對「無 root」安裝（\$HOME 底下）。"
  warn "A rootful install lives in /etc/containers/systemd/ and needs 'systemctl' without --user." \
       "若當初是以 root 安裝於 /etc/containers/systemd/，請改用不加 --user 的 systemctl 手動移除。"
fi

PODMAN_BIN="$(command -v podman || true)"
if [[ -z "$PODMAN_BIN" ]]; then
  warn "podman not found on PATH - unit files will still be removed, but podman resources cannot be." \
       "找不到 podman，仍會移除 unit 檔，但無法清理 podman 資源。"
fi

HAVE_SYSTEMCTL=1
command -v systemctl >/dev/null 2>&1 || { HAVE_SYSTEMCTL=0; warn "systemctl not found." "找不到 systemctl。"; }

# `systemctl --user` needs a running user manager (DBus session). Without it,
# every call fails with "Failed to connect to bus" - detect this once instead of
# emitting five identical errors.
USER_BUS_OK=1
if [[ "$HAVE_SYSTEMCTL" -eq 1 ]]; then
  systemctl --user show-environment >/dev/null 2>&1 || USER_BUS_OK=0
fi
if [[ "$USER_BUS_OK" -eq 0 ]]; then
  warn "No usable 'systemctl --user' session (no DBus / not a login session)." \
       "無法使用 systemctl --user（缺少 DBus 或非登入工作階段）。"
  warn "Units will be deleted from disk; containers will be removed with podman directly." \
       "將直接刪除 unit 檔，並改用 podman 指令移除容器。"
fi

# -----------------------------------------------------------------------------
# confirm - typed confirmation for destructive actions.
#   Returns 0 on confirmation, non-zero otherwise. Never dies, so the caller
#   decides whether to skip the step or abort entirely.
# -----------------------------------------------------------------------------
confirm() {
  local expected="$1" prompt_en="$2" prompt_zh="$3" answer=""
  if [[ "$ASSUME_YES" -eq 1 ]]; then
    warn "--yes given, skipping the typed confirmation." "已指定 --yes，略過輸入確認。"
    return 0
  fi
  if [[ ! -t 0 ]]; then
    warn "stdin is not a terminal and --yes was not given - refusing to proceed." \
         "非互動模式且未指定 --yes，拒絕執行。"
    return 1
  fi
  printf '%s\n%s\n' "$prompt_en" "$prompt_zh"
  printf 'Type exactly %s%s%s to continue (anything else aborts): ' "$C_DANGER" "$expected" "$C_RESET"
  # `read` returning non-zero on EOF must not trip `set -e`.
  read -r answer || answer=""
  [[ "$answer" == "$expected" ]]
}

# -----------------------------------------------------------------------------
# LOUD confirmation for --purge-data. Printed BEFORE anything is stopped so the
# operator can still take a pg_dump from the running container.
# -----------------------------------------------------------------------------
if [[ "$PURGE_DATA" -eq 1 ]]; then
  printf '\n%s                                                                            %s\n' "$C_DANGER" "$C_RESET"
  printf '%s   !!!  DESTRUCTIVE OPERATION REQUESTED  --purge-data  !!!                   %s\n' "$C_DANGER" "$C_RESET"
  printf '%s   !!!  警告：已要求刪除資料庫  --purge-data            !!!                   %s\n' "$C_DANGER" "$C_RESET"
  printf '%s                                                                            %s\n' "$C_DANGER" "$C_RESET"
  cat <<EOF

  This will run:   podman volume rm ${VOLUME_NAME}

  That PERMANENTLY destroys the LiteLLM Postgres database, including:
      * every virtual API key (hashes, aliases, budgets, rate limits)
      * every team, internal user and spend/usage log row
      * every provider credential stored via the Admin UI - these are encrypted
        with LITELLM_SALT_KEY and cannot be recovered from anywhere else
      * the model rows created by store_model_in_db: true

  There is NO undo. There is NO snapshot. Podman volumes are not versioned.

  這將永久刪除 LiteLLM 的 Postgres 資料庫，包含所有虛擬金鑰、團隊、使用者、
  花費紀錄，以及以 LITELLM_SALT_KEY 加密儲存的供應商憑證。此操作無法復原。

  TAKE A BACKUP FIRST (the container is still running right now):
      podman exec ${CONTAINERS[1]} pg_dump -U litellm -d litellm > backup.sql

  If you only want to stop and remove the services, re-run this script WITHOUT
  --purge-data. That keeps the volume and a later install.sh restores everything.
  若只是想移除服務，請不要加 --purge-data，資料會完整保留。

EOF
  if ! confirm "DELETE-LITELLM-DATA" \
       "  Confirm permanent deletion of the database volume '${VOLUME_NAME}'." \
       "  請確認要永久刪除資料卷 '${VOLUME_NAME}'。"; then
    die "Aborted - nothing was changed. 已中止，未做任何變更。" \
        "Re-run without --purge-data to remove the services only."
  fi
  if [[ "$ASSUME_YES" -eq 1 ]]; then
    printf '%s  Proceeding in 10 seconds - press Ctrl-C to abort. 10 秒後開始，可按 Ctrl-C 中止。%s\n' \
      "$C_WARN" "$C_RESET"
    sleep 10
  fi
  ok "Confirmed. Continuing with data purge." "已確認，將刪除資料。"
fi

if [[ "$PURGE_CONFIG" -eq 1 ]]; then
  printf '\n%s  --purge-config will delete %s (config.yaml AND litellm.env with your keys).%s\n' \
    "$C_WARN" "$APP_DIR" "$C_RESET"
  printf '%s  --purge-config 會刪除 %s，包含你的 API 金鑰與 LITELLM_SALT_KEY。%s\n' \
    "$C_WARN" "$APP_DIR" "$C_RESET"
  printf '%s  NOTE: if the database volume survives, you MUST keep the same LITELLM_SALT_KEY%s\n' "$C_WARN" "$C_RESET"
  printf '%s        or every credential encrypted in that database becomes undecryptable.%s\n' "$C_WARN" "$C_RESET"
  printf '%s  注意：若資料庫仍保留，salt key 必須維持不變，否則資料庫內的加密憑證將永久無法解密。%s\n' "$C_WARN" "$C_RESET"
  if ! confirm "DELETE-LITELLM-CONFIG" \
       "  Confirm deletion of ${APP_DIR}." \
       "  請確認要刪除 ${APP_DIR}。"; then
    warn "Skipping --purge-config; ${APP_DIR} is kept." "略過 --purge-config，保留 ${APP_DIR}。"
    PURGE_CONFIG=0
  fi
fi

printf '\n%s=== WOOWTECH LiteLLM gateway - rootless Quadlet uninstall ===%s\n' "$C_STEP" "$C_RESET"
printf '%s=== WOOWTECH LiteLLM 閘道器 - 無 root Quadlet 移除 ===%s\n' "$C_STEP" "$C_RESET"
printf '    user            : %s\n'  "$(id -un)"
printf '    quadlet dir     : %s\n'  "$QUADLET_DIR"
printf '    user unit dir   : %s\n'  "$USER_UNIT_DIR"
printf '    purge data      : %s\n'  "$( ((PURGE_DATA))   && echo YES || echo no )"
printf '    purge config    : %s\n'  "$( ((PURGE_CONFIG)) && echo YES || echo no )"
printf '    purge images    : %s\n'  "$( ((PURGE_IMAGES)) && echo YES || echo no )"

# =============================================================================
# 1. Stop the services
# =============================================================================
step "Stopping services / 停止服務" \
     "Reverse dependency order: proxy -> wait unit -> postgres -> volume -> network."

if [[ "$HAVE_SYSTEMCTL" -eq 1 && "$USER_BUS_OK" -eq 1 ]]; then
  for svc in "${SERVICES_STOP_ORDER[@]}"; do
    # `systemctl stop` on a unit that does not exist exits non-zero; under
    # `set -e` that would abort the whole uninstall, so every call is guarded.
    if systemctl --user stop "$svc" >/dev/null 2>&1; then
      ok "stopped ${svc}" "已停止 ${svc}"
    else
      info "not running / not present: ${svc}" "未執行或不存在：${svc}"
    fi
  done

  # Quadlet-generated units are TRANSIENT - they exist only in
  # /run/user/<uid>/systemd/generator/ and are recreated by every
  # daemon-reload. `systemctl --user disable` on them fails with
  # "unit file does not exist"; enablement lives in the [Install] section of the
  # source .container file, so DELETING THE FILE (step 3) is the real "disable".
  # The plain wait unit is the only one that could carry real symlinks, and it
  # ships without an [Install] section, so this is belt-and-braces.
  for svc in "${PLAIN_UNIT_FILES[@]}"; do
    if systemctl --user disable "$svc" >/dev/null 2>&1; then
      ok "disabled ${svc}" "已停用 ${svc}"
    else
      info "nothing to disable for ${svc} (no [Install] section - expected)" \
           "${svc} 無需停用（本來就沒有 [Install] 區段）"
    fi
  done

  # Clear any "failed" state so a later `systemctl --user --failed` is clean.
  systemctl --user reset-failed >/dev/null 2>&1 || true
else
  warn "Skipping systemctl stop - no user manager available." "略過 systemctl，改用 podman 停止。"
fi

# =============================================================================
# 2. Remove leftover containers
# =============================================================================
step "Removing leftover containers / 移除殘留容器" \
     "Containers started outside systemd, or left behind by a killed run."

if [[ -n "$PODMAN_BIN" ]]; then
  for c in "${CONTAINERS[@]}"; do
    if "$PODMAN_BIN" container exists "$c" >/dev/null 2>&1; then
      # -f stops it first; -i makes a missing container non-fatal.
      if "$PODMAN_BIN" rm -f -i "$c" >/dev/null 2>&1; then
        ok "removed container ${c}" "已移除容器 ${c}"
      else
        warn "could not remove container ${c} - remove it by hand" "無法移除容器 ${c}，請手動處理"
      fi
    else
      info "no container named ${c}" "沒有名為 ${c} 的容器"
    fi
  done
else
  warn "podman unavailable - skipping container cleanup." "無 podman，略過容器清理。"
fi

# =============================================================================
# 3. Remove the unit files
# =============================================================================
step "Removing unit files / 移除 unit 檔案" \
     "Quadlet units from ${QUADLET_DIR}, the plain unit from ${USER_UNIT_DIR}."

removed_any=0
for f in "${QUADLET_UNIT_FILES[@]}"; do
  if [[ -e "${QUADLET_DIR}/${f}" ]]; then
    rm -f -- "${QUADLET_DIR}/${f}"
    ok "removed ${QUADLET_DIR}/${f}" "已刪除 ${QUADLET_DIR}/${f}"
    removed_any=1
  else
    info "absent: ${QUADLET_DIR}/${f}" "不存在：${QUADLET_DIR}/${f}"
  fi
done
for f in "${PLAIN_UNIT_FILES[@]}"; do
  if [[ -e "${USER_UNIT_DIR}/${f}" ]]; then
    rm -f -- "${USER_UNIT_DIR}/${f}"
    ok "removed ${USER_UNIT_DIR}/${f}" "已刪除 ${USER_UNIT_DIR}/${f}"
    removed_any=1
  else
    info "absent: ${USER_UNIT_DIR}/${f}" "不存在：${USER_UNIT_DIR}/${f}"
  fi
done
[[ "$removed_any" -eq 1 ]] || warn "No unit files were found - was this ever installed here?" \
                                   "找不到任何 unit 檔，是否曾在此使用者下安裝過？"

# Only remove the Quadlet directory if WE emptied it. Other stacks may live here.
# `rmdir` fails harmlessly when the directory is non-empty; that is the point.
if [[ -d "$QUADLET_DIR" ]]; then
  rmdir -- "$QUADLET_DIR" 2>/dev/null \
    && info "removed empty ${QUADLET_DIR}" "已刪除空目錄 ${QUADLET_DIR}" \
    || info "${QUADLET_DIR} kept (other units present)" "保留 ${QUADLET_DIR}（尚有其他 unit）"
fi

# =============================================================================
# 4. daemon-reload
# =============================================================================
step "Reloading systemd / 重新載入 systemd" \
     "Re-runs the Quadlet generator so the deleted units disappear from systemd."

if [[ "$HAVE_SYSTEMCTL" -eq 1 && "$USER_BUS_OK" -eq 1 ]]; then
  systemctl --user daemon-reload
  ok "systemctl --user daemon-reload" "已重新載入使用者 systemd"
else
  warn "Run 'systemctl --user daemon-reload' yourself once a user session exists." \
       "請在有使用者工作階段時自行執行 systemctl --user daemon-reload。"
fi

# =============================================================================
# 5. Podman resources - network always, volume/images only when asked
# =============================================================================
step "Cleaning podman resources / 清理 podman 資源" \
     "Network is always removed; the volume needs --purge-data."

if [[ -n "$PODMAN_BIN" ]]; then
  # Network: safe to drop. It is created by the .network unit and recreated on
  # the next install. Removal fails if any container is still attached, which is
  # why containers are cleared in step 2.
  if "$PODMAN_BIN" network exists "$NETWORK_NAME" >/dev/null 2>&1; then
    if "$PODMAN_BIN" network rm "$NETWORK_NAME" >/dev/null 2>&1; then
      ok "removed network ${NETWORK_NAME}" "已移除網路 ${NETWORK_NAME}"
    else
      warn "could not remove network ${NETWORK_NAME} (still in use?)" \
           "無法移除網路 ${NETWORK_NAME}（可能仍被使用）"
      info "check with: podman network inspect ${NETWORK_NAME}" \
           "可用 podman network inspect ${NETWORK_NAME} 檢查"
    fi
  else
    info "no network named ${NETWORK_NAME}" "沒有名為 ${NETWORK_NAME} 的網路"
  fi

  # Volume.
  if "$PODMAN_BIN" volume exists "$VOLUME_NAME" >/dev/null 2>&1; then
    if [[ "$PURGE_DATA" -eq 1 ]]; then
      if "$PODMAN_BIN" volume rm "$VOLUME_NAME" >/dev/null 2>&1; then
        ok "DESTROYED volume ${VOLUME_NAME}" "已刪除資料卷 ${VOLUME_NAME}"
      else
        warn "could not remove volume ${VOLUME_NAME} - a container may still reference it" \
             "無法移除資料卷 ${VOLUME_NAME}，可能仍有容器參照"
        info "retry with: podman volume rm -f ${VOLUME_NAME}" \
             "可重試：podman volume rm -f ${VOLUME_NAME}"
      fi
    else
      ok "KEPT volume ${VOLUME_NAME} - your database is safe" \
         "已保留資料卷 ${VOLUME_NAME}，資料庫安全"
      info "to destroy it later: ./quadlet/uninstall.sh --purge-data" \
           "若日後要刪除：./quadlet/uninstall.sh --purge-data"
    fi
  else
    info "no volume named ${VOLUME_NAME}" "沒有名為 ${VOLUME_NAME} 的資料卷"
  fi

  # Images.
  if [[ "$PURGE_IMAGES" -eq 1 ]]; then
    for img in "${IMAGES[@]}"; do
      if "$PODMAN_BIN" rmi "$img" >/dev/null 2>&1; then
        ok "removed image ${img}" "已移除映像檔 ${img}"
      else
        info "image ${img} absent or still in use" "映像檔 ${img} 不存在或仍被使用"
      fi
    done
  else
    info "images kept (use --purge-images to remove)" "保留映像檔（可用 --purge-images 移除）"
  fi
else
  warn "podman unavailable - network/volume/images untouched." "無 podman，未清理網路/資料卷/映像檔。"
fi

# =============================================================================
# 6. Config directory and linger
# =============================================================================
step "Config directory and linger / 設定目錄與 linger" \
     "Both are preserved unless you explicitly asked otherwise."

if [[ "$PURGE_CONFIG" -eq 1 ]]; then
  if [[ -d "$APP_DIR" ]]; then
    rm -rf -- "$APP_DIR"
    ok "removed ${APP_DIR}" "已刪除 ${APP_DIR}"
  else
    info "absent: ${APP_DIR}" "不存在：${APP_DIR}"
  fi
else
  if [[ -d "$APP_DIR" ]]; then
    ok "KEPT ${APP_DIR} (config.yaml + litellm.env)" "已保留 ${APP_DIR}（設定與金鑰）"
    info "the env file still holds your secrets - it is mode 0600" \
         "環境變數檔仍含機密資訊，權限為 0600"
  fi
fi

if [[ "$DISABLE_LINGER" -eq 1 ]]; then
  if command -v loginctl >/dev/null 2>&1; then
    if loginctl disable-linger "$(id -un)" >/dev/null 2>&1; then
      ok "lingering disabled for $(id -un)" "已關閉 $(id -un) 的 linger"
      warn "ANY other rootless service of this user will now stop at logout." \
           "此使用者的其他無 root 服務也會在登出後停止。"
    else
      warn "could not disable lingering (try: sudo loginctl disable-linger $(id -un))" \
           "無法關閉 linger（可試 sudo loginctl disable-linger $(id -un)）"
    fi
  else
    warn "loginctl not found - skipping." "找不到 loginctl，略過。"
  fi
else
  info "lingering left as-is (use --disable-linger to turn it off)" \
       "linger 設定維持不變（可用 --disable-linger 關閉）"
fi

# =============================================================================
# Summary
# =============================================================================
printf '\n%s=============================== SUMMARY / 摘要 ===============================%s\n' \
  "$C_STEP" "$C_RESET"

printf '  Services stopped and unit files removed.\n'
printf '  服務已停止，unit 檔已移除。\n\n'

if [[ "$PURGE_DATA" -eq 1 ]]; then
  printf '%s  DATABASE DESTROYED - volume %s is gone.%s\n' "$C_ERR" "$VOLUME_NAME" "$C_RESET"
  printf '%s  資料庫已刪除 - 資料卷 %s 已不存在。%s\n\n' "$C_ERR" "$VOLUME_NAME" "$C_RESET"
else
  printf '%s  DATABASE PRESERVED - volume %s is intact.%s\n' "$C_OK" "$VOLUME_NAME" "$C_RESET"
  printf '%s  資料庫已保留 - 資料卷 %s 完好。%s\n\n' "$C_OK" "$VOLUME_NAME" "$C_RESET"
fi

printf '  What is still on this host / 主機上仍存在的東西:\n'
[[ "$PURGE_DATA"   -eq 1 ]] || printf '    - podman volume  %s\n' "$VOLUME_NAME"
[[ "$PURGE_CONFIG" -eq 1 ]] || printf '    - %s (config.yaml, litellm.env)\n' "$APP_DIR"
[[ "$PURGE_IMAGES" -eq 1 ]] || printf '    - container images: %s\n' "${IMAGES[*]}"
printf '    - this repository checkout\n\n'

printf '  Verify nothing is left running / 確認沒有殘留:\n'
printf '    systemctl --user list-units "litellm*"\n'
printf '    podman ps -a --filter "label=io.woowtech.stack=litellm-gw"\n'
printf '    podman volume ls\n'
printf '    podman network ls\n\n'

if [[ "$PURGE_DATA" -eq 0 ]]; then
  printf '  Reinstall (data comes back with it) / 重新安裝（資料會一併回來）:\n'
  printf '    ./quadlet/install.sh\n\n'
  printf '  NOTE: keep the SAME LITELLM_SALT_KEY in your env file when you reinstall,\n'
  printf '        or the provider credentials encrypted inside that volume become\n'
  printf '        permanently undecryptable.\n'
  printf '  注意：重新安裝時務必使用「相同的」LITELLM_SALT_KEY，否則資料卷中\n'
  printf '        已加密的憑證將永久無法解密。\n'
fi

printf '%s==============================================================================%s\n' \
  "$C_STEP" "$C_RESET"
