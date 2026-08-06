#!/usr/bin/env bash
# =============================================================================
# install.sh - install the WOOWTECH LiteLLM gateway as ROOTLESS Podman Quadlet
#              units under the current user's systemd manager.
#
# 安裝 WOOWTECH LiteLLM 閘道器為「無 root」Podman Quadlet systemd 服務。
#
# Repo: https://github.com/WOOWTECH/Woow_podman_litellm
#
# WHAT THIS SCRIPT DOES / 這個腳本會做什麼
#   1. Verify Podman exists, is new enough, and has cgroup v2.
#   2. Validate your env file (must exist, must not contain placeholders).
#   3. Copy config.yaml + the env file to ~/.config/litellm/ (env file 0600).
#   4. Copy the Quadlet units to ~/.config/containers/systemd/ and the plain
#      systemd fallback unit to ~/.config/systemd/user/.
#   5. Dry-run the Quadlet generator to prove every unit actually generates.
#   6. Enable lingering so the stack survives logout and starts at boot.
#   7. daemon-reload, start postgres, then the proxy, then poll health.
#
# NOTHING IN THIS REPO HAS BEEN LIVE-TESTED against a real Podman host. Treat
# every behaviour described here as documented, not verified.
# 本套件未在實際 Podman 主機上執行測試，所有行為皆依官方文件推導。
# =============================================================================
set -euo pipefail

# -----------------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

XDG_CONFIG="${XDG_CONFIG_HOME:-${HOME}/.config}"
QUADLET_DIR="${XDG_CONFIG}/containers/systemd"   # Quadlet reads .container/.volume/.network here
USER_UNIT_DIR="${XDG_CONFIG}/systemd/user"       # plain .service units live here instead
APP_DIR="${XDG_CONFIG}/litellm"                  # our own config + env file
CONFIG_DEST="${APP_DIR}/config.yaml"
ENV_DEST="${APP_DIR}/litellm.env"

CONFIG_SRC="${REPO_ROOT}/config/config.yaml"
ENV_SRC_DEFAULT="${REPO_ROOT}/.env"

QUADLET_UNITS=(litellm.network litellm-pgdata.volume litellm-postgres.container litellm.container)
PLAIN_UNITS=(litellm-wait-postgres.service)

IMAGES=(docker.io/library/postgres:16-alpine ghcr.io/berriai/litellm:v1.83.14-stable)

REQUIRED_ENV_KEYS=(OPENROUTER_API_KEY LITELLM_MASTER_KEY LITELLM_SALT_KEY DATABASE_URL
                   POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB)

# -----------------------------------------------------------------------------
# Options
# -----------------------------------------------------------------------------
DRY_RUN=0
DO_PULL=1
DO_LINGER=1
ENV_SRC=""
HEALTH_TIMEOUT="${LITELLM_HEALTH_TIMEOUT:-300}"

# -----------------------------------------------------------------------------
# Output helpers - bilingual, like the sibling WOOWTECH repos.
# -----------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_INFO=$'\033[36m'; C_OK=$'\033[32m'
  C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_STEP=$'\033[1;35m'
else
  C_RESET=""; C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""; C_STEP=""
fi

STEP_N=0
step() { STEP_N=$((STEP_N + 1)); printf '%s\n[%d/%d] %s\n      %s%s\n' \
         "$C_STEP" "$STEP_N" "$STEP_TOTAL" "$1" "$2" "$C_RESET"; }
info() { printf '%s  ->  %s\n      %s%s\n' "$C_INFO" "$1" "$2" "$C_RESET"; }
ok()   { printf '%s  OK  %s\n      %s%s\n' "$C_OK"   "$1" "$2" "$C_RESET"; }
warn() { printf '%sWARN  %s\n      %s%s\n' "$C_WARN" "$1" "$2" "$C_RESET" >&2; }
die()  { printf '%sFAIL  %s\n      %s%s\n' "$C_ERR"  "$1" "$2" "$C_RESET" >&2; exit 1; }
STEP_TOTAL=10

usage() {
  cat <<'EOF'
Usage: install.sh [OPTIONS]

  --dry-run            Validate the unit files with the Quadlet generator and
                       exit. Installs nothing, starts nothing.
                       僅驗證 unit 檔語法，不安裝、不啟動。
  --env-file PATH      Source env file (default: <repo>/.env).
                       指定來源環境變數檔。
  --no-pull            Skip pre-pulling images.
                       略過預先拉取映像檔。
  --no-linger          Skip `loginctl enable-linger` (stack will NOT survive
                       logout or reboot).
                       略過 linger 設定（登出後服務會停止）。
  -h, --help           Show this help.

Environment:
  LITELLM_HEALTH_TIMEOUT   Seconds to wait for the proxy to become healthy
                           (default 300).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)   DRY_RUN=1; shift ;;
    --no-pull)   DO_PULL=0; shift ;;
    --no-linger) DO_LINGER=0; shift ;;
    --env-file)  ENV_SRC="${2:-}"; [[ -n "$ENV_SRC" ]] || die "--env-file needs a path" "--env-file 需要一個路徑"; shift 2 ;;
    -h|--help)   usage; exit 0 ;;
    *)           usage >&2; die "Unknown option: $1" "未知的參數：$1" ;;
  esac
done
[[ -n "$ENV_SRC" ]] || ENV_SRC="$ENV_SRC_DEFAULT"

# -----------------------------------------------------------------------------
# find_quadlet_generator - locate the podman Quadlet systemd generator.
# Documented invocation:
#   /usr/lib/systemd/system-generators/podman-system-generator --user --dryrun
# Some distros ship it only at /usr/libexec/podman/quadlet (the generator binary
# the two generator symlinks point at) or under /usr/lib64.
# -----------------------------------------------------------------------------
find_quadlet_generator() {
  local c
  for c in /usr/lib/systemd/system-generators/podman-system-generator \
           /usr/lib64/systemd/system-generators/podman-system-generator \
           /usr/libexec/podman/quadlet \
           /usr/lib/podman/quadlet; do
    [[ -x "$c" ]] && { printf '%s' "$c"; return 0; }
  done
  return 1
}

# -----------------------------------------------------------------------------
# validate_units <dir> - run the generator against <dir> only.
# QUADLET_UNIT_DIRS makes Quadlet look ONLY there, so the output is scoped to
# our units. An unknown or misspelled key makes a unit silently not generate,
# which later shows up as the very confusing "Unit litellm.service not found" -
# this check is how we catch that before starting anything.
# -----------------------------------------------------------------------------
validate_units() {
  local dir="$1" gen out rc=0
  if ! gen="$(find_quadlet_generator)"; then
    warn "Quadlet generator binary not found - skipping unit validation." \
         "找不到 Quadlet 產生器，略過 unit 檔驗證。"
    return 0
  fi
  info "Validating with: QUADLET_UNIT_DIRS=${dir} ${gen} --user --dryrun" \
       "使用上述指令驗證 unit 檔。"
  out="$(QUADLET_UNIT_DIRS="$dir" "$gen" --user --dryrun 2>&1)" || rc=$?
  if [[ $rc -ne 0 ]]; then
    printf '%s\n' "$out" >&2
    die "Quadlet generator reported errors (exit ${rc})." \
        "Quadlet 產生器回報錯誤（結束碼 ${rc}）。"
  fi
  # The generator prints parse problems to stderr but still exits 0 in some
  # versions, so grep the captured output as well.
  if printf '%s' "$out" | grep -qiE 'converting|error|unsupported key|failed'; then
    printf '%s\n' "$out" >&2
    die "Quadlet generator output contains errors - see above." \
        "Quadlet 產生器輸出含有錯誤訊息，請見上方。"
  fi
  local n
  n="$(printf '%s' "$out" | grep -cE '^---' || true)"
  ok "Quadlet generator parsed the units cleanly (${n} service files)." \
     "Quadlet 產生器已成功解析 unit 檔（產生 ${n} 個 service）。"
}

# =============================================================================
# --dry-run: validate the repo's quadlet/ directory and stop.
# =============================================================================
if [[ $DRY_RUN -eq 1 ]]; then
  printf '%sLiteLLM Quadlet - DRY RUN (validation only) / 僅驗證模式%s\n' "$C_STEP" "$C_RESET"
  validate_units "$SCRIPT_DIR"
  info "Note: litellm-wait-postgres.service is a PLAIN systemd unit and is not" \
       "注意：litellm-wait-postgres.service 是一般 systemd unit，"
  info "seen by the Quadlet generator. Check it with:" \
       "Quadlet 產生器不會處理它，請改用下列指令檢查："
  printf '        systemd-analyze --user verify %s/litellm-wait-postgres.service\n' "$SCRIPT_DIR"
  ok "Dry run complete. Nothing was installed or started." \
     "驗證完成，未安裝也未啟動任何服務。"
  exit 0
fi

printf '%s\n=== WOOWTECH LiteLLM gateway - rootless Podman Quadlet install ===\n' "$C_STEP"
printf '=== WOOWTECH LiteLLM 閘道器 - 無 root Podman Quadlet 安裝 ===%s\n' "$C_RESET"

# =============================================================================
# 1. Preflight: user, podman, version, cgroups
# =============================================================================
step "Preflight checks" "環境檢查"

if [[ "${EUID}" -eq 0 ]]; then
  warn "You are running as root. This script installs a ROOTLESS deployment into" \
       "偵測到 root 身分。本腳本安裝的是「無 root」部署，"
  warn "root's own user manager. For a system-wide install put the units in" \
       "會裝進 root 自己的 user manager。若要系統層級安裝，請改放到"
  warn "/etc/containers/systemd/ and use [Install] WantedBy=multi-user.target." \
       "/etc/containers/systemd/ 並使用 WantedBy=multi-user.target。"
fi

command -v podman >/dev/null 2>&1 || \
  die "podman not found in PATH." "PATH 中找不到 podman。"
PODMAN_BIN="$(command -v podman)"
info "podman binary: ${PODMAN_BIN}" "podman 執行檔位置：${PODMAN_BIN}"

# The account whose systemd user manager we are installing into. Do NOT use a
# bare $USER: it is not POSIX-guaranteed, so it is absent under `su` without -l,
# under `sudo` configurations that reset the environment, and in cron/systemd
# contexts - and under `set -u` an unset $USER aborts the script mid-install.
# Prefer $USER when it IS set, because under `sudo` (without -E) sudo sets USER
# to the target account while `id -un` would report root, which would enable
# lingering for the wrong user.
RUN_USER="${USER:-$(id -un)}"
info "Installing for user: ${RUN_USER}" "安裝對象使用者：${RUN_USER}"

command -v systemctl >/dev/null 2>&1 || \
  die "systemctl not found - this stack requires systemd." "找不到 systemctl，本套件需要 systemd。"

PODMAN_VER="$("$PODMAN_BIN" version --format '{{.Client.Version}}' 2>/dev/null || echo "0.0.0")"
PV_MAJOR="${PODMAN_VER%%.*}"
PV_REST="${PODMAN_VER#*.}"
PV_MINOR="${PV_REST%%.*}"
[[ "$PV_MAJOR" =~ ^[0-9]+$ ]] || PV_MAJOR=0
[[ "$PV_MINOR" =~ ^[0-9]+$ ]] || PV_MINOR=0
info "Podman version: ${PODMAN_VER}" "Podman 版本：${PODMAN_VER}"

# Quadlet was introduced in Podman 4.4 - hard floor.
if (( PV_MAJOR < 4 || (PV_MAJOR == 4 && PV_MINOR < 4) )); then
  warn "Podman ${PODMAN_VER} is older than 4.4 - Quadlet does not exist here." \
       "Podman ${PODMAN_VER} 低於 4.4，此版本沒有 Quadlet 功能。"
  warn "The unit files will not generate any services. Use docker-compose.yml" \
       "unit 檔不會產生任何服務，請改用 docker-compose.yml"
  warn "with podman-compose instead, or upgrade Podman." \
       "搭配 podman-compose，或升級 Podman。"
  die "Podman >= 4.4 required for the Quadlet path." "Quadlet 路徑需要 Podman >= 4.4。"
fi

# PodmanArgs= was added in Podman 4.6. Both .container files use it (for
# --network-alias, --memory and --cpus), and Quadlet refuses to generate a unit
# that contains a key it does not recognise - it skips the WHOLE file.
if (( PV_MAJOR == 4 && PV_MINOR < 6 )); then
  warn "Podman ${PODMAN_VER} is below 4.6: 'PodmanArgs=' does NOT exist and" \
       "Podman ${PODMAN_VER} 低於 4.6：不支援 PodmanArgs=，"
  warn "both .container files will FAIL TO GENERATE as shipped." \
       "兩個 .container 檔都會無法產生服務。"
  warn "Fix: delete every 'PodmanArgs=' line (network alias, --memory, --cpus)," \
       "解法：刪除所有 PodmanArgs= 行（network alias、--memory、--cpus），"
  warn "or upgrade Podman. Losing them costs the 'postgres' DNS alias and the" \
       "或升級 Podman。刪除後會失去 postgres DNS 別名與"
  warn "resource caps only; DATABASE_URL uses the container name either way." \
       "資源上限；DATABASE_URL 仍以容器名稱解析，不受影響。"
fi

# Podman 5.0 is the practical floor: litellm-postgres.container uses
# Notify=healthy, which does not exist before 5.0.
if (( PV_MAJOR < 5 )); then
  warn "Podman ${PODMAN_VER} is below 5.0: 'Notify=healthy' does NOT exist and" \
       "Podman ${PODMAN_VER} 低於 5.0：不支援 Notify=healthy，"
  warn "litellm-postgres.container will FAIL TO GENERATE as shipped." \
       "litellm-postgres.container 會無法產生服務。"
  warn "Fix: comment out the 'Notify=healthy' line in litellm-postgres.container." \
       "解法：將 litellm-postgres.container 中的 Notify=healthy 註解掉。"
  warn "The litellm-wait-postgres.service fallback (installed by this script)" \
       "本腳本安裝的 litellm-wait-postgres.service 後備方案"
  warn "then provides the startup ordering instead." \
       "會改為提供啟動順序保證。"
  warn "Also note: [Container] Memory= needs 5.5+ (this repo uses PodmanArgs" \
       "另注意：[Container] Memory= 需 5.5+，本套件改用 PodmanArgs"
  warn "--memory instead, so no change is needed there)." \
       "--memory，因此該處不需修改。"
fi
if (( PV_MAJOR == 5 && PV_MINOR < 5 )); then
  info "Podman ${PODMAN_VER}: resource limits use PodmanArgs=--memory/--cpus" \
       "Podman ${PODMAN_VER}：資源限制使用 PodmanArgs=--memory/--cpus"
  info "because the native [Container] Memory= key needs 5.5+." \
       "因為原生 [Container] Memory= 需要 5.5 以上。"
fi

CGROUPS="$("$PODMAN_BIN" info --format '{{.Host.CgroupsVersion}}' 2>/dev/null || echo unknown)"
if [[ "$CGROUPS" != "v2" ]]; then
  warn "cgroups version reported as '${CGROUPS}'. Quadlet requires cgroup v2." \
       "cgroups 版本為 '${CGROUPS}'，Quadlet 需要 cgroup v2。"
else
  ok "cgroup v2 present." "已偵測到 cgroup v2。"
fi

# =============================================================================
# 2. Validate the env file BEFORE touching anything
# =============================================================================
step "Validating the environment file" "檢查環境變數檔"

if [[ ! -f "$ENV_SRC" ]]; then
  if [[ -f "$ENV_DEST" ]]; then
    warn "No ${ENV_SRC}; reusing the already-installed ${ENV_DEST}." \
         "找不到 ${ENV_SRC}，改用已安裝的 ${ENV_DEST}。"
    ENV_SRC="$ENV_DEST"
  else
    # Point at .env.quadlet.example, NOT .env.example: the latter is the COMPOSE
    # template built around ${VAR:?message} interpolation, which systemd's
    # EnvironmentFile= does not expand (see DEPLOYMENT.md).
    printf '\n  mkdir -p ~/.config/litellm && chmod 700 ~/.config/litellm\n  cp %s/.env.quadlet.example ~/.config/litellm/litellm.env\n  chmod 600 ~/.config/litellm/litellm.env\n  $EDITOR ~/.config/litellm/litellm.env\n  %s/quadlet/install.sh --env-file ~/.config/litellm/litellm.env\n\n' \
      "$REPO_ROOT" "$REPO_ROOT"
    die "Env file not found: ${ENV_SRC}. Copy .env.quadlet.example and fill it in first." \
        "找不到環境變數檔 ${ENV_SRC}，請先複製 .env.quadlet.example 並填入真實值。"
  fi
fi
info "Env file: ${ENV_SRC}" "環境變數檔：${ENV_SRC}"

# Refuse to deploy an unedited template. This is the single most common way to
# end up with a gateway protected by a publicly-known password.
# Only KEY=VALUE assignment lines are inspected - the shipped templates mention
# the placeholder strings in their prose comments too, and matching those would
# make the gate fire on a perfectly good env file.
PLACEHOLDER_RE='^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=.*(REPLACE_ME|CHANGE_ME|PASTE_[A-Z_]+_HERE|<your[^>]*>)'
if grep -nqE "$PLACEHOLDER_RE" "$ENV_SRC"; then
  printf '\n'
  grep -nE "$PLACEHOLDER_RE" "$ENV_SRC" | sed 's/=.*/=<placeholder>/' >&2
  printf '\n'
  die "The env file still contains placeholders (listed above). Refusing to install." \
      "環境變數檔仍含有預設佔位字串（如上），拒絕安裝。"
fi

MISSING=()
for k in "${REQUIRED_ENV_KEYS[@]}"; do
  if ! grep -qE "^[[:space:]]*${k}=[^[:space:]]" "$ENV_SRC"; then
    MISSING+=("$k")
  fi
done
if (( ${#MISSING[@]} > 0 )); then
  die "Missing or empty in ${ENV_SRC}: ${MISSING[*]}" \
      "${ENV_SRC} 中缺少或為空的變數：${MISSING[*]}"
fi

# systemd EnvironmentFile= is NOT a shell script: `export FOO=bar` would set a
# variable literally named "export FOO".
if grep -qE '^[[:space:]]*export[[:space:]]' "$ENV_SRC"; then
  die "Remove 'export ' prefixes from ${ENV_SRC} - systemd EnvironmentFile= is not a shell script." \
      "請移除 ${ENV_SRC} 中的 'export ' 前綴，systemd EnvironmentFile 不是 shell 腳本。"
fi

# Soft check: the DB host must be a name the Podman network can resolve.
DB_LINE="$(grep -E '^[[:space:]]*DATABASE_URL=' "$ENV_SRC" | tail -n1 || true)"
if [[ -n "$DB_LINE" ]] && ! printf '%s' "$DB_LINE" | grep -qE '@(litellm-postgres|postgres):5432'; then
  warn "DATABASE_URL does not point at litellm-postgres:5432 (or postgres:5432)." \
       "DATABASE_URL 未指向 litellm-postgres:5432（或 postgres:5432）。"
  warn "Those are the only hostnames aardvark-dns resolves on this network." \
       "在此網路上只有這兩個名稱可被 aardvark-dns 解析。"
fi

# LITELLM_SALT_KEY deserves its own shout.
printf '%s\n' "$C_WARN"
printf '  ###########################################################################\n'
printf '  #  LITELLM_SALT_KEY: set ONCE, NEVER rotate.                              #\n'
printf '  #  It encrypts every provider credential stored in Postgres. Changing it  #\n'
printf '  #  makes them PERMANENTLY undecryptable - LiteLLM logs a decrypt error    #\n'
printf '  #  and silently returns nothing, so the breakage is easy to miss.         #\n'
printf '  #  Back the value up somewhere durable, off this host, right now.         #\n'
printf '  #                                                                         #\n'
printf '  #  LITELLM_SALT_KEY：只設定一次，永不更換。                                 #\n'
printf '  #  它用來加密資料庫中所有供應商憑證，更換後將永遠無法解密。                    #\n'
printf '  #  請立刻將此值備份到本機以外的安全位置。                                     #\n'
printf '  ###########################################################################\n'
printf '%s' "$C_RESET"

ok "Env file looks usable." "環境變數檔檢查通過。"

# =============================================================================
# 3. Create directories
# =============================================================================
step "Creating directories" "建立目錄"
mkdir -p "$QUADLET_DIR" "$USER_UNIT_DIR" "$APP_DIR"
chmod 700 "$APP_DIR"
info "Quadlet units : ${QUADLET_DIR}" "Quadlet unit 目錄：${QUADLET_DIR}"
info "Plain units   : ${USER_UNIT_DIR}" "一般 unit 目錄：${USER_UNIT_DIR}"
info "App config    : ${APP_DIR}" "設定檔目錄：${APP_DIR}"

# =============================================================================
# 4. Install config.yaml and the env file
# =============================================================================
step "Installing config.yaml and the env file" "安裝 config.yaml 與環境變數檔"

[[ -f "$CONFIG_SRC" ]] || die "Missing ${CONFIG_SRC}" "找不到 ${CONFIG_SRC}"
install -m 0644 "$CONFIG_SRC" "$CONFIG_DEST"
ok "config.yaml -> ${CONFIG_DEST}" "已安裝 config.yaml 至 ${CONFIG_DEST}"

if [[ "$ENV_SRC" != "$ENV_DEST" ]]; then
  install -m 0600 "$ENV_SRC" "$ENV_DEST"
else
  chmod 0600 "$ENV_DEST"
fi
chmod 0600 "$ENV_DEST"
ok "env file -> ${ENV_DEST} (mode 0600)" "已安裝環境變數檔至 ${ENV_DEST}（權限 0600）"

# =============================================================================
# 5. Install unit files
# =============================================================================
step "Installing unit files" "安裝 unit 檔"

for u in "${QUADLET_UNITS[@]}"; do
  [[ -f "${SCRIPT_DIR}/${u}" ]] || die "Missing unit ${SCRIPT_DIR}/${u}" "找不到 unit 檔 ${SCRIPT_DIR}/${u}"
  install -m 0644 "${SCRIPT_DIR}/${u}" "${QUADLET_DIR}/${u}"
  info "installed ${u}" "已安裝 ${u}"
done

# The plain .service fallback goes in the ORDINARY user unit dir - Quadlet only
# reads .container/.volume/.network/... from its own search paths and would
# silently ignore a .service file placed there.
for u in "${PLAIN_UNITS[@]}"; do
  [[ -f "${SCRIPT_DIR}/${u}" ]] || die "Missing unit ${SCRIPT_DIR}/${u}" "找不到 unit 檔 ${SCRIPT_DIR}/${u}"
  # Rewrite the hardcoded podman path to whatever this host actually has.
  sed "s#/usr/bin/podman#${PODMAN_BIN}#g" "${SCRIPT_DIR}/${u}" > "${USER_UNIT_DIR}/${u}"
  chmod 0644 "${USER_UNIT_DIR}/${u}"
  info "installed ${u} (podman path -> ${PODMAN_BIN})" "已安裝 ${u}（podman 路徑改為 ${PODMAN_BIN}）"
done
ok "Unit files in place." "unit 檔已就位。"

# =============================================================================
# 6. Validate the installed units with the Quadlet generator
# =============================================================================
step "Validating generated systemd services" "驗證產生的 systemd 服務"
validate_units "$QUADLET_DIR"

# =============================================================================
# 7. Pre-pull images (keeps a slow first pull outside TimeoutStartSec)
# =============================================================================
step "Pre-pulling container images" "預先拉取容器映像檔"
if [[ $DO_PULL -eq 1 ]]; then
  for img in "${IMAGES[@]}"; do
    info "podman pull ${img}" "正在拉取 ${img}"
    if ! "$PODMAN_BIN" pull "$img"; then
      warn "Failed to pull ${img}. The unit will try again on start, but a slow" \
           "拉取 ${img} 失敗。啟動時會再試一次，但過慢的拉取"
      warn "pull can exceed TimeoutStartSec." \
           "可能超過 TimeoutStartSec 而導致啟動失敗。"
    fi
  done
  ok "Images present locally." "映像檔已在本機。"
else
  info "Skipped (--no-pull)." "已略過（--no-pull）。"
fi

# =============================================================================
# 8. Lingering - MANDATORY for a rootless stack to survive logout / reboot
# =============================================================================
step "Enabling user lingering" "啟用使用者 linger"
if [[ $DO_LINGER -eq 1 ]]; then
  if command -v loginctl >/dev/null 2>&1; then
    if loginctl enable-linger "$RUN_USER" 2>/dev/null; then
      ok "Lingering enabled for ${RUN_USER}." "已為 ${RUN_USER} 啟用 linger。"
    elif sudo -n true 2>/dev/null && sudo loginctl enable-linger "$RUN_USER"; then
      ok "Lingering enabled for ${RUN_USER} (via sudo)." "已透過 sudo 為 ${RUN_USER} 啟用 linger。"
    else
      warn "Could not enable lingering. Without it the user systemd manager is" \
           "無法啟用 linger。若未啟用，登出時 user systemd manager 會被關閉，"
      warn "torn down at logout, so the stack stops and never starts at boot." \
           "服務會停止且開機時不會自動啟動。"
      warn "Run manually: sudo loginctl enable-linger ${RUN_USER}" \
           "請手動執行：sudo loginctl enable-linger ${RUN_USER}"
    fi
  else
    warn "loginctl not found - cannot enable lingering." "找不到 loginctl，無法啟用 linger。"
  fi
else
  warn "Skipped (--no-linger): the stack will stop when you log out." \
       "已略過（--no-linger）：登出後服務會停止。"
fi

# =============================================================================
# 9. daemon-reload and start, in dependency order
# =============================================================================
step "Reloading systemd and starting services" "重新載入 systemd 並啟動服務"

# daemon-reload is what actually RE-RUNS the Quadlet generator. It also applies
# the [Install] sections - you cannot `systemctl --user enable` a Quadlet unit,
# because the generated services are transient.
systemctl --user daemon-reload
ok "systemctl --user daemon-reload done." "已完成 systemctl --user daemon-reload。"

if ! systemctl --user list-unit-files 2>/dev/null | grep -q '^litellm\.service' \
   && ! systemctl --user cat litellm.service >/dev/null 2>&1; then
  warn "litellm.service is not visible to systemd yet." "systemd 尚未看到 litellm.service。"
  warn "That usually means a unit failed to generate - re-run with --dry-run." \
       "通常代表 unit 產生失敗，請以 --dry-run 重新檢查。"
fi

# The .network and .volume services are pulled in automatically by the
# containers (Quadlet injects Requires=+After=), but starting them explicitly
# gives a clearer error if, say, the subnet collides.
info "Starting network + volume" "啟動網路與磁碟區"
systemctl --user start litellm-network.service || \
  warn "litellm-network.service did not start cleanly." "litellm-network.service 啟動不順利。"
systemctl --user start litellm-pgdata-volume.service || \
  warn "litellm-pgdata-volume.service did not start cleanly." "litellm-pgdata-volume.service 啟動不順利。"

info "Starting litellm-postgres.service" "啟動 litellm-postgres.service"
if ! systemctl --user start litellm-postgres.service; then
  systemctl --user status --no-pager litellm-postgres.service || true
  die "Postgres failed to start. See the status output above and 'journalctl --user -u litellm-postgres.service -n 100'." \
      "Postgres 啟動失敗，請參考上方狀態與 journalctl 記錄。"
fi
ok "litellm-postgres.service started." "litellm-postgres.service 已啟動。"

# Only meaningful on Podman < 5.0; harmless (succeeds immediately) on 5.x.
info "Starting litellm-wait-postgres.service (ordering fallback)" \
     "啟動 litellm-wait-postgres.service（順序後備方案）"
systemctl --user start litellm-wait-postgres.service || \
  warn "The wait unit failed. Continuing - Notify=healthy may already cover this." \
       "等待 unit 失敗，繼續執行 - Notify=healthy 可能已提供保護。"

info "Starting litellm.service" "啟動 litellm.service"
if ! systemctl --user start litellm.service; then
  systemctl --user status --no-pager litellm.service || true
  die "LiteLLM failed to start. See 'journalctl --user -u litellm.service -n 200'." \
      "LiteLLM 啟動失敗，請執行 journalctl --user -u litellm.service -n 200 查看。"
fi
ok "litellm.service started." "litellm.service 已啟動。"

# =============================================================================
# 10. Poll health
# =============================================================================
step "Waiting for health checks" "等待健康檢查通過"

health_of() {
  "$PODMAN_BIN" inspect --format '{{.State.Health.Status}}' "$1" 2>/dev/null || echo "unknown"
}

wait_healthy() {
  local ctr="$1" limit="$2" waited=0 st
  while (( waited < limit )); do
    st="$(health_of "$ctr")"
    case "$st" in
      healthy)   printf '\n'; return 0 ;;
      unhealthy) printf '\n'; return 2 ;;
    esac
    printf '.'
    sleep 5
    waited=$((waited + 5))
  done
  printf '\n'
  return 1
}

PG_RC=0
info "Waiting for litellm-postgres (up to 120s)" "等待 litellm-postgres（最多 120 秒）"
wait_healthy litellm-postgres 120 || PG_RC=$?
case $PG_RC in
  0) ok "litellm-postgres is healthy." "litellm-postgres 健康檢查通過。" ;;
  2) warn "litellm-postgres reports UNHEALTHY." "litellm-postgres 回報為不健康。" ;;
  *) warn "litellm-postgres did not report healthy within 120s." "litellm-postgres 於 120 秒內未回報健康。" ;;
esac

LL_RC=0
info "Waiting for litellm (up to ${HEALTH_TIMEOUT}s - first boot runs DB migrations)" \
     "等待 litellm（最多 ${HEALTH_TIMEOUT} 秒 - 首次啟動需執行資料庫遷移）"
wait_healthy litellm "$HEALTH_TIMEOUT" || LL_RC=$?
case $LL_RC in
  0) ok "litellm is healthy." "litellm 健康檢查通過。" ;;
  2) warn "litellm reports UNHEALTHY." "litellm 回報為不健康。" ;;
  *) warn "litellm did not report healthy within ${HEALTH_TIMEOUT}s." "litellm 於 ${HEALTH_TIMEOUT} 秒內未回報健康。" ;;
esac

# =============================================================================
# Summary
# =============================================================================
printf '\n%s================================ SUMMARY / 摘要 ================================%s\n' "$C_STEP" "$C_RESET"
printf '  Podman            : %s\n' "$PODMAN_VER"
printf '  Quadlet units     : %s\n' "$QUADLET_DIR"
printf '  Fallback unit     : %s/litellm-wait-postgres.service\n' "$USER_UNIT_DIR"
printf '  Config            : %s\n' "$CONFIG_DEST"
printf '  Env file (0600)   : %s\n' "$ENV_DEST"
printf '  Podman network    : litellm-net\n'
printf '  Podman volume     : litellm-pgdata\n'
printf '  postgres health   : %s\n' "$(health_of litellm-postgres)"
printf '  litellm health    : %s\n' "$(health_of litellm)"
printf '  Listening on      : 127.0.0.1:4000 (loopback only, by design)\n'

if [[ $PG_RC -eq 0 && $LL_RC -eq 0 ]]; then
  printf '\n%s  SUCCESS - the gateway is up.  部署成功，閘道器已啟動。%s\n' "$C_OK" "$C_RESET"
  printf '\n  Smoke test / 煙霧測試:\n'
  printf '    curl -s http://127.0.0.1:4000/health/liveliness\n'
  printf '    curl -s http://127.0.0.1:4000/health/readiness\n'
  printf '    curl -s -H "Authorization: Bearer $LITELLM_MASTER_KEY" http://127.0.0.1:4000/v1/models\n'
  printf '\n  Admin UI / 管理介面: http://127.0.0.1:4000/ui  (user: admin, password: LITELLM_MASTER_KEY)\n'
  EXIT_CODE=0
else
  printf '\n%s  PARTIAL - services started but health did not go green.%s\n' "$C_WARN" "$C_RESET"
  printf '%s  部分完成 - 服務已啟動但健康檢查未通過。%s\n' "$C_WARN" "$C_RESET"
  printf '\n  Debug / 除錯:\n'
  printf '    systemctl --user status litellm.service litellm-postgres.service\n'
  printf '    journalctl --user -u litellm.service -n 200 --no-pager\n'
  printf '    podman logs --tail 200 litellm\n'
  printf '    podman inspect --format "{{json .State.Health}}" litellm\n'
  printf '    podman exec litellm getent hosts litellm-postgres\n'
  EXIT_CODE=1
fi

printf '\n  Everyday commands / 常用指令:\n'
printf '    systemctl --user restart litellm.service       # apply a config.yaml change\n'
printf '    journalctl --user -u litellm.service -f        # follow logs\n'
printf '    ./quadlet/uninstall.sh                         # remove (KEEPS the database)\n'
printf '    ./quadlet/uninstall.sh --purge-data            # remove AND destroy the database\n'
printf '\n  NOTE: config.yaml is read only at process start. After editing\n'
printf '        %s run: systemctl --user restart litellm.service\n' "$CONFIG_DEST"
printf '  注意：config.yaml 只在啟動時讀取，修改後請重新啟動服務。\n'
printf '%s================================================================================%s\n' "$C_STEP" "$C_RESET"

exit "${EXIT_CODE}"
