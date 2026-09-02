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

CONFIG="${1:-configs/toy.local.yaml}"
[ $# -gt 0 ] && shift
[ -f "$CONFIG" ] || { echo "run-local: config not found: $CONFIG" >&2; exit 2; }
LS="${LIGHTSPEED_CHECKOUT:-../lightspeed}"
[ -f "$LS/Cargo.toml" ] || { echo "run-local: sibling checkout not found at $LS" >&2; exit 2; }
export LIGHTSPEED_API_URL="${LIGHTSPEED_API_URL:-http://127.0.0.1:18080/rpc}"
export LIGHTSPEED_API_KEY="${LIGHTSPEED_API_KEY:-local}"
TLS_PORT="${LS_TLS_PORT:-18443}"
KEY_FILE=.local/registration-key.json
KEY_HOURS="${LS_KEY_HOURS:-24}"
WANT_BASE="https://host.docker.internal:${TLS_PORT}"

log() { echo "run-local: $*" >&2; }
rpc() { # rpc <method> <params-json> [timeout-sec]
  curl -sS --max-time "${3:-30}" -X POST "$LIGHTSPEED_API_URL" \
    -H 'content-type: application/json' -H "authorization: Bearer ${LIGHTSPEED_API_KEY}" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$1\",\"params\":$2}"
}
json_get() { # json_get <file-or-'-'> <python expression over d>
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1]) if sys.argv[1] != "-" else sys.stdin); print(eval(sys.argv[2]))' "$1" "$2"
}

# 1. dependencies -------------------------------------------------------------
log "uv sync --frozen"
uv sync --frozen --quiet

# 2. gateway ------------------------------------------------------------------
if ! rpc initialize '{}' 5 2>/dev/null | grep -q '"protocolVersion"'; then
  log "no Lightspeed gateway answering at $LIGHTSPEED_API_URL. In $LS run:"
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
    log "gateway ok (pid $pid, public base url $base)"
  else
    log "gateway ok; could not read the runtime's environment to verify LIGHTSPEED_PUBLIC_BASE_URL"
  fi
else
  log "gateway ok (not a local lightspeed-server process; skipping the public base url check)"
fi

# 3. TLS terminator -----------------------------------------------------------
eval "$(scripts/local-gateway-tls.sh up)"
curl -sS --max-time 10 --cacert "$LIGHTSPEED_HARBOR_ENVD_CA_FILE" \
  --resolve "host.docker.internal:${TLS_PORT}:127.0.0.1" -o /dev/null \
  "https://host.docker.internal:${TLS_PORT}/rpc" \
  || { log "TLS terminator on :$TLS_PORT is not answering; try scripts/local-gateway-tls.sh recreate"; exit 1; }
log "tls ok ($LIGHTSPEED_ENVD_GATEWAY_URL)"

# 4. envd for the sandbox architecture ----------------------------------------
arch="${LS_SANDBOX_ARCH:-$(docker version --format '{{.Server.Arch}}')}"
case "$arch" in
  arm64|aarch64) arch=arm64; target=aarch64-unknown-linux-gnu ;;
  amd64|x86_64) arch=amd64; target=x86_64-unknown-linux-gnu ;;
  *) log "unknown sandbox architecture: $arch"; exit 2 ;;
esac
bin=".local/envd/$target/lightspeed-envd"
stamp=".local/envd/$target/lightspeed-envd.gitsha"
want="$(git -C "$LS" rev-parse HEAD)"
if [ -n "$(git -C "$LS" status --porcelain -- crates)" ]; then
  want="$want-$( (git -C "$LS" diff HEAD -- crates; git -C "$LS" status --porcelain -- crates) | shasum -a 256 | cut -c1-12)"
fi
have="$(cat "$stamp" 2>/dev/null || true)"
if [ ! -x "$bin" ] || [ "$have" != "$want" ] || [ "${LS_REBUILD_ENVD:-0}" = 1 ]; then
  log "envd $target: rebuilding (have ${have:-none}, want $want)"
  eval "$(scripts/build-envd-linux.sh "$arch")"
  echo "$want" > "$stamp"
else
  log "envd $target current at ${want:0:12}"
  export LIGHTSPEED_HARBOR_ENVD_PATH="$PWD/$bin"
fi

# 5. registration key ---------------------------------------------------------
now_ms=$(( $(date +%s) * 1000 ))
key_ok=false
if [ -f "$KEY_FILE" ]; then
  key_id="$(json_get "$KEY_FILE" 'd["result"]["result"]["registrationKey"]["registrationKeyId"]' 2>/dev/null || true)"
  if [ -n "$key_id" ]; then
    read -r st exp <<<"$(rpc environments/registration-keys/read "{\"registrationKeyId\":\"$key_id\"}" 2>/dev/null \
      | json_get - 'str(d["result"]["result"]["registrationKey"]["status"]) + " " + str(d["result"]["result"]["registrationKey"].get("expiresAtMs") or 0)' 2>/dev/null \
      || echo "unknown 0")"
    if [ "$st" = active ] && { [ "$exp" = 0 ] || [ "$exp" -gt $(( now_ms + 3600000 )) ]; }; then
      key_ok=true
      log "registration key $key_id active"
    else
      log "registration key $key_id is $st; minting a new one"
    fi
  fi
fi
if [ "$key_ok" != true ]; then
  mkdir -p .local
  rpc environments/registration-keys/create \
    "{\"displayName\":\"harbor-local\",\"identityMode\":\"ephemeral\",\"maxActiveEnvironments\":8,\"expiresAtMs\":$(( now_ms + KEY_HOURS * 3600000 ))}" \
    > "$KEY_FILE.tmp"
  if ! json_get "$KEY_FILE.tmp" 'd["result"]["result"]["secret"][:5]' >/dev/null 2>&1; then
    log "registration key creation failed: $(cat "$KEY_FILE.tmp")"; rm -f "$KEY_FILE.tmp"; exit 1
  fi
  mv "$KEY_FILE.tmp" "$KEY_FILE"; chmod 600 "$KEY_FILE"
  log "minted registration key $(json_get "$KEY_FILE" 'd["result"]["result"]["registrationKey"]["registrationKeyId"]') (ephemeral, ${KEY_HOURS}h)"
fi
LIGHTSPEED_HARBOR_REGISTRATION_KEY="$(json_get "$KEY_FILE" 'd["result"]["result"]["secret"]')"
export LIGHTSPEED_HARBOR_REGISTRATION_KEY

# 6. run ----------------------------------------------------------------------
jobs_dir="$(uv run python -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1])).get("jobs_dir") or "jobs")' "$CONFIG")"
job_name="$(basename "$CONFIG" .yaml | tr . -)-$(date -u +%Y%m%d-%H%M%S)"
log "harbor run -c $CONFIG -y --job-name $job_name $*"
set +e
uv run harbor run -c "$CONFIG" -y --job-name "$job_name" "$@"
status=$?
set -e

# 7. summary ------------------------------------------------------------------
uv run python - "$jobs_dir/$job_name" <<'PY'
import json
import sys
from pathlib import Path

job = Path(sys.argv[1])
results = sorted(job.glob("*/result.json"))
print(f"run-local: {len(results)} trial(s) in {job}")
for result in results:
    r = json.loads(result.read_text())
    agent = (r.get("agent_info") or {}).get("name")
    reward = ((r.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    exc = (r.get("exception_info") or {}).get("exception_type")
    ar = r.get("agent_result") or {}
    ls = (ar.get("metadata") or {}).get("lightspeed") or {}
    extra = ""
    if ls:
        extra = (
            f" lightspeed={ls.get('status')} tokens={ar.get('n_input_tokens')}/{ar.get('n_output_tokens')}"
            + (f" error={ls.get('error')}" if ls.get("error") else "")
        )
    print(f"  {result.parent.name:40s} {agent!s:12s} reward={reward} exception={exc}{extra}")
PY
exit "$status"
