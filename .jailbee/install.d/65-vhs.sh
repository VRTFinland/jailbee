#!/bin/bash
# 65-vhs — terminal-recording toolchain for the website's demo clips.
# Env: CONTAINER_USER, JAILBEE_USER_HOME
# Installs: vhs (Charm), Google Chrome, and the VHS_NO_SANDBOX default.
#
# `ttyd` and `ffmpeg`, VHS's other two dependencies, come from
# golden.extra_apt_packages in .jailbee/config.yaml — plain apt handles those.
# The two below cannot: see the notes on each.
#
# Only needed to re-render website/assets/media/*. Costs ~150 MB in the golden
# image; drop this snippet and the two apt packages if the website is ever
# split out of this repo.
set -euo pipefail

VHS_VERSION="0.11.0"

echo "==> Installing vhs ${VHS_VERSION}"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
curl -sSLo "${tmp}/vhs.deb" \
    "https://github.com/charmbracelet/vhs/releases/download/v${VHS_VERSION}/vhs_${VHS_VERSION}_amd64.deb"
apt-get install -y --no-install-recommends "${tmp}/vhs.deb"

# Chrome is EXTRACTED, not apt-installed, for two reasons found the hard way:
#
# 1. `apt-get install google-chrome-stable` pulls fonts-liberation-sans-narrow,
#    which writes into /usr/share/fonts/truetype — a READ-ONLY bind mount of the
#    host's fonts in every jailbee container. dpkg fails there and takes the
#    whole transaction down with it.
# 2. VHS drives a browser through go-rod, which downloads its own Chromium if it
#    cannot find a system one. That bundled build is old enough that its xterm
#    canvas layers never appear, and vhs then blocks forever on an element that
#    will not arrive — a hang with no error message. A current system Chrome on
#    PATH avoids the download entirely.
echo "==> Installing Google Chrome (extracted, not apt — see comment)"
curl -sSLo "${tmp}/chrome.deb" \
    "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
dpkg-deb -x "${tmp}/chrome.deb" /opt/google-chrome
# `dpkg-deb -x` creates the extraction root with the caller's umask, and this
# script runs as root with umask 027 — so /opt/google-chrome lands 0750 and the
# container user cannot traverse it. Everything *below* it is 0755, so the
# symlink looks fine and `google-chrome --version` succeeds for root while
# `command -v google-chrome` finds nothing for the dev user. VHS then falls
# back to go-rod's bundled Chromium and blocks forever with no error — the
# exact failure the comment above exists to prevent, reached by a different
# road. Found by a render that hung for 5 minutes on 2026-08-21.
chmod 0755 /opt/google-chrome
ln -sf /opt/google-chrome/opt/google/chrome/google-chrome /usr/local/bin/google-chrome

# Chromium's sandbox needs unprivileged user namespaces, which an unprivileged
# Incus container does not get. Without this, every render dies in
# sandbox::Credentials::CanCreateProcessInNewUserNS().
cat > /etc/profile.d/jailbee-vhs.sh <<'EOF'
export VHS_NO_SANDBOX=1
EOF
chmod 0644 /etc/profile.d/jailbee-vhs.sh

vhs --version
# As ${CONTAINER_USER}, not as root: root can reach Chrome through a 0750
# parent directory and the dev user cannot, so a root-only check passes on
# precisely the image that will hang at render time. This is the one assertion
# here that would have failed on the broken image.
runuser -l "${CONTAINER_USER}" -c 'google-chrome --version'
