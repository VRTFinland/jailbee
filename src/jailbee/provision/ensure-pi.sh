#!/bin/bash
# ensure-pi — make pi available in this container.
#
# Run during `jailbee new` (before autostart), as the dev user, with:
#   HOME=/home/<user>
#   JAILBEE_AUTO_UPDATE=true|false
#
# ~/.local/share/pi is a shared bind mount (<shared_dir>/pi-install), and
# ~/.local/bin/pi is per container, so every fresh container passes through
# here; `_ensure_one`'s install-vs-update split never sees a populated store.
# This script makes that decision itself, as ensure-claude.sh does.
#
# Each release is its own npm prefix, releases/<version>/, and `current` is
# switched to a new one by rename. npm would rewrite the package in place, and
# pi's bundle imports its chunks lazily, so a pi running in a sibling container
# would lose them mid-session. Node resolves the launcher symlink to the real
# path at startup, so a running pi keeps reading the release it started from —
# until that release is pruned. The two newest are kept.
set -euo pipefail

PKG="@earendil-works/pi-coding-agent"
STORE="${HOME}/.local/share/pi"
RELEASES="${STORE}/releases"
BIN="${HOME}/.local/bin/pi"

if ! command -v node >/dev/null || ! command -v npm >/dev/null; then
    echo "==> ensure-pi: ERROR: pi needs Node.js and none is on PATH: set golden.stacks.node and rebuild the base image" >&2
    exit 1
fi

mkdir -p "${HOME}/.local/bin" "${RELEASES}"

# Serialize concurrent jailbee-new runs sharing this directory. Every writer
# holds this lock, so a leftover temp prefix is from a run that died.
exec 9>"${STORE}/.update.lock"
flock 9
rm -rf "${RELEASES}"/.tmp-*

current_release() {
    if [ -L "${STORE}/current" ]; then
        basename "$(readlink "${STORE}/current")"
    fi
}

install_release() {
    local version="$1" tmp
    if [ ! -d "${RELEASES}/${version}" ]; then
        tmp="$(mktemp -d "${RELEASES}/.tmp-XXXXXX")"
        if ! npm install -g --ignore-scripts --prefix "${tmp}" "${PKG}@${version}"; then
            rm -rf "${tmp}"
            return 1
        fi
        mv -T "${tmp}" "${RELEASES}/${version}"
    fi
    ln -sfn "releases/${version}" "${STORE}/current.new"
    mv -T "${STORE}/current.new" "${STORE}/current"
}

prune_releases() {
    local current v
    current="$(current_release)"
    for v in $(ls -1 "${RELEASES}" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+' | sort -V -r | tail -n +3 || true); do
        [ "${v}" = "${current}" ] || rm -rf "${RELEASES:?}/${v}"
    done
}

# npm only warns (EBADENGINE) when Node is older than the package's `engines`,
# so without this an install on an old image succeeds and pi crashes at first
# use. Checked after linking, against the release in use, so it also catches a
# container whose image is older than the one that filled the store. Only a
# plain `>=X.Y.Z` is understood; anything else is not checked.
require_node() {
    local manifest="${STORE}/current/lib/node_modules/${PKG}/package.json" want have
    want="$(node -p "(require(process.argv[1]).engines || {}).node || ''" "${manifest}" 2>/dev/null || true)"
    want="$(sed -n 's/^>=[[:space:]]*\([0-9][0-9.]*\)$/\1/p' <<<"${want}")"
    [ -n "${want}" ] || return 0
    have="$(node -p process.versions.node)"
    if [ "$(printf '%s\n%s\n' "${want}" "${have}" | sort -V | head -n 1)" != "${want}" ]; then
        echo "==> ensure-pi: ERROR: pi $(current_release) needs Node >= ${want}, this container has ${have}: raise golden.stacks.node and rebuild the base image" >&2
        return 1
    fi
}

CURRENT="$(current_release)"
STATUS=0

if [ -z "${CURRENT}" ] || [ "${JAILBEE_AUTO_UPDATE:-false}" = "true" ]; then
    if LATEST="$(npm view "${PKG}" version)" && [ -n "${LATEST}" ]; then
        if [ "${LATEST}" != "${CURRENT}" ]; then
            echo "==> ensure-pi: installing pi ${LATEST}"
            if install_release "${LATEST}"; then
                prune_releases
            else
                echo "==> ensure-pi: ERROR: npm install of pi ${LATEST} failed" >&2
                STATUS=1
            fi
        fi
    else
        echo "==> ensure-pi: ERROR: could not look up the latest pi on the npm registry" >&2
        STATUS=1
    fi
fi

# Re-made on every run: a failed update still leaves this container on the
# release the store already has.
ln -sfn "${STORE}/current/bin/pi" "${BIN}"
if [ ! -x "${BIN}" ]; then
    echo "==> ensure-pi: ERROR: ${BIN} missing/not executable" >&2
    exit 1
fi
require_node || exit 1
exit "${STATUS}"
