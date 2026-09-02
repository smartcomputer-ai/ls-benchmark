#!/usr/bin/env bash
# The `harbor-runner` VM on hz02: an isolated Incus virtual machine with its own
# Docker daemon where Harbor runs the amd64 Terminal-Bench sandboxes. The
# laptop drives it over SSH (ProxyJump through hz02); envd inside the sandboxes
# dials the public ls.bot gateway; the adapter talks to https://ls.bot/rpc.
#
#   scripts/hz02-runner.sh create      # launch + cloud-init (Docker CE, uv, git, rsync, tmux)
#   scripts/hz02-runner.sh status      # incus state, cloud-init, docker, disk
#   scripts/hz02-runner.sh ip          # the VM's bridge address
#   scripts/hz02-runner.sh ssh-config  # the ~/.ssh/config block to append
#   scripts/hz02-runner.sh destroy     # requires LS_RUNNER_DESTROY=1
#
# Overrides: LS_RUNNER_HOST (hz02), LS_RUNNER_NAME (harbor-runner),
# LS_RUNNER_CPU (24), LS_RUNNER_MEM (96GiB), LS_RUNNER_DISK (500GiB),
# LS_RUNNER_IMAGE (images:ubuntu/24.04/cloud), LS_RUNNER_PUBKEY (the key
# ~/.ssh/config uses for hz02), LS_RUNNER_USER (harbor).
set -euo pipefail

HOST="${LS_RUNNER_HOST:-hz02}"
NAME="${LS_RUNNER_NAME:-harbor-runner}"
CPU="${LS_RUNNER_CPU:-24}"
MEM="${LS_RUNNER_MEM:-96GiB}"
DISK="${LS_RUNNER_DISK:-500GiB}"
IMAGE="${LS_RUNNER_IMAGE:-images:ubuntu/24.04/cloud}"
USER_NAME="${LS_RUNNER_USER:-harbor}"

log() { echo "hz02-runner: $*" >&2; }
remote() { ssh -o BatchMode=yes -o ConnectTimeout=20 "$HOST" "$@"; }

pubkey_path() {
  if [ -n "${LS_RUNNER_PUBKEY:-}" ]; then echo "$LS_RUNNER_PUBKEY"; return; fi
  local key
  key="$(awk -v host="$HOST" '$1=="Host" && $2==host {f=1; next} $1=="Host" {f=0} f && $1=="IdentityFile" {print $2; exit}' ~/.ssh/config)"
  key="${key/#\~/$HOME}"
  [ -n "$key" ] || { log "no IdentityFile for Host $HOST in ~/.ssh/config; set LS_RUNNER_PUBKEY"; exit 2; }
  echo "$key.pub"
}

vm_ip() {
  remote "incus list '$NAME' -c 4 -f csv" | tr ',' '\n' | grep -oE '10\.[0-9]+\.[0-9]+\.[0-9]+' | head -1
}

render_instance_yaml() {
  local pubkey
  pubkey="$(cat "$(pubkey_path)")"
  cat <<YAML
config:
  limits.cpu: "$CPU"
  limits.memory: $MEM
  boot.autostart: "true"
  user.user-data: |
    #cloud-config
    hostname: $NAME
    ssh_pwauth: false
    users:
      - name: $USER_NAME
        shell: /bin/bash
        groups: [sudo]
        sudo: ALL=(ALL) NOPASSWD:ALL
        ssh_authorized_keys:
          - $pubkey
    package_update: true
    package_upgrade: false
    packages: [openssh-server, ca-certificates, curl, git, rsync, gnupg, tmux, jq, python3, unzip, build-essential]
    write_files:
      - path: /etc/docker/daemon.json
        content: |
          {"log-driver": "json-file", "log-opts": {"max-size": "50m", "max-file": "3"}}
      - path: /etc/sysctl.d/90-harbor.conf
        content: |
          fs.inotify.max_user_instances = 8192
          fs.inotify.max_user_watches = 1048576
    runcmd:
      - install -m 0755 -d /etc/apt/keyrings
      - curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
      - echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" > /etc/apt/sources.list.d/docker.list
      - apt-get update
      - apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
      - usermod -aG docker $USER_NAME
      - systemctl enable --now docker
      - sysctl --system
      - sudo -u $USER_NAME sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
      # cloud-init leaves the account locked ('!'), which Ubuntu's sshd
      # refuses even for key auth; '*' is invalid-but-unlocked.
      - usermod -p '*' $USER_NAME
      - systemctl enable --now ssh
      - touch /var/lib/cloud/harbor-runner-ready
devices:
  root:
    path: /
    pool: default
    size: $DISK
    type: disk
YAML
}

case "${1:-status}" in
  create)
    if remote "incus info '$NAME' >/dev/null 2>&1"; then
      log "$NAME already exists on $HOST; use status or destroy"; exit 1
    fi
    log "launching $NAME on $HOST ($CPU CPU, $MEM, $DISK, $IMAGE)"
    render_instance_yaml | remote "incus launch '$IMAGE' '$NAME' --vm" >/dev/null
    log "waiting for the agent and cloud-init"
    for _ in $(seq 1 120); do
      if remote "incus exec '$NAME' -- test -f /var/lib/cloud/harbor-runner-ready" 2>/dev/null; then break; fi
      sleep 5
    done
    remote "incus exec '$NAME' -- cloud-init status --long" | sed 's/^/  /' >&2 || true
    remote "incus exec '$NAME' -- docker --version" >&2
    log "ready: ip $(vm_ip)"
    "$0" ssh-config
    ;;
  status)
    remote "incus list '$NAME' -c ns4mD"
    remote "incus exec '$NAME' -- sh -c 'cloud-init status; docker --version; docker compose version; df -h / | tail -1; nproc; free -g | head -2'" 2>&1 | sed 's/^/  /'
    ;;
  ip) vm_ip ;;
  ssh-config)
    ip="$(vm_ip)"
    cat <<SSH

# ls-benchmark runner on $HOST (scripts/hz02-runner.sh)
Host $NAME
    HostName $ip
    User $USER_NAME
    ProxyJump $HOST
    IdentityFile $(pubkey_path | sed "s/\.pub$//")
SSH
    ;;
  destroy)
    [ "${LS_RUNNER_DESTROY:-}" = 1 ] || { log "set LS_RUNNER_DESTROY=1 to delete $NAME and its disk"; exit 2; }
    remote "incus delete --force '$NAME'"
    log "$NAME deleted"
    ;;
  *) echo "usage: $0 {create|status|ip|ssh-config|destroy}" >&2; exit 2 ;;
esac
