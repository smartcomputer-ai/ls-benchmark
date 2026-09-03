#!/usr/bin/env bash
# Run one Harbor job against hosted Lightspeed (ls.bot) from this machine.
# Sandboxes run wherever the job config's `environment` says (local Docker by
# default); envd inside them dials the public gateway, and the adapter talks
# to the public api-key RPC. Idempotent; safe to run repeatedly.
#
#   scripts/run-hosted.sh [configs/toy.local.yaml] [extra `harbor run` args...]
#
# Operator hand-off, read from .local/hosted.env (gitignored):
#   LIGHTSPEED_API_URL=https://ls.bot/rpc
#   LIGHTSPEED_API_KEY=lsk_...            # bound to the evaluation universe
#   LS_UNIVERSE_ID=<uuid>                 # recorded in provenance only
#   LIGHTSPEED_ENVD_GATEWAY_URL=wss://ls.bot/environment-gateway/connect
#
# Checks or refreshes, in order: the uv environment, the gateway with the key,
# the envd binary for the sandbox architecture (amd64: the adapter downloads
# the deployment's published musl archive and verifies it against the server's
# build; arm64: built here from a checkout at the server's commit), and the
# campaign registration key in .local/hosted-registration-key.json
# (minted through the public RPC when missing, revoked, expired, or expiring
# within the hour; LS_KEY_MAX_ACTIVE default 8, LS_KEY_HOURS default 24).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=common.sh
source scripts/common.sh
LS_LOG_PREFIX=run-hosted

CONFIG="${1:-configs/toy.local.yaml}"
[ $# -gt 0 ] && shift
[ -f "$CONFIG" ] || { log "config not found: $CONFIG"; exit 2; }
ENV_FILE=.local/hosted.env
[ -f "$ENV_FILE" ] || { log "missing $ENV_FILE (operator hand-off); see the header of this script"; exit 2; }
set -a; # shellcheck disable=SC1090
source "$ENV_FILE"; set +a
: "${LIGHTSPEED_API_URL:?missing in $ENV_FILE}" "${LIGHTSPEED_API_KEY:?missing in $ENV_FILE}"
: "${LIGHTSPEED_ENVD_GATEWAY_URL:?missing in $ENV_FILE}"
unset LIGHTSPEED_HARBOR_ENVD_CA_FILE LIGHTSPEED_UNIVERSE   # public TLS, api-key mode

log "uv sync --frozen"
uv sync --frozen --quiet
require_gateway

# envd: the sandbox daemon must be the server's build. The adapter resolves
# the archive from the deployment's discovery document (P152) and refuses a
# mismatch; only arm64 development daemons (no published musl aarch64 artifact
# yet) are built here from a checkout at the server's commit.
deployed="$(server_git_sha)"
[ -n "$deployed" ] || { log "server does not report its build (gitSha); cannot verify envd"; exit 1; }
envd_target "${LS_SANDBOX_ARCH:-$(docker version --format '{{.Server.Arch}}')}"
if [ -n "${LIGHTSPEED_HARBOR_ENVD_PATH:-}" ]; then
  log "envd: using LIGHTSPEED_HARBOR_ENVD_PATH=$LIGHTSPEED_HARBOR_ENVD_PATH (adapter checks it against ${deployed:0:12})"
elif [ "$ARCH" = amd64 ]; then
  unset LIGHTSPEED_HARBOR_ENVD_PATH
  log "envd: $TARGET from ${LIGHTSPEED_HARBOR_ENVD_DISCOVERY_URL:-the discovery document} (server ${deployed:0:12})"
else
  bin=".local/envd/$TARGET/lightspeed-envd"
  stamp=".local/envd/$TARGET/lightspeed-envd.gitsha"
  have="$(cat "$stamp" 2>/dev/null || true)"
  if [ ! -x "$bin" ] || [ "$have" != "$deployed" ]; then
    LS="${LIGHTSPEED_CHECKOUT:-../lightspeed}"
    if [ "$(git -C "$LS" rev-parse HEAD 2>/dev/null)" = "$deployed" ] && [ -z "$(git -C "$LS" status --porcelain -- crates)" ]; then
      log "envd $TARGET: building from the server's commit ${deployed:0:12}"
      eval "$(scripts/build-envd-linux.sh "$ARCH")"
      echo "$deployed" > "$stamp"
    else
      log "envd $TARGET is built from ${have:-nothing}, server runs $deployed."
      log "check out $deployed in $LS (clean crates/) and rerun, or run scripts/build-envd-linux.sh $ARCH there"
      exit 1
    fi
  else
    log "envd $TARGET matches server build ${deployed:0:12}"
    export LIGHTSPEED_HARBOR_ENVD_PATH="$PWD/$bin"
  fi
fi

ensure_registration_key .local/hosted-registration-key.json "harbor-${USER:-ls-benchmark}" "${LS_KEY_MAX_ACTIVE:-8}" "${LS_KEY_HOURS:-24}"

run_harbor_job "$CONFIG" "$(basename "$CONFIG" .yaml | tr . -)-hosted" "$@"
exit "$HARBOR_STATUS"
