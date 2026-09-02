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
#   LS_DEPLOYED_GIT_SHA=<commit>          # optional; else read from hz01 over ssh
#
# Checks or refreshes, in order: the uv environment, the gateway with the key,
# the envd binary for the sandbox architecture (must be built from the
# deployed release commit; LS_PROD_HOST, default hz01, is asked for it over
# ssh), and the campaign registration key in .local/hosted-registration-key.json
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

# envd: the sandbox binary must come from the commit that is deployed.
deployed="${LS_DEPLOYED_GIT_SHA:-$(ssh -o BatchMode=yes -o ConnectTimeout=15 "${LS_PROD_HOST:-hz01}" \
  "sed -n 's/^LIGHTSPEED_RELEASE_GIT_SHA=//p' /var/lib/ls-deploy/last-good.env")}"
[ -n "$deployed" ] || { log "could not determine the deployed release commit; set LS_DEPLOYED_GIT_SHA"; exit 1; }
envd_target "${LS_SANDBOX_ARCH:-$(docker version --format '{{.Server.Arch}}')}"
bin=".local/envd/$TARGET/lightspeed-envd"
stamp=".local/envd/$TARGET/lightspeed-envd.gitsha"
have="$(cat "$stamp" 2>/dev/null || true)"
if [ ! -x "$bin" ] || [ "$have" != "$deployed" ]; then
  LS="${LIGHTSPEED_CHECKOUT:-../lightspeed}"
  if [ "$(git -C "$LS" rev-parse HEAD 2>/dev/null)" = "$deployed" ] && [ -z "$(git -C "$LS" status --porcelain -- crates)" ]; then
    log "envd $TARGET: building from deployed commit ${deployed:0:12}"
    eval "$(scripts/build-envd-linux.sh "$ARCH")"
    echo "$deployed" > "$stamp"
  else
    log "envd $TARGET is built from ${have:-nothing}, deployed release is $deployed."
    log "check out $deployed in $LS (clean crates/) and rerun, or run scripts/build-envd-linux.sh $ARCH there"
    exit 1
  fi
else
  log "envd $TARGET matches deployed release ${deployed:0:12}"
  export LIGHTSPEED_HARBOR_ENVD_PATH="$PWD/$bin"
fi
export LIGHTSPEED_HARBOR_ENVD_VERSION="${deployed:0:12}"

ensure_registration_key .local/hosted-registration-key.json "harbor-${USER:-ls-benchmark}" "${LS_KEY_MAX_ACTIVE:-8}" "${LS_KEY_HOURS:-24}"

run_harbor_job "$CONFIG" "$(basename "$CONFIG" .yaml | tr . -)-hosted" "$@"
exit "$HARBOR_STATUS"
