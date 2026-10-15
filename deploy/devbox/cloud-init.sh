#!/bin/bash
# First-boot setup for the copperline devbox (Ubuntu 24.04). Installs Docker, uv, make and git.
# Progress: /var/log/cloud-init-output.log. Writes /var/lib/copperline-devbox-ready when done.
set -euxo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git make rsync jq unzip

# Docker Engine + compose plugin from Docker's apt repo.
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
usermod -aG docker ubuntu
systemctl enable --now docker

# uv (Python toolchain) for the ubuntu user; it installs Python 3.12 on demand.
sudo -u ubuntu bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'

# AWS CLI v2 (used for Bedrock checks with the instance role).
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp && /tmp/aws/install

# SSH key for the developer (SSH only ever arrives through the SSM tunnel; no port 22 is open).
install -d -m 0700 -o ubuntu -g ubuntu /home/ubuntu/.ssh
echo "${ssh_public_key}" >> /home/ubuntu/.ssh/authorized_keys
chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys && chmod 0600 /home/ubuntu/.ssh/authorized_keys

mkdir -p /home/ubuntu/xc-agentic-demo && chown ubuntu:ubuntu /home/ubuntu/xc-agentic-demo
touch /var/lib/copperline-devbox-ready
