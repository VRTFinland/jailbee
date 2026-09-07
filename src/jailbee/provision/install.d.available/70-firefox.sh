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
# comparison: Ubuntu's transitional package carries an epoch (e.g.
# `1:1snap1-0ubuntu2`), which on its own would always outrank Mozilla's
# plain `142.0`-style version — and Pin-Priority 1000 is specifically the
# threshold at which apt accepts what is technically a downgrade to get
# Mozilla's build installed instead. Without the pin, `apt-get install
# firefox` silently installs the snap stub instead of a browser.
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

# `test -x /usr/bin/firefox` would NOT catch the failure this guards
# against: Ubuntu's transitional package exists specifically to keep that
# path present (its whole job is to install the snap and get out of the
# way), so the stub is present and executable too. Check provenance
# instead. Mozilla's own build never carries an epoch; the transitional
# package's version always does (see the comment above) — so an epoch in
# the installed version is conclusive evidence the pin did not take.
installed_version="$(dpkg-query -W -f='${Version}' firefox)"
case "${installed_version}" in
    *snap*|[0-9]:*)
        echo "70-firefox: apt installed firefox ${installed_version} — that is" >&2
        echo "70-firefox: Ubuntu's transitional/snap package, not Mozilla's build." >&2
        echo "70-firefox: the packages.mozilla.org pin did not take." >&2
        exit 1
        ;;
esac

# Fail loudly rather than shipping an image whose `firefox` is broken in
# some other way: a browser that exits immediately is far harder to
# diagnose later than a build that stops here.
firefox --version
