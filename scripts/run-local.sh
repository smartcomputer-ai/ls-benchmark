#!/usr/bin/env bash
# Run one Harbor job against the local Lightspeed stack, refreshing whatever is
# stale first. Idempotent; safe to run repeatedly.
#
#   scripts/run-local.sh [configs/toy.local.yaml] [extra `harbor run` args...]
#
# Checks or refreshes, in order:
#   1. the uv environment (`uv sync --frozen`);
#   2. the gateway at LIGHTSPEED_API_URL (default http://127.0.0.1:18080/rpc),
#      which must run with LIGHTSPEED_PUBLIC_BASE_URL=https://host.docker.internal:<port>
#      so sandboxed envd can reach its data route;
#   3. the Caddy TLS terminator (scripts/local-gateway-tls.sh up) and its CA file;
#   4. lightspeed-envd for the sandbox architecture, rebuilt when the sibling
#      checkout's HEAD moved, its crates/ tree changed, the binary is missing,
#      or LS_REBUILD_ENVD=1;
#   5. the campaign registration key in .local/registration-key.json, minted when
#      missing, revoked, expired, or expiring within the hour;
# then runs `harbor run -c <config> -y --job-name <config>-<utc timestamp>` and
# prints one line per trial from the job directory.
#
# Overrides: LIGHTSPEED_CHECKOUT (../lightspeed), LIGHTSPEED_API_URL,
# LIGHTSPEED_API_KEY (default `local`; single-mode gateways ignore it),
# LS_TLS_PORT (18443), LS_SANDBOX_ARCH (Docker daemon arch), LS_KEY_HOURS (24).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=common.sh
source scripts/common.sh
LS_LOG_PREFIX=run-local

CONFIG="${1:-configs/toy.local.yaml}"
[ $# -gt 0 ] && shift
[ -f "$CONFIG" ] || { log "config not found: $CONFIG"; exit 2; }
LS="${LIGHTSPEED_CHECKOUT:-../lightspeed}"
[ -f "$LS/Cargo.toml" ] || { log "sibling checkout not found at $LS"; exit 2; }
export LIGHTSPEED_API_URL="${LIGHTSPEED_API_URL:-http://127.0.0.1:18080/rpc}"
export LIGHTSPEED_API_KEY="${LIGHTSPEED_API_KEY:-local}"
TLS_PORT="${LS_TLS_PORT:-18443}"
WANT_BASE="https://host.docker.internal:${TLS_PORT}"

# 1. dependencies -------------------------------------------------------------
log "uv sync --frozen"
uv sync --frozen --quiet

# 2. gateway ------------------------------------------------------------------
if ! require_gateway; then
  log "start the local stack in $LS with:"
  log "  LIGHTSPEED_PUBLIC_BASE_URL=$WANT_BASE ./dev.sh runtime"
  exit 1
fi
pid="$(pgrep -f 'lightspeed-server' | head -1 || true)"
if [ -n "$pid" ]; then
  if [ "$(uname)" = Darwin ]; then
    envtext="$(ps -Eo command= -p "$pid" 2>/dev/null | tr ' ' '\n' || true)"
  else
    envtext="$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null || true)"
  fi
  if grep -q '^LIGHTSPEED_' <<<"$envtext"; then
    base="$(sed -n 's/^LIGHTSPEED_PUBLIC_BASE_URL=//p' <<<"$envtext" | head -1)"
    if [ "$base" != "$WANT_BASE" ]; then
      log "runtime pid $pid has LIGHTSPEED_PUBLIC_BASE_URL='${base:-unset}'; sandboxes need '$WANT_BASE'."
      log "restart it: LIGHTSPEED_PUBLIC_BASE_URL=$WANT_BASE ./dev.sh runtime"
      exit 1
    fi
    log "runtime pid $pid public base url ok"
  else
    log "could not read the runtime's environment to verify LIGHTSPEED_PUBLIC_BASE_URL"
  fi
fi

# 3. TLS terminator -----------------------------------------------------------
eval "$(scripts/local-gateway-tls.sh up)"
curl -sS --max-time 10 --cacert "$LIGHTSPEED_HARBOR_ENVD_CA_FILE" \
  --resolve "host.docker.internal:${TLS_PORT}:127.0.0.1" -o /dev/null \
  "https://host.docker.internal:${TLS_PORT}/rpc" \
  || { log "TLS terminator on :$TLS_PORT is not answering; try scripts/local-gateway-tls.sh recreate"; exit 1; }
log "tls ok ($LIGHTSPEED_ENVD_GATEWAY_URL)"

# 4. envd for the sandbox architecture ----------------------------------------
envd_target "${LS_SANDBOX_ARCH:-$(docker version --format '{{.Server.Arch}}')}"
bin=".local/envd/$TARGET/lightspeed-envd"
stamp=".local/envd/$TARGET/lightspeed-envd.gitsha"
want="$(git -C "$LS" rev-parse HEAD)"
if [ -n "$(git -C "$LS" status --porcelain -- crates)" ]; then
  want="$want-$( (git -C "$LS" diff HEAD -- crates; git -C "$LS" status --porcelain -- crates) | shasum -a 256 | cut -c1-12)"
fi
have="$(cat "$stamp" 2>/dev/null || true)"
if [ ! -x "$bin" ] || [ "$have" != "$want" ] || [ "${LS_REBUILD_ENVD:-0}" = 1 ]; then
  log "envd $TARGET: rebuilding (have ${have:-none}, want $want)"
  eval "$(scripts/build-envd-linux.sh "$ARCH")"
  echo "$want" > "$stamp"
else
  log "envd $TARGET current at ${want:0:12}"
  export LIGHTSPEED_HARBOR_ENVD_PATH="$PWD/$bin"
fi

# 5. registration key ---------------------------------------------------------
ensure_registration_key .local/registration-key.json harbor-local 8 "${LS_KEY_HOURS:-24}"

# 6. run ----------------------------------------------------------------------
run_harbor_job "$CONFIG" "$(basename "$CONFIG" .yaml | tr . -)" "$@"
exit "$HARBOR_STATUS"
