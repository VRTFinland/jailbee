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

# Google ships no linux/arm64 .deb for Chrome — the `arch=amd64` below is
# not a placeholder, it's the only value Google ever publishes. On an
# arm64 build host, apt-get install below would otherwise die minutes in
# with a generic "no installation candidate" that names neither the cause
# nor the way out. Fail immediately and name both.
build_arch="$(dpkg --print-architecture)"
if [ "${build_arch}" != "amd64" ]; then
    echo "70-chrome: Google Chrome has no linux/${build_arch} apt package." >&2
    echo "70-chrome: set browsers.chrome.source: host and RO-mount a host install instead," >&2
    echo "70-chrome: or use Firefox (browsers.firefox.source: image) — it ships arm64." >&2
    exit 1
fi

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

google-chrome-stable --version
