#!/bin/bash
# ensure-opencode — make opencode available in this container.
#
# Run during `jailbee new` (before autostart), as the dev user, with:
#   HOME=/home/<user>
#   JAILBEE_AUTO_UPDATE=true|false
#
# ~/.opencode is a shared bind mount (<shared_dir>/opencode-install), and
# ~/.local/bin/opencode is per container, so every fresh container passes
# through here; `_ensure_one`'s install-vs-update split never sees a populated
# store. This script makes that decision itself, as ensure-claude.sh does.
#
# The vendor installer downloads the ~88MB tarball on every run, whatever is
# installed, so an update first compares versions, reading the same pointer the
# installer reads, and pins the installer to the version it found.
set -euo pipefail

INSTALLER_URL="https://opencode.ai/v2/install"
LATEST_URL="https://opencode.ai/update/api/latest/cli/npm"
STORE_BIN="${HOME}/.opencode/bin/opencode"
BIN="${HOME}/.local/bin/opencode"

mkdir -p "${HOME}/.local/bin" "${HOME}/.opencode"

# Serialize concurrent jailbee-new runs sharing this directory.
exec 9>"${HOME}/.opencode/.update.lock"
flock 9

# `--no-modify-path`: the installer's PATH edit lands at the end of ~/.bashrc,
# after Debian's early return for non-interactive shells — every shell jailbee
# runs an agent under. The link below is what puts opencode on PATH.
run_installer() {
    curl -fsSL "${INSTALLER_URL}" | bash -s -- --no-modify-path "$@"
}

installed_version() {
    local v
    v="$("${STORE_BIN}" --version 2>/dev/null || true)"
    v="${v##* }"
    echo "${v#v}"
}

STATUS=0

if [ ! -x "${STORE_BIN}" ]; then
    echo "==> ensure-opencode: empty store, installing opencode"
    run_installer || STATUS=1
elif [ "${JAILBEE_AUTO_UPDATE:-false}" = "true" ]; then
    LATEST="$(curl -fsSL "${LATEST_URL}" | sed -n 's/.*"version":"\([^"]*\)".*/\1/p' || true)"
    CURRENT="$(installed_version)"
    if [ -z "${LATEST}" ]; then
        echo "==> ensure-opencode: ERROR: could not look up the latest opencode" >&2
        STATUS=1
    elif [ "${LATEST}" != "${CURRENT}" ]; then
        echo "==> ensure-opencode: updating opencode ${CURRENT:-?} -> ${LATEST}"
        if ! run_installer --version "${LATEST}"; then
            echo "==> ensure-opencode: ERROR: installing opencode ${LATEST} failed" >&2
            STATUS=1
        fi
    fi
fi

# The installer hardcodes ~/.opencode/bin, which nothing puts on PATH; without
# this link `command -v opencode` fails and the autostart window dies with
# `opencode: not found`. Re-made on every run: a failed update still leaves
# this container on the binary the store already has. `ln -sfn` happily makes
# a dangling link, so the `-x` test is what catches an install that produced
# nothing.
ln -sfn "${STORE_BIN}" "${BIN}"
if [ ! -x "${BIN}" ]; then
    echo "==> ensure-opencode: ERROR: ${BIN} missing/not executable" >&2
    exit 1
fi
exit "${STATUS}"
