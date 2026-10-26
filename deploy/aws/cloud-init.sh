#!/bin/bash
# First-boot setup for EC2 copperline-core (Amazon Linux 2023): Docker and the compose plugin, and /opt/copperline
# for the files deploy/aws/core/docker-compose.yaml mounts (.env, jwks.json, kubeconfig; none of them in git).
# Progress: /var/log/cloud-init-output.log. Writes /var/lib/copperline-core-ready when done.
set -euxo pipefail

dnf install -y docker
systemctl enable --now docker

# The compose v2 plugin is not packaged for AL2023: install the release binary as a CLI plugin.
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod 0755 /usr/local/lib/docker/cli-plugins/docker-compose
docker compose version

install -d -m 0750 /opt/copperline
touch /var/lib/copperline-core-ready
