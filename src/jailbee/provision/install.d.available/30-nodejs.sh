#!/bin/bash
# 30-nodejs — install Node.js $NODE_MAJOR.x from NodeSource and set up
# a per-user npm global prefix at ${JAILBEE_USER_HOME}/.npm-global.
# Env: NODE_MAJOR, JAILBEE_USER_HOME, CONTAINER_USER
# Installs: nodejs, ${JAILBEE_USER_HOME}/.npmrc, /etc/profile.d/npm-global.sh
set -euo pipefail

echo "==> Installing Node.js ${NODE_MAJOR} (NodeSource)"
curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs
# No pnpm/corepack setup here: package-manager version is a per-project
# decision. The project's bootstrap (e.g. `make install`) is responsible
# for installing whichever pnpm/yarn it needs into the dev user's
# ~/.npm-global, which the npmrc below routes to.

# Per-user writable npm global prefix. Default npm prefix is /usr, which
# only root can write to — so the dev user's `npm install -g <tool>`
# (used by upstream Makefiles that run e.g. `npm install -g pnpm`)
# fails with EACCES on /usr/lib/node_modules.
#
# We write the user's ~/.npmrc directly: npm 10+ ignores /etc/npmrc and
# instead reads $(npm config get prefix)/etc/npmrc (= /usr/etc/npmrc for
# system Node), so a top-level /etc file wouldn't be honored. ~/.npmrc
# is always read first.
echo "==> Configuring per-user npm global prefix for ${CONTAINER_USER}"
cat > "${JAILBEE_USER_HOME}/.npmrc" <<EOF
prefix=${JAILBEE_USER_HOME}/.npm-global
EOF
mkdir -p "${JAILBEE_USER_HOME}/.npm-global"
chown -R "${CONTAINER_USER}:${CONTAINER_USER}" \
    "${JAILBEE_USER_HOME}/.npmrc" "${JAILBEE_USER_HOME}/.npm-global"

cat > /etc/profile.d/npm-global.sh <<'EOF'
export PATH="$HOME/.npm-global/bin:$PATH"
EOF
chmod 0644 /etc/profile.d/npm-global.sh

node --version
