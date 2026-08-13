#!/bin/bash
# 80-ecr-helper — install amazon-ecr-credential-helper.
# Env: (none)
# Installs: amazon-ecr-credential-helper (docker-credential-ecr-login)
set -euo pipefail

echo "==> Installing amazon-ecr-credential-helper"
DEBIAN_FRONTEND=noninteractive apt-get install -y amazon-ecr-credential-helper

command -v docker-credential-ecr-login
