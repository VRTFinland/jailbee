#!/bin/bash
# ensure-claude — make the latest Claude Code available in this container.
#
# Run during `jailbee new` (before autostart), as the dev user, with:
#   HOME=/home/<user>
#   JAILBEE_CLAUDE_AUTO_UPDATE=true|false
#
# The Claude install dir ~/.local/share/claude is a shared bind mount
# (<shared_dir>/claude-install) common to every container, so:
#   - the version store is empty only until the FIRST container seeds it;
#   - the per-container ~/.local/bin/claude symlink lives in $HOME and is
#     therefore absent from every fresh container even when the store is
#     full — we (re)create it here;
#   - `claude update` advances the shared store and is gated on the flag.
#
# A flock on a lock file inside the shared dir serializes parallel
# `jailbee new` invocations (full install / update both write the store).
set -euo pipefail

SHARE_DIR="${HOME}/.local/share/claude"
VERSIONS_DIR="${SHARE_DIR}/versions"
BIN="${HOME}/.local/bin/claude"

mkdir -p "${HOME}/.local/bin" "${VERSIONS_DIR}"

# Serialize concurrent jailbee-new runs sharing this directory.
exec 9>"${SHARE_DIR}/.update.lock"
flock 9

# versions/ holds one executable file per release, named by semver (e.g. 2.1.160).
# The newest is the target of ~/.local/bin/claude; claude is invoked via that symlink.
LATEST="$(ls -1 "${VERSIONS_DIR}" 2>/dev/null | sort -V | tail -1 || true)"

if [ -z "${LATEST}" ]; then
    # Nothing in the shared store yet → full install (always, even when
    # auto_update is off). The native installer populates versions/ and
    # creates the ~/.local/bin/claude symlink.
    echo "==> ensure-claude: empty store, installing Claude Code"
    # The installer runs `claude` as a final smoke-check; that invocation can
    # exit non-zero for reasons unrelated to the binary install (historically a
    # not-yet-valid ~/.claude.json — see _ensure_claude_json_exists). Under
    # `pipefail` such a failure would abort before we (re)create the symlink,
    # leaving an empty store that later surfaces as an opaque exit-127 in the
    # autostart `claude` step. Run the installer tolerantly, then verify the
    # store ourselves and relink from whatever it populated.
    curl -fsSL https://claude.ai/install.sh | bash \
        || echo "==> ensure-claude: installer exited non-zero; verifying store"
    LATEST="$(ls -1 "${VERSIONS_DIR}" 2>/dev/null | sort -V | tail -1 || true)"
    if [ -n "${LATEST}" ]; then
        ln -sfn "${VERSIONS_DIR}/${LATEST}" "${BIN}"
    fi
    if [ ! -x "${BIN}" ]; then
        echo "==> ensure-claude: ERROR: install failed, ${BIN} missing/not executable" >&2
        exit 1
    fi
else
    # Store already populated by another container; this container's
    # bin symlink is missing/stale → point it at the newest version.
    echo "==> ensure-claude: linking ${BIN} -> versions/${LATEST}"
    ln -sfn "${VERSIONS_DIR}/${LATEST}" "${BIN}"
    if [ "${JAILBEE_CLAUDE_AUTO_UPDATE:-false}" = "true" ]; then
        echo "==> ensure-claude: auto_update on, running 'claude update'"
        "${BIN}" update
    fi
fi
