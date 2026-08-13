#!/bin/bash
# 75-github — install GitHub CLI (gh) from cli.github.com.
# Env: (none required)
# Installs: /usr/bin/gh
#
# We pull from GitHub's own apt repo instead of Ubuntu's universe so we
# always get the latest stable gh, not whatever the distro froze. Same
# pattern as 50-docker.sh (docker.com keyring) — keyring under
# /etc/apt/keyrings, sources.list.d snippet, then apt install.
set -euo pipefail

echo "==> Installing GitHub CLI (gh) from cli.github.com"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
chmod a+r /etc/apt/keyrings/githubcli-archive-keyring.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list

apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y gh
gh --version
