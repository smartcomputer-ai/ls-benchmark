#!/usr/bin/env bash
# Shared pieces of scripts/run-local.sh and scripts/run-hosted.sh. Source it;
# every function reads LIGHTSPEED_API_URL and LIGHTSPEED_API_KEY from the
# environment.

log() { echo "${LS_LOG_PREFIX:-ls-benchmark}: $*" >&2; }

rpc() { # rpc <method> <params-json> [timeout-sec]
  curl -sS --max-time "${3:-30}" -X POST "$LIGHTSPEED_API_URL" \
    -H 'content-type: application/json' \
    -H "authorization: Bearer ${LIGHTSPEED_API_KEY:-}" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$1\",\"params\":$2}"
}

json_get() { # json_get <file-or-'-'> <python expression over d>
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1]) if sys.argv[1] != "-" else sys.stdin); print(eval(sys.argv[2]))' "$1" "$2"
}

# Fail unless the gateway answers `initialize` with the configured key.
require_gateway() {
  local response
  response="$(rpc initialize '{}' 10 2>/dev/null || true)"
  if ! grep -q '"protocolVersion"' <<<"$response"; then
    log "gateway at $LIGHTSPEED_API_URL did not accept the request: ${response:0:200}"
    return 1
  fi
  log "gateway ok: $(json_get - 'd["result"]["result"]["serverInfo"]["name"] + " " + d["result"]["result"]["serverInfo"]["version"]' <<<"$response")"
}

# Map an architecture name to the envd target triple. Sets ARCH and TARGET.
envd_target() {
  case "$1" in
    arm64|aarch64) ARCH=arm64; TARGET=aarch64-unknown-linux-gnu ;;
    amd64|x86_64) ARCH=amd64; TARGET=x86_64-unknown-linux-musl ;;
    *) log "unknown sandbox architecture: $1"; return 2 ;;
  esac
}

# Reuse the registration key stored in <file> while it is active with more
# than an hour left; otherwise mint an ephemeral one. Exports
# LIGHTSPEED_HARBOR_REGISTRATION_KEY and sets REGISTRATION_KEY_ID.
ensure_registration_key() { # ensure_registration_key <file> <display-name> <max-active> <hours>
  local file="$1" name="$2" max_active="$3" hours="$4"
  local now_ms key_ok=false key_id st exp
  now_ms=$(( $(date +%s) * 1000 ))
  if [ -f "$file" ]; then
    key_id="$(json_get "$file" 'd["result"]["result"]["registrationKey"]["registrationKeyId"]' 2>/dev/null || true)"
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
    mkdir -p "$(dirname "$file")"
    rpc environments/registration-keys/create \
      "{\"displayName\":\"$name\",\"identityMode\":\"ephemeral\",\"maxActiveEnvironments\":$max_active,\"expiresAtMs\":$(( now_ms + hours * 3600000 ))}" \
      > "$file.tmp"
    if ! json_get "$file.tmp" 'd["result"]["result"]["secret"][:5]' >/dev/null 2>&1; then
      log "registration key creation failed: $(cat "$file.tmp")"; rm -f "$file.tmp"; return 1
    fi
    mv "$file.tmp" "$file"; chmod 600 "$file"
    log "minted registration key $(json_get "$file" 'd["result"]["result"]["registrationKey"]["registrationKeyId"]') (ephemeral, max active $max_active, ${hours}h)"
  fi
  REGISTRATION_KEY_ID="$(json_get "$file" 'd["result"]["result"]["registrationKey"]["registrationKeyId"]')"
  LIGHTSPEED_HARBOR_REGISTRATION_KEY="$(json_get "$file" 'd["result"]["result"]["secret"]')"
  export LIGHTSPEED_HARBOR_REGISTRATION_KEY
}

# Run one Harbor job under a timestamped name and print one line per trial.
# Sets HARBOR_STATUS and JOB_DIR.
run_harbor_job() { # run_harbor_job <config> <name-prefix> [extra harbor args...]
  local config="$1" prefix="$2"; shift 2
  local jobs_dir job_name
  jobs_dir="$(uv run python -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1])).get("jobs_dir") or "jobs")' "$config")"
  job_name="${prefix}-$(date -u +%Y%m%d-%H%M%S)"
  JOB_DIR="$jobs_dir/$job_name"
  log "harbor run -c $config -y --job-name $job_name $*"
  set +e
  uv run harbor run -c "$config" -y --job-name "$job_name" "$@"
  HARBOR_STATUS=$?
  set -e
  uv run python - "$JOB_DIR" <<'PY'
import json
import sys
from pathlib import Path

job = Path(sys.argv[1])
results = sorted(job.glob("*/result.json"))
print(f"{len(results)} trial(s) in {job}")
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
}
