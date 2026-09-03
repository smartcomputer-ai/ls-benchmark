#!/usr/bin/env bash
# Drive Harbor jobs on the hz02 runner VM from this machine. Harbor and the
# adapter run inside the VM (its Docker daemon holds the amd64 sandboxes);
# this script only syncs the repository over, starts a detached run, and
# fetches results back.
#
#   scripts/run-remote.sh sync                       # rsync repo + .local hand-off to the VM
#   scripts/run-remote.sh start <config> [harbor args]   # sync, then start a detached run
#   scripts/run-remote.sh status                     # running job, log tail, trial counts
#   scripts/run-remote.sh log [lines]                # tail of the newest run log
#   scripts/run-remote.sh fetch [job-name]           # rsync jobs/<job> back into jobs/
#   scripts/run-remote.sh stop                       # SIGINT, then SIGTERM, the running harbor; remove leftover sandboxes
#   scripts/run-remote.sh ssh [cmd...]               # shell on the VM
#
# The VM needs only https://ls.bot: the adapter downloads the deployment's
# published envd archive and checks it against the server's build (P152).
# Overrides: LS_RUNNER (ssh host, harbor-runner), LS_REMOTE_DIR (~/ls-benchmark).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

RUNNER="${LS_RUNNER:-harbor-runner}"
REMOTE_DIR="${LS_REMOTE_DIR:-ls-benchmark}"
RUN_DIR=".local/runs"

log() { echo "run-remote: $*" >&2; }
rssh() { ssh -o BatchMode=yes -o ConnectTimeout=20 "$RUNNER" "$@"; }

sync_repo() {
  [ -f .local/hosted.env ] || { log "missing .local/hosted.env (operator hand-off)"; exit 2; }
  rssh "mkdir -p '$REMOTE_DIR/.local/envd' '$REMOTE_DIR/$RUN_DIR' && chmod 700 '$REMOTE_DIR/.local'"
  rsync -az --delete \
    --exclude .git --exclude .venv --exclude jobs --exclude .local --exclude '__pycache__' \
    --exclude .pytest_cache --exclude .ruff_cache \
    ./ "$RUNNER:$REMOTE_DIR/"
  rsync -az .local/hosted.env "$RUNNER:$REMOTE_DIR/.local/hosted.env"
  rssh "chmod 600 '$REMOTE_DIR/.local/hosted.env'"
  # GitHub throttles unauthenticated clones from the VM's Hetzner address, so
  # ship Harbor's task cache (harbor dataset download <name>@<version> --cache
  # here first); the job then finds every task cached and clones nothing.
  if [ -d "$HOME/.cache/harbor/tasks" ]; then
    rssh "mkdir -p .cache/harbor/tasks"
    rsync -az "$HOME/.cache/harbor/tasks/" "$RUNNER:.cache/harbor/tasks/"
    log "task cache synced ($(find "$HOME/.cache/harbor/tasks" -maxdepth 3 -name task.toml | wc -l | tr -d ' ') tasks)"
  fi
  log "synced to $RUNNER:$REMOTE_DIR"
}

case "${1:-status}" in
  sync)
    sync_repo
    ;;
  start)
    shift
    config="${1:?usage: run-remote.sh start <config> [harbor args]}"; shift
    sync_repo
    if rssh "pgrep -f '[h]arbor run' >/dev/null"; then
      log "a harbor run is already active on $RUNNER; use status or stop"; exit 1
    fi
    ts="$(date -u +%Y%m%d-%H%M%S)"
    printf -v quoted ' %q' "$@"
    # `cd` and `export` run in the login shell; only the runner is backgrounded.
    # `set -m`: without job control a non-interactive shell starts `&` jobs
    # with SIGINT ignored, and Harbor inherits that, so `stop` could not
    # interrupt it (2026-09-03).
    rssh "cd '$REMOTE_DIR' || exit 1; export PATH=\"\$HOME/.local/bin:\$PATH\" LS_SANDBOX_ARCH=amd64 ${LS_REMOTE_ENV:-}; set -m; \
      nohup setsid scripts/run-hosted.sh '$config'$quoted > '$RUN_DIR/$ts.log' 2>&1 < /dev/null & \
      echo \$! > '$RUN_DIR/$ts.pid'; echo started run $ts pid \$(cat '$RUN_DIR/$ts.pid')"
    ;;
  status)
    rssh "cd '$REMOTE_DIR' 2>/dev/null || exit 0; latest=\$(ls -t $RUN_DIR/*.log 2>/dev/null | head -1); \
      if pgrep -f '[h]arbor run' >/dev/null; then echo \"harbor: running (\$(pgrep -fc '[h]arbor run') processes)\"; else echo 'harbor: not running'; fi; \
      echo \"latest log: \$latest\"; [ -n \"\$latest\" ] && grep -E 'run-hosted:|trial\\(s\\)|reward=|exit=' \"\$latest\" | tail -8; \
      for j in \$(ls -td jobs/*/ 2>/dev/null | head -1); do echo \"newest job: \$j\"; echo \"  trials with results: \$(ls \$j/*/result.json 2>/dev/null | wc -l)\"; done; \
      echo \"containers: \$(docker ps -q | wc -l) running\"; df -h / | tail -1"
    ;;
  log)
    rssh "cd '$REMOTE_DIR' && tail -n ${2:-40} \$(ls -t $RUN_DIR/*.log | head -1)"
    ;;
  fetch)
    job="${2:-$(rssh "cd '$REMOTE_DIR' && ls -t jobs | head -1")}"
    [ -n "$job" ] || { log "no job to fetch"; exit 1; }
    mkdir -p jobs
    rsync -az "$RUNNER:$REMOTE_DIR/jobs/$job" jobs/
    log "fetched jobs/$job"
    ;;
  stop)
    # SIGINT lets Harbor cancel trials and the adapter cancel its runs; after
    # LS_STOP_GRACE_SEC (60) SIGTERM ends it, and any sandbox Harbor left
    # behind is removed. Sessions whose trial was cut off stay open on the
    # Lightspeed side until their retention runs out; close them with
    # session/close if they matter.
    rssh "if ! pkill -INT -f '[h]arbor run'; then echo 'no harbor run active'; else \
      echo 'sent SIGINT to harbor'; \
      for i in \$(seq 1 ${LS_STOP_GRACE_SEC:-60}); do pgrep -f '[h]arbor run' >/dev/null || break; sleep 1; done; \
      if pgrep -f '[h]arbor run' >/dev/null; then pkill -TERM -f '[h]arbor run'; sleep 3; echo 'sent SIGTERM to harbor'; fi; \
      pgrep -f '[h]arbor run' >/dev/null && echo 'harbor still running' || echo 'harbor stopped'; fi; \
      left=\$(docker ps -q); if [ -n \"\$left\" ]; then docker rm -f \$left >/dev/null && echo \"removed \$(echo \$left | wc -w | tr -d ' ') leftover container(s)\"; fi; \
      docker network prune -f >/dev/null 2>&1 || true"
    ;;
  ssh)
    shift
    exec ssh "$RUNNER" "$@"
    ;;
  *) echo "usage: $0 {sync|start <config> [args]|status|log [n]|fetch [job]|stop|ssh [cmd]}" >&2; exit 2 ;;
esac
