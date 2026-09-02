#!/usr/bin/env bash
# TLS terminator for the local Lightspeed gateway.
#
# A Harbor sandbox cannot reach the host's loopback, and lightspeed-envd refuses
# plain ws:// toward anything but loopback. This runs Caddy in Docker with its
# internal CA, listening on host.docker.internal:${LS_TLS_PORT:-18443} and
# proxying to the local gateway (default host.docker.internal:18080, i.e. the
# host's 127.0.0.1:18080 from `./dev.sh runtime`). The CA root is exported to
# .local/gateway-ca.pem for LIGHTSPEED_HARBOR_ENVD_CA_FILE.
#
# Usage: scripts/local-gateway-tls.sh [up|down|recreate|status]
# `up` is idempotent: it reuses a running terminator and re-exports its CA.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

NAME=ls-benchmark-gateway-tls
LISTEN_PORT="${LS_TLS_PORT:-18443}"
UPSTREAM="${LS_GATEWAY_UPSTREAM:-host.docker.internal:18080}"
CA_OUT=.local/gateway-ca.pem

case "${1:-up}" in
  up)
    mkdir -p .local
    state="$(docker inspect -f '{{.State.Status}}' "$NAME" 2>/dev/null || echo absent)"
    case "$state" in
      running) ;;
      absent)
        docker run -d --name "$NAME" --restart unless-stopped \
          -p "${LISTEN_PORT}:${LISTEN_PORT}" caddy:2 \
          caddy reverse-proxy \
            --from "https://host.docker.internal:${LISTEN_PORT}" \
            --to "http://${UPSTREAM}" \
            --internal-certs >/dev/null
        ;;
      *) docker start "$NAME" >/dev/null ;;
    esac
    for _ in $(seq 1 60); do
      if docker cp "$NAME:/data/caddy/pki/authorities/local/root.crt" "$CA_OUT" >/dev/null 2>&1; then
        break
      fi
      sleep 0.5
    done
    [ -s "$CA_OUT" ] || { echo "Caddy CA root did not appear; see: docker logs $NAME" >&2; exit 1; }
    echo "# TLS terminator ${state/absent/created}: https://host.docker.internal:${LISTEN_PORT} -> http://${UPSTREAM}" >&2
    echo "export LIGHTSPEED_ENVD_GATEWAY_URL=wss://host.docker.internal:${LISTEN_PORT}/environment-gateway/connect"
    echo "export LIGHTSPEED_HARBOR_ENVD_CA_FILE=$(pwd)/${CA_OUT}"
    ;;
  down)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    ;;
  recreate)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    exec "$0" up
    ;;
  status)
    docker ps --filter "name=$NAME" --format '{{.Names}} {{.Status}}'
    ;;
  *)
    echo "usage: $0 [up|down|recreate|status]" >&2
    exit 2
    ;;
esac
