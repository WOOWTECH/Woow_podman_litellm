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
