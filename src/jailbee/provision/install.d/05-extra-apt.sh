#!/bin/bash
# 05-extra-apt — install repo-supplied extra apt packages.
# Env: EXTRA_APT_PACKAGES (whitespace-separated; may be empty)
# Installs: packages listed in $EXTRA_APT_PACKAGES
set -euo pipefail

if [ -n "${EXTRA_APT_PACKAGES:-}" ]; then
    echo "==> Installing extra apt packages: ${EXTRA_APT_PACKAGES}"
    # Intentionally unquoted: shell word-splitting feeds apt-get multiple
    # package args. Names are validated by config.py against
    # [a-z0-9][a-z0-9+\-.]* so this expansion can't smuggle shell syntax.
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive apt-get install -y ${EXTRA_APT_PACKAGES}
fi
