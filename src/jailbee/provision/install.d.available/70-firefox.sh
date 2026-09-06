#!/bin/bash
# 70-firefox — install Mozilla Firefox into the golden image.
# Env: (none)
# Installs: firefox from Mozilla's own apt repository.
#
# Staged when `browsers.firefox.source: image`, which is the default for
# Firefox and the only source that works out of the box: Ubuntu's own
# `firefox` deb is a transitional package whose entire job is to install
# the snap, and a snap does not run in this container. The APT pin below
# is what stops that transitional package from winning the version
# comparison — without it `apt-get install firefox` silently installs the
# snap stub instead of a browser.
#
# Runs after 60-gui-libs, which installs the shared X11/Wayland/EGL
# libraries and fonts Firefox needs.
set -euo pipefail

echo "==> Installing Mozilla Firefox"
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://packages.mozilla.org/apt/repo-signing-key.gpg \
    -o /etc/apt/keyrings/packages.mozilla.org.asc
chmod 0644 /etc/apt/keyrings/packages.mozilla.org.asc
cat >/etc/apt/sources.list.d/mozilla.list <<'EOF'
deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main
EOF
cat >/etc/apt/preferences.d/mozilla <<'EOF'
Package: *
Pin: origin packages.mozilla.org
Pin-Priority: 1000
EOF
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y firefox

# Fail loudly rather than shipping an image whose `firefox` is the snap
# stub: a browser that exits immediately is far harder to diagnose later
# than a build that stops here.
test -x /usr/bin/firefox || {
    echo "70-firefox: /usr/bin/firefox missing after install — the Mozilla pin did not take" >&2
    exit 1
}
