#!/bin/bash
# First boot of the training machine. Hard stop: the machine powers off (and so terminates,
# launch template) ${max_runtime_minutes} minutes after boot, whatever is running.
set -eu
shutdown -h +${max_runtime_minutes}
apt-get update -q
apt-get install -y -q python3-venv git unzip curl
curl -sSfL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscli.zip
unzip -q /tmp/awscli.zip -d /tmp && /tmp/aws/install
curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh
install -d -o ubuntu -g ubuntu /work
echo "bucket=${bucket}" > /work/README
