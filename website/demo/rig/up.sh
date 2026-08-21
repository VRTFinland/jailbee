#!/usr/bin/env bash
# Bring up the workflow-video recording rig against this container's nested
# Incus daemon. Idempotent: safe to re-run, and re-running is the intended way
# to recover from a partial failure.
#
# Every step below except `jailbee init` and `jailbee base build` exists
# because a feasibility gate failed without it on 2026-08-21. None of the five
# is documented anywhere else — see the spec's section 13 for the evidence.
#
# Runtime-only steps: the /dev/dri mask and the Chrome chmod do not survive a
# container restart. Re-run this script after one.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SUBSTRATE="${SUBSTRATE:-$REPO_ROOT/.local/video-rig/jailbee-demo}"
BRIDGE_CIDR="${BRIDGE_CIDR:-10.78.0.1/24}"
HOST_UID="$(id -u)"

say() { printf '\n==> %s\n' "$1"; }

[[ -f "$SUBSTRATE/.jailbee/config.yaml" ]] || {
  echo "error: no substrate at $SUBSTRATE (see the plan's Tasks 2-3)" >&2
  exit 1
}

# Bare `jailbee`, and it must be an editable install: a non-editable one bakes
# a stale copy of the provisioning tree into the golden image.
command -v jailbee >/dev/null || {
  echo "error: jailbee is not on PATH. Install it editable:" >&2
  echo "  uv tool install -e $REPO_ROOT" >&2
  exit 1
}

say "Starting the nested Incus daemon"
sudo systemctl start incus
# Poll for the socket instead of sleeping a guessed interval.
for _ in $(seq 1 30); do
  incus info >/dev/null 2>&1 && break
  sleep 1
done
incus info >/dev/null

say "Masking /dev/dri"
# profiles.py adds one unix-char device per host /dev/dri/renderD* WITH a
# `mode` property, and Incus refuses `mode` when the parent is a nested
# container. That fails `jailbee apply` on the base profile and — the part
# that is easy to miss — aborts it before it ever populates <prefix>-binds,
# so the shared ~/.claude mounts never attach and the agent in the container
# reports "Not logged in" for a reason that looks unrelated.
#
# Masking the directory makes _dri_nodes() return nothing, so no dri device is
# generated at all. Unlike `incus profile device unset`, this survives every
# later `apply`. Verified harmless: VHS's headless Chrome renders fine with no
# DRI node.
if [[ -n "$(ls -A /dev/dri 2>/dev/null || true)" ]]; then
  sudo mount -t tmpfs -o mode=0755,size=1M none /dev/dri
fi

say "Disabling the docker registry mirror for this container"
# Defaults to true (global_config.py), and cli.py's init hard-fails with
# "jailbee-registry-mirror container not found" before doing anything else.
# The substrate pulls no Docker images, so the mirror is dead weight here and
# this flag is the intended switch rather than a workaround.
mkdir -p ~/.config/jailbee
cat > ~/.config/jailbee/global.yaml <<'YAML'
# Written by website/demo/rig/up.sh. The workflow-video substrate is
# Python/uv/SQLite and pulls no Docker images.
docker_registry_mirror:
  enabled: false
YAML

say "Allowing the identity uid range for nested containers"
# The base profile asks for `raw.idmap: uid <host uid> <host uid>` so
# host-owned bind mounts stay readable, and newuidmap refuses it unless
# /etc/subuid covers that uid: "uid range [53023-53024) not allowed".
# 75-incus.sh caps the file to root:1000000:65536, which does not.
for f in /etc/subuid /etc/subgid; do
  grep -qx "root:${HOST_UID}:1" "$f" || echo "root:${HOST_UID}:1" | sudo tee -a "$f" >/dev/null
done

say "Making the extracted Chrome traversable"
# Only needed until the golden image is rebuilt with the 65-vhs.sh fix
# (a1b4e47): a 0750 extraction root hides Chrome from the container user, VHS
# falls back to a bundled Chromium, and the render blocks forever with no
# error message at all.
if [[ -d /opt/google-chrome ]] && ! command -v google-chrome >/dev/null 2>&1; then
  sudo chmod 0755 /opt/google-chrome
fi
command -v google-chrome >/dev/null || {
  echo "error: google-chrome is not on PATH; vhs would hang silently" >&2
  exit 1
}

say "Creating incusbr0"
# jailbee hardcodes this bridge name (init_command.py) and never creates it —
# on a real host `incus admin init` already has. The CIDR must be explicit:
# `ipv4.address=auto` hung indefinitely on this nested daemon (killed at
# 5m30s, nothing created), while an explicit subnet returns in under a second.
if ! incus network show incusbr0 >/dev/null 2>&1; then
  incus network create incusbr0 "ipv4.address=${BRIDGE_CIDR}" ipv4.nat=true ipv6.address=none
fi

say "Initialising jailbee for the substrate"
cd "$SUBSTRATE"
if incus profile show jailbee-demo-base >/dev/null 2>&1; then
  jailbee apply
else
  jailbee init
fi

say "Building the golden image"
# ~3m18s and ~871MiB for this config, measured. Skipped when the alias exists.
if incus image alias list --format csv | grep -q '^jailbee-demo-base,'; then
  echo "    already built — skipping"
else
  jailbee base build
fi

say "Rig up. Next: rig/seed-claude.sh, then website/demo/render.sh"
