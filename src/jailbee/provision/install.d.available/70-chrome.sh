#!/bin/bash
# 70-chrome — install Google Chrome into the golden image.
# Env: (none)
# Installs: google-chrome-stable from Google's own apt repository.
#
# Staged when `browsers.chrome.source: image`. The alternative — and the
# default — is `source: host`, which RO-mounts the host's install instead
# and keeps the image small. This snippet exists for hosts that have no
# Chrome to mount.
#
# Runs after 60-gui-libs, which installs the shared X11/Wayland/EGL
# libraries and fonts Chrome needs; without those it starts and renders
# nothing.
set -euo pipefail

echo "==> Installing Google Chrome"
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
    | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg
chmod 0644 /etc/apt/keyrings/google-chrome.gpg
cat >/etc/apt/sources.list.d/google-chrome.list <<'EOF'
deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main
EOF
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y google-chrome-stable
