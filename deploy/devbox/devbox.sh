#!/usr/bin/env bash
# Helper for the remote dev server. Code is edited on the laptop and mirrored to the box with rsync;
# Docker, tests and Bedrock calls run on the box. Git stays on the laptop.
#
#   deploy/devbox/devbox.sh up        create/update the box (terraform) and wait until it is ready
#   deploy/devbox/devbox.sh sync      mirror the working tree to the box
#   deploy/devbox/devbox.sh run CMD   sync, then run CMD in the repo on the box
#   deploy/devbox/devbox.sh ssh       interactive shell on the box
#   deploy/devbox/devbox.sh tunnel    forward the cockpit and front doors to localhost (Ctrl-C to stop)
#   deploy/devbox/devbox.sh destroy   delete the box and its IAM role
#
# Needs: terraform, the AWS CLI with session-manager-plugin, and AWS SSO credentials
# (AWS_PROFILE, default Users-409239147779).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
export AWS_PROFILE="${AWS_PROFILE:-Users-409239147779}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
KEY="$HOME/.ssh/copperline-devbox"
SSH_CONFIG="$HERE/.ssh_config"          # generated, gitignored
REMOTE_DIR="/home/ubuntu/xc-agentic-demo"

instance_id() { terraform -chdir="$HERE" output -raw instance_id; }

write_ssh_config() {
  cat > "$SSH_CONFIG" <<EOF
Host copperline-devbox
  HostName $(instance_id)
  User ubuntu
  IdentityFile $KEY
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
  UserKnownHostsFile $HERE/.known_hosts
  ProxyCommand sh -c "AWS_PROFILE=$AWS_PROFILE aws ssm start-session --region $AWS_REGION --target %h --document-name AWS-StartSSHSession --parameters portNumber=%p"
  ControlMaster auto
  ControlPath /tmp/copperline-devbox-%C
  ControlPersist 30m
  ServerAliveInterval 30
EOF
}

ssh_box() { ssh -F "$SSH_CONFIG" copperline-devbox "$@"; }

cmd_up() {
  [ -f "$KEY" ] || ssh-keygen -t ed25519 -N "" -C copperline-devbox -f "$KEY"
  terraform -chdir="$HERE" init -input=false >/dev/null
  terraform -chdir="$HERE" apply -input=false -auto-approve \
    -var "owner=${DEVBOX_OWNER:-$(whoami)}" \
    -var "expires=${DEVBOX_EXPIRES:-2026-12-31}" \
    -var "ssh_public_key=$(cat "$KEY.pub")"
  write_ssh_config
  echo "waiting for SSM agent and first-boot setup (about 3-5 minutes on a new box)..."
  for _ in $(seq 1 60); do
    if ssh_box -o ConnectTimeout=20 test -f /var/lib/copperline-devbox-ready 2>/dev/null; then
      ssh_box 'docker --version && ~/.local/bin/uv --version'
      echo "devbox ready: $(instance_id)"
      return 0
    fi
    sleep 10
  done
  echo "devbox not ready after 10 minutes; check /var/log/cloud-init-output.log via '$0 ssh'" >&2
  return 1
}

cmd_sync() {
  [ -f "$SSH_CONFIG" ] || write_ssh_config
  # The keys and secrets are generated ON the box (make local-up) and must never be overwritten or deleted by a
  # sync from the laptop, so they are excluded: rsync then neither copies nor deletes them.
  rsync -az --delete \
    --exclude .git/ --exclude .venv/ --exclude __pycache__/ --exclude .pytest_cache/ --exclude .ruff_cache/ \
    --exclude /.secrets/ --exclude /deploy/xc/generated/ --exclude /deploy/local/.run/ \
    --exclude 'deploy/devbox/.terraform/' --exclude 'deploy/devbox/*.tfstate*' \
    --exclude 'deploy/devbox/.ssh_config' --exclude 'deploy/devbox/.known_hosts' \
    -e "ssh -F $SSH_CONFIG" "$ROOT/" "copperline-devbox:$REMOTE_DIR/"
}

cmd_run() {
  cmd_sync
  ssh_box "cd $REMOTE_DIR && export PATH=\$HOME/.local/bin:\$PATH && $*"
}

cmd_tunnel() {
  [ -f "$SSH_CONFIG" ] || write_ssh_config
  echo "cockpit http://localhost:8095   shop :8001   ops :8002   partner :8003   (Ctrl-C to stop)"
  ssh -F "$SSH_CONFIG" -N \
    -L 8095:127.0.0.1:8095 -L 8001:127.0.0.1:8001 -L 8002:127.0.0.1:8002 -L 8003:127.0.0.1:8003 \
    copperline-devbox
}

cmd_destroy() {
  terraform -chdir="$HERE" destroy -input=false -auto-approve \
    -var "owner=x" -var "expires=x" -var "ssh_public_key=x"
  rm -f "$SSH_CONFIG" "$HERE/.known_hosts"
}

case "${1:-}" in
  up) cmd_up ;;
  sync) cmd_sync ;;
  run) shift; cmd_run "$@" ;;
  ssh) [ -f "$SSH_CONFIG" ] || write_ssh_config; ssh_box ;;
  tunnel) cmd_tunnel ;;
  destroy) cmd_destroy ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
