#!/bin/bash
# install.sh — slim LXC/Incus plumbing for the jailbee golden image.
#
# Run inside a fresh Ubuntu container by `jailbee base build`. Almost all
# feature provisioning has moved to /provision/install.d/*.sh — this
# script only owns the plumbing that every container needs regardless of
# stack (user creation, sudoers, SSH_AUTH_SOCK passthrough, etc.).
#
# Environment variables (passed via `incus exec --env`):
#   CONTAINER_UID, CONTAINER_GID — uid/gid for the dev user
#   JAILBEE_USER_HOME                — = /home/dev (constant, defaulted below)
#   JAILBEE_PROVISION_DIR            — = /provision (constant, defaulted below)
# Plus per-snippet env (JAVA_PACKAGE, NODE_MAJOR, PYTHON_VERSION,
# EXTRA_APT_PACKAGES, and any golden.provision_env additions) passed
# straight through to snippets.
#
# The unix username inside the container is hardcoded to "dev".
set -euo pipefail

# Exported so install.d/* snippets (which run as `bash "$f"`, a fresh
# process) inherit it. CONTAINER_USER is intentionally not passed via
# `incus exec --env` — it's an internal detail of the golden image.
export CONTAINER_USER=dev

: "${CONTAINER_UID:?CONTAINER_UID required}"
: "${CONTAINER_GID:?CONTAINER_GID required}"
: "${JAILBEE_USER_HOME:=/home/dev}"
: "${JAILBEE_PROVISION_DIR:=/provision}"

# Ubuntu's unattended-upgrade machinery is a liability in a dev container.
# apt-daily.timer fires within minutes of every boot, so it takes the dpkg
# lock out from under whoever is installing something — including this
# script, moments from now — and an apt run still in flight at shutdown
# blocks systemd, which can burn the whole clean-shutdown budget `jailbee`
# gives a stop and leave the container Running. Nothing in a branch
# container wants surprise background upgrades: the image is rebuilt by
# `jailbee base build` instead.
#
# Masked rather than disabled: `apt-get install` of anything that ships
# these units re-enables a merely-disabled timer, and masking survives that.
# The timers are stopped here; the services deliberately are not, since
# SIGTERM to an apt run mid-dpkg is how an image gets a broken package
# database. If one is running, the apt-get below fails loudly on the lock.
echo "==> Masking Ubuntu's automatic apt machinery"
systemctl stop apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl mask \
    apt-daily.timer apt-daily-upgrade.timer \
    apt-daily.service apt-daily-upgrade.service \
    unattended-upgrades.service 2>/dev/null || true

echo "==> Updating apt cache"
apt-get update -y

echo "==> Installing LXC plumbing apt packages"
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    apt-transport-https ca-certificates curl gnupg lsb-release wget \
    sudo locales tzdata \
    git make tmux \
    build-essential libssl-dev pkg-config \
    ripgrep fd-find jq htop \
    openssh-server

# tzdata installs /etc/localtime as a symlink (-> /usr/share/zoneinfo/Etc/UTC).
# The <repo>-binds profile bind-mounts the host's /etc/localtime onto this
# path, but LXC refuses to mount over a symlink destination and
# the container fails to start with "Too many levels of symbolic links".
# Drop the symlink; the bind-mount supplies the contents at runtime.
echo "==> Replacing /etc/localtime symlink with empty regular file"
rm -f /etc/localtime
touch /etc/localtime

# Ubuntu cloud images ship with a default `ubuntu` user at UID 1000 / GID 1000.
# When CONTAINER_UID/GID == 1000 (the common case — host user UID), useradd/
# groupadd below collide and silently fail (the `|| true` mask was hiding this,
# leaving subsequent `chown dev:dev` calls to fail with "invalid user").
# Remove the default user/group first so the dev user can claim the UID.
if getent passwd ubuntu >/dev/null; then
    echo "==> Removing default 'ubuntu' user (conflicts with CONTAINER_UID)"
    userdel -r ubuntu 2>/dev/null || true
fi
getent group ubuntu >/dev/null && groupdel ubuntu 2>/dev/null || true

echo "==> Creating user ${CONTAINER_USER} (UID=${CONTAINER_UID}, GID=${CONTAINER_GID})"
groupadd -g "${CONTAINER_GID}" "${CONTAINER_USER}"
useradd -m -u "${CONTAINER_UID}" -g "${CONTAINER_GID}" \
        -s /bin/bash "${CONTAINER_USER}"

# ~/.local/bin first on PATH for any tool that follows XDG conventions
# (pipx, cargo install --root ~/.local, the ensure-claude.sh step that
# installs claude into ~/.local/bin at jailbee-new time).
cat > /etc/profile.d/local-bin.sh <<'EOF'
export PATH="$HOME/.local/bin:$PATH"
EOF
chmod 0644 /etc/profile.d/local-bin.sh

# Heal a dangling ~/.local/bin/claude at login.
#
# The two halves of the Claude install disagree about lifetime:
# ~/.local/share/claude/versions is a bind mount SHARED by every container of
# a repo (agent_presets.claude_preset's claude-install cache), while
# ~/.local/bin/claude is a per-container symlink pinned to one exact version
# by ensure-claude.sh — which runs at `jailbee new` and never again. Claude's
# own updater prunes old releases from the shared store, so a `claude update`
# in ANY container of the repo can delete the version THIS container points
# at, and the launcher stays dangling for the rest of the container's life
# (`-bash: /home/dev/.local/bin/claude: No such file or directory`).
#
# Repointing it at login covers every path that runs `claude` in a container:
# each goes through a `bash -lc` login shell (jailbee shell, tmux windows,
# autostart steps, `jailbee pr`'s claude invocation, the agent install check).
#
# Two properties this snippet must keep:
#   - Only acts when the launcher is missing or dangling. A healthy pin is
#     left alone, so `claude.auto_update: false` keeps its chosen version.
#   - Prints nothing, ever. pr_ai.ask_claude_for_pr_text parses the stdout of
#     a `bash -lc` login shell as JSON; a chatty snippet would corrupt it.
# The reverse-sorted loop skips a newest-named entry that isn't a usable
# binary (an interrupted download) instead of linking the stub.
cat > /etc/profile.d/jailbee-claude.sh <<'EOF'
if [ ! -x "$HOME/.local/bin/claude" ]; then
    _jb_claude_store="$HOME/.local/share/claude/versions"
    for _jb_claude_v in $(ls -1 "$_jb_claude_store" 2>/dev/null | sort -V -r); do
        if [ -x "$_jb_claude_store/$_jb_claude_v" ]; then
            mkdir -p "$HOME/.local/bin"
            ln -sfn "$_jb_claude_store/$_jb_claude_v" "$HOME/.local/bin/claude"
            break
        fi
    done
    unset _jb_claude_store _jb_claude_v
fi
EOF
chmod 0644 /etc/profile.d/jailbee-claude.sh

# Passwordless sudo for the dev user.
echo "${CONTAINER_USER} ALL=(ALL) NOPASSWD:ALL" \
    > "/etc/sudoers.d/90-${CONTAINER_USER}"
chmod 0440 "/etc/sudoers.d/90-${CONTAINER_USER}"

# Make ssh-agent → gpg-agent forwarding survive login shells that lose
# the environment (a plain `sudo -i`, an SSH login into the container).
# The base Incus profile sets SSH_AUTH_SOCK at exec level, but env_reset
# clears it — so ssh-add reports "no identities" even though the
# gpg-agent socket is mounted in. The profile.d snippet re-derives it
# from XDG_RUNTIME_DIR, which PAM keeps set.
#
# Two guards, because one golden image serves every config:
#   - Only when SSH_AUTH_SOCK is still unset. jailbee's own `jailbee
#     shell` / `jailbee exec` reach the shell via `incus exec --user` +
#     setpriv, which preserves the profile value, and `container.env` is
#     documented to be able to point SSH_AUTH_SOCK at a different agent —
#     neither may be overwritten here.
#   - Only when the socket actually exists. With `gpg.enabled: false`
#     jailbee attaches no gpg-socket device and the host may run no
#     gpg-agent at all; exporting a path to a missing socket breaks
#     ssh-add and shadows any agent started inside the container.
#
# The sudoers drop-in additionally preserves SSH_AUTH_SOCK plus the GUI
# socket env for callers that already have it set (e.g. `jailbee ide`).
echo "==> Configuring SSH_AUTH_SOCK passthrough for login shells / sudo"
cat > /etc/profile.d/jailbee-env.sh <<'EOF'
if [ -z "${SSH_AUTH_SOCK:-}" ]; then
    _jailbee_gpg_sock="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/gnupg/S.gpg-agent.ssh"
    if [ -S "$_jailbee_gpg_sock" ]; then
        export SSH_AUTH_SOCK="$_jailbee_gpg_sock"
    fi
    unset _jailbee_gpg_sock
fi
EOF
chmod 0644 /etc/profile.d/jailbee-env.sh

cat > /etc/sudoers.d/90-jailbee-env <<'EOF'
Defaults env_keep += "SSH_AUTH_SOCK WAYLAND_DISPLAY DISPLAY"
EOF
chmod 0440 /etc/sudoers.d/90-jailbee-env

# Pre-create dev-owned parent dirs for bind-mount targets. Without these,
# Incus auto-creates parents as root:root at container start, which blocks
# the dev user from later writing siblings into them — e.g. `docker buildx`
# into /home/dev/.docker/buildx, or `uv` into /home/dev/.config/uv when
# /home/dev/.config/JetBrains is bind-mounted in.
#
# Run `mkdir -p` as ${CONTAINER_USER} so EVERY intermediate (e.g. `.local`,
# `.local/share`, `.java`) is dev-owned. The earlier "mkdir as root + chown
# only the leaf" approach left intermediates root:root, which broke tools
# writing siblings of the precreated leaves — e.g. the jailbee-new-time
# ensure-claude.sh step's `mkdir /home/dev/.local/share/claude` (EACCES).
#
# `.cache` is here because a repo may bind-mount a shared cache under it
# (e.g. `~/.cache/uv`), which makes Incus auto-create `.cache` as root:root.
# The Claude Code native installer then fails `mkdir ~/.cache/claude`
# (EACCES), leaving an empty version store and no `claude` binary.
echo "==> Pre-creating bind-mount parent dirs owned by ${CONTAINER_USER}"
for d in \
    .cache \
    .docker \
    .config \
    .java/.userPrefs \
    .local/share/pnpm \
; do
    runuser -u "${CONTAINER_USER}" -- mkdir -p "${JAILBEE_USER_HOME}/${d}"
done

# Enable systemd-logind "linger" for the dev user. Without this, the
# per-user runtime dir (/run/user/<UID>) is only created when a real
# PAM login happens — and `incus exec` bypasses PAM. With linger on,
# logind creates /run/user/<UID> with the right owner+mode at container
# boot, *before* Incus mounts disk devices like the Wayland socket.
# This avoids the race where Incus would otherwise auto-create the
# parent as root:root, mode 700, leaving the bind-mounted sockets
# inaccessible to the dev user.
echo "==> Enabling systemd-logind linger for ${CONTAINER_USER}"
mkdir -p /var/lib/systemd/linger
touch "/var/lib/systemd/linger/${CONTAINER_USER}"

# Run user/repo install.d snippets (and after this task, the bundled
# feature snippets too). Empty files are skipped — that's the same-name
# shadow disable mechanism.
if [ -d /provision/install.d ]; then
    for f in /provision/install.d/*.sh; do
        [ -e "$f" ] || continue
        [ -s "$f" ] || continue
        echo "==> Running install.d snippet: $(basename "$f")"
        bash "$f"
    done
fi

echo "==> Running plumbing smoke checks"
git --version
tmux -V

echo "==> Cleaning up apt caches"
apt-get clean
rm -rf /var/lib/apt/lists/*

echo "==> Disabling SSH server (enable manually if needed)"
systemctl disable ssh || true

echo "==> Provisioning complete"
