# shellcheck shell=bash
# tests/dryrun.local.sh: repo-specific checks, sourced at the end of tests/dryrun.sh (which
# defines REPO, WORK, APP, failures and run_variant). CI runs it through tests/dryrun.sh.
# shellcheck disable=SC2154 # REPO and failures come from tests/dryrun.sh

local_fail() { echo "FAIL $*"; failures=$((failures + 1)); }
local_ok() { echo "ok   $*"; }

# podman 4.9.3 silently ignores Notify=healthy; the gate is litellm-postgres' ExecStartPost=.
if grep -rn '^[[:space:]]*Notify=' "$REPO/quadlet"; then
  local_fail "Notify= in quadlet/ (ignored by podman 4.9.3; use the ExecStartPost= gate)"
else
  local_ok "no Notify= in quadlet/"
fi
# The user manager has no multi-user.target.
if grep -rn 'multi-user\.target' "$REPO/quadlet"; then local_fail "multi-user.target in quadlet/"; else local_ok "no multi-user.target in quadlet/"; fi
if grep -q '^ExecStartPost=.*pg_isready -h 127\.0\.0\.1' "$REPO/quadlet/litellm-postgres.container" \
  && grep -qx 'Requires=litellm-postgres.service' "$REPO/quadlet/litellm.container"; then
  local_ok "litellm Requires= a postgres unit gated by ExecStartPost= pg_isready over TCP"
else
  local_fail "the readiness gate (ExecStartPost= pg_isready -h 127.0.0.1 + Requires=) is missing"
fi

# config/config.yaml is shared byte-for-byte with Woow_k3s_litellm. Change both, then this pin.
cfg_hash=$(git -C "$REPO" hash-object config/config.yaml)
if [[ $cfg_hash == 4876702dde54e214d71fd6724a303c3f9c70fec6 ]]; then
  local_ok "config/config.yaml matches the pinned k3s copy"
else
  local_fail "config/config.yaml drifted from the pinned k3s copy (hash $cfg_hash)"
fi

# Pinned images only (STANDARD section 3): an exact version, no floating tag.
while IFS= read -r img; do
  if [[ $img =~ :v?[0-9]+\.[0-9]+(\.[0-9]+)?(-[A-Za-z0-9.]+)?$ ]]; then local_ok "pinned $img"; else local_fail "not pinned: $img"; fi
done < <(sed -n 's/^Image=//p' "$REPO"/quadlet/*.container)

# Loopback by default; no cloudflared anywhere (the k3s tunnel token must never be reused).
if grep -qx 'LITELLM_BIND=127.0.0.1' "$REPO/config/litellm.env.example"; then local_ok "example binds 127.0.0.1"; else local_fail "config/litellm.env.example must default to LITELLM_BIND=127.0.0.1"; fi
if grep -rniE '^Image=.*cloudflared' "$REPO/quadlet"; then local_fail "a cloudflared container is defined"; else local_ok "no cloudflared container"; fi

# ------------------------------------------------------------------------------------------
# Regression: --purge-images must never touch an image this package did not build.
#
# It used to collect the Image= lines of the installed and repo units, which for LiteLLM are
# both PINNED UPSTREAM images (ghcr.io/berriai/litellm, docker.io/library/postgres), and
# `podman rmi` them. On a test host that only untagged them, because production pins the
# sibling tag postgres:16.15-alpine with the same image ID; on a host that pins this exact
# tag it would delete a base image a live stack depends on. The omnigent and code-server
# packages only ever remove their own localhost/* images; this now matches them.
#
# The test runs the real scripts/uninstall.sh --purge-images against a stub podman that
# reports both upstream images as present, and fails if either is named for removal.
purge_stub=$WORK/purge-images-stub
mkdir -p "$purge_stub/bin" "$purge_stub/qdir" "$purge_stub/state"
cat >"$purge_stub/bin/podman" <<'STUB'
#!/usr/bin/env bash
# Just enough podman for uninstall.sh --purge-images --dry-run. `images` reports the two
# pinned upstream images as present on the host; `rmi` is a hard failure, because a dry run
# must not reach it and a real run must never be asked to remove one of these.
case ${1:-} in
  --version) echo "podman version 4.9.3" ;;
  images)
    printf 'ghcr.io/berriai/litellm:v1.83.14-stable\n'
    printf 'docker.io/library/postgres:16.15-alpine3.24\n'
    printf 'docker.io/library/postgres:16.15-alpine\n'
    ;;
  image) [[ ${2:-} == exists ]] && exit 0 ;;
  volume | secret | network) [[ ${2:-} == exists ]] && exit 1 ;;
  rmi) echo "STUB-RMI ${*:2}" >&2; exit 0 ;;
  ps) : ;;
  *) : ;;
esac
exit 0
STUB
cat >"$purge_stub/bin/systemctl" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod 755 "$purge_stub/bin/podman" "$purge_stub/bin/systemctl"

purge_out=$WORK/purge-images.out
purge_rc=0
PATH=$purge_stub/bin:$PATH \
  QL_QUADLET_DIR=$purge_stub/qdir QL_STATE_ROOT=$purge_stub/state QL_CONFIG_ROOT=$purge_stub \
  bash "$REPO/scripts/uninstall.sh" --purge-images --dry-run >"$purge_out" 2>&1 || purge_rc=$?
if ((purge_rc != 0)); then
  local_fail "uninstall.sh --purge-images --dry-run exited $purge_rc: $(tr '\n' ' ' <"$purge_out")"
elif grep -qE 'would remove image (ghcr\.io|docker\.io|registry\.|quay\.)' "$purge_out" \
  || grep -q 'STUB-RMI' "$purge_out"; then
  local_fail "--purge-images names an upstream image for removal: $(grep -E 'image|STUB-RMI' "$purge_out" | tr '\n' ' ')"
else
  local_ok "--purge-images leaves the pinned upstream images alone"
fi
