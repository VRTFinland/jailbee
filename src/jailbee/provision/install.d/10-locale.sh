#!/bin/bash
# 10-locale — generate the en_US.UTF-8 locale.
# Env: (none)
# Installs: locale-gen entries; updates /etc/default/locale
set -euo pipefail

echo "==> Configuring locale"
locale-gen en_US.UTF-8
update-locale LANG=en_US.UTF-8

locale -a | grep -q en_US.utf8
