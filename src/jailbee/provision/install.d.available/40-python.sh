#!/bin/bash
# 40-python — install the base image's system Python + venv + pip.
# The container Python version is whatever the base image ships (a function
# of golden.ubuntu_version); it is intentionally NOT configurable — the
# Ubuntu archive provides only one python3.X per release.
# Installs: python3, python3-venv, python3-pip
set -euo pipefail

echo "==> Installing Python (system python3 from the base image)"
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 python3-venv python3-pip

python3 --version
