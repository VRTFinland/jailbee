#!/bin/bash
# 15-prompt — surface the container's branch in the bash prompt.
# Env: (uses CONTAINER_USER from install.sh)
# Modifies: /home/${CONTAINER_USER}/.bashrc and /root/.bashrc — appends
# a PS1 override that inserts a yellow "(<branch>)" segment when
# $JAILBEE_BRANCH is set. The variable is delivered to the container via
# `environment.JAILBEE_BRANCH`, set by lifecycle.new_container at create
# time from the user-supplied branch (same source as user.jailbee.branch).
# Mount-mode containers don't set the label, so JAILBEE_BRANCH is unset
# and the prompt falls back to the default Ubuntu form.
set -euo pipefail

: "${CONTAINER_USER:?CONTAINER_USER required}"

write_snippet() {
    cat <<'BASHRC_SNIPPET'

# === jailbee: show $JAILBEE_BRANCH in PS1 (managed by 15-prompt.sh) ===
if [ -n "${PS1-}" ]; then
    PS1='${debian_chroot:+($debian_chroot)}\[\033[01;32m\]\u@\h\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]${JAILBEE_BRANCH:+ \[\033[01;33m\](${JAILBEE_BRANCH})\[\033[00m\]}\$ '
    case "$TERM" in
        xterm*|rxvt*)
            PS1='\[\e]0;${debian_chroot:+($debian_chroot)}\u@\h: \w\a\]'"$PS1"
            ;;
    esac
fi
# === end jailbee ===
BASHRC_SNIPPET
}

for rc in "/home/${CONTAINER_USER}/.bashrc" /root/.bashrc; do
    [ -f "$rc" ] || continue
    if grep -q "managed by 15-prompt.sh" "$rc"; then
        echo "==> Prompt snippet already present in $rc — skipping"
        continue
    fi
    echo "==> Adding JAILBEE_BRANCH prompt snippet to $rc"
    write_snippet >> "$rc"
done
