#!/usr/bin/env bash
# Seed Claude credentials into the substrate's shared dir.
#
# jailbee gives every repo its own <shared>/<prefix>/claude mount, so the
# substrate's starts empty and Claude Code would open its onboarding screen on
# camera. Seeding needs two files, and only two: the OAuth tokens, and a
# MINIMAL account record.
#
# Why minimal matters: the maintainer's own ~/.claude.json is ~166 KB and holds
# 47 project entries with local paths and session history. None of that belongs
# in a container about to be recorded, so this copies five keys and nothing
# else. Copying the whole file would work and would be a mistake.
set -euo pipefail

PREFIX="${PREFIX:-jailbee-demo}"
SHARED="${SHARED:-$HOME/.local/share/jailbee/shared/$PREFIX}"

# Where this container's own Claude Code keeps its two files is not fixed, and
# assuming it cost a session: the account record follows CLAUDE_CONFIG_DIR
# (jailbee's golden image exports it as ~/.claude, so the file is
# ~/.claude/.claude.json, not ~/.claude.json), and the credentials may sit in a
# separate directory when the container was handed an account rather than
# logging in itself. Both are overridable; the defaults below are searched in
# order and the first readable hit wins.
first_readable() {
    local f
    for f in "$@"; do
        [[ -r $f ]] && {
            printf '%s\n' "$f"
            return 0
        }
    done
    return 1
}

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SRC_CREDS="${SRC_CREDS:-$(first_readable \
    "$CLAUDE_DIR/.credentials.json" \
    "$HOME/.claude-creds/.credentials.json" \
    "$HOME/.claude/.credentials.json" || true)}"
SRC_JSON="${SRC_JSON:-$(first_readable \
    "$CLAUDE_DIR/.claude.json" \
    "$HOME/.claude.json" || true)}"
# The repo path *inside* the container: jailbee clones into ~/<prefix> and the
# unix user is always `dev`. Claude Code records per-project state under this
# key, and without it the agent opens a trust dialog instead of working.
CONTAINER_REPO="${CONTAINER_REPO:-/home/dev/$PREFIX}"

# Not as root. `install` recreates the destination with the *caller's*
# ownership, so a sudo run leaves a root-owned 0600 .credentials.json inside a
# directory bind-mounted into the container. The container's unprivileged user
# then cannot read it, Claude Code opens its onboarding screen on camera —
# exactly the failure this script exists to prevent — and nothing else would
# catch it.
[[ "$(id -u)" -ne 0 ]] || {
  echo "error: do not run this as root — the container user could not read the result" >&2
  exit 1
}

[[ -d "$SHARED/claude" ]] || {
  echo "error: $SHARED/claude does not exist — run rig/up.sh first" >&2
  exit 1
}
[[ -n "$SRC_CREDS" && -r "$SRC_CREDS" ]] || {
  echo "error: found no readable .credentials.json to seed from." >&2
  echo "       Looked in \$CLAUDE_CONFIG_DIR (${CLAUDE_DIR}), ~/.claude-creds and ~/.claude." >&2
  echo "       Point SRC_CREDS at it if it lives somewhere else." >&2
  exit 1
}
[[ -n "$SRC_JSON" && -r "$SRC_JSON" ]] || {
  echo "error: found no readable .claude.json to take the account record from." >&2
  echo "       Looked in \$CLAUDE_CONFIG_DIR (${CLAUDE_DIR}) and \$HOME." >&2
  echo "       Point SRC_JSON at it if it lives somewhere else." >&2
  exit 1
}
echo "Seeding from $SRC_CREDS and $SRC_JSON"

install -m 0600 "$SRC_CREDS" "$SHARED/claude/.credentials.json"

# THE ACCOUNT RECORD GOES INSIDE THE DIRECTORY MOUNT, not beside it. The
# golden image exports CLAUDE_CONFIG_DIR=$HOME/.claude, so the file Claude Code
# actually reads is <shared>/claude/.claude.json — the one reachable through
# the `shared-claude` directory mount. jailbee also creates <shared>/claude.json
# for the older `~/.claude.json` layout, and seeding only that one is why a
# container came up on the "select login method" screen with valid credentials
# sitting right next to it: `claude -p` answered fine, the TUI asked to log in,
# and the two looked unrelated.
python3 - "$SRC_JSON" "$SHARED/claude/.claude.json" "$CONTAINER_REPO" <<'PY'
import json
import os
import sys

src, dst, container_repo = sys.argv[1], sys.argv[2], sys.argv[3]
KEYS = [
    "oauthAccount",
    "userID",
    "hasCompletedOnboarding",
    "lastOnboardingVersion",
    "hasAvailableSubscription",
]
with open(src) as fh:
    data = json.load(fh)
out = {k: data[k] for k in KEYS if k in data}
missing = [k for k in KEYS if k not in data]
if "oauthAccount" in missing:
    sys.exit("error: source has no oauthAccount — is the source logged in?")

# One project entry, synthesised rather than copied.
#
# Without it Claude Code opens its per-project trust dialog ("Is this a project
# you created or one you trust?") the first time it runs in the container's
# repo. On camera that dialog swallows the prompt the tape types and the tmux
# pane dies — found six minutes into a render, and invisible until someone
# watched the frames.
#
# The account keys above come from the maintainer's file; this does not. It is
# built from the container path alone, so seeding trust for the demo repo can
# never carry any other project's path into the container.
out["projects"] = {
    container_repo: {
        "hasTrustDialogAccepted": True,
        "hasCompletedProjectOnboarding": True,
        "projectOnboardingSeenCount": 1,
    }
}

# Forced rather than copied. A recording container that was handed an account
# by jailbee's claude pool reports `hasAvailableSubscription: false` in its own
# file, and seeding that value verbatim puts the demo container on the
# "Claude account with subscription / Console account / 3rd-party platform"
# chooser instead of a working agent.
out["hasAvailableSubscription"] = True

# Merge rather than replace: a container that has already run writes its own
# caches here (numStartups, feature flags, model caches), and throwing those
# away on every re-seed would make each take the container's first run.
if os.path.exists(dst) and os.path.getsize(dst) > 2:
    with open(dst) as fh:
        try:
            existing = json.load(fh)
        except ValueError:
            existing = {}
    existing.update(out)
    out = existing

# Serialise first, then write once. `open(dst, "w")` truncates before json.dump
# writes, so a failure mid-serialisation would leave a zero-byte claude.json —
# which is worse than never having run, since that is the state Claude Code
# cannot parse.
#
# And NOT a temp file plus os.replace, tempting as that is: claude.json is a
# *file*-level bind-mount source, so swapping the inode would leave a running
# container pinned to the old one. Truncate in place is the right call here.
payload = json.dumps(out, indent=2) + "\n"
with open(dst, "w") as fh:
    fh.write(payload)

# 0600 to match .credentials.json. This file carries the account email and the
# account/organisation UUIDs, and the ambient umask would leave it 0644.
os.chmod(dst, 0o600)

note = f" (absent: {missing})" if missing else ""
print(f"wrote {len(out)} keys, claude.json is now {len(payload)} bytes{note}")
PY

# The legacy path has to go, or jailbee says so on camera. `jailbee new`
# migrates <shared>/claude.json into <shared>/claude/.claude.json but refuses
# to overwrite an existing destination, and prints a four-line warning saying
# to merge it by hand — which landed in the middle of a take, right where the
# video creates a container. The destination above is the authority here, so
# the stale file is simply removed.
rm -f "$SHARED/claude.json"

# The agent has to work while nobody is watching it, and the container is the
# isolation boundary that makes that safe — which is the claim these videos
# exist to show. Without this the agent stops on a permission prompt the moment
# it wants to run a command, and a take records an agent waiting for a human.
#
# `skipDangerousModePermissionPrompt` is Claude Code's own key, written into
# this same file when someone accepts the "Bypass Permissions mode" dialog by
# hand. Setting it here means the dialog never appears on camera; leaving it
# out costs a take, because the dialog defaults to "No, exit" and a stray Enter
# kills the pane.
cat > "$SHARED/claude/settings.json" <<'JSON'
{
  "permissions": {
    "defaultMode": "bypassPermissions"
  },
  "skipDangerousModePermissionPrompt": true
}
JSON

# Confirm the result is readable by this user. The container maps this uid
# identically (the base profile's raw.idmap), so readable here means readable
# there — and a silent permission problem in these files surfaces as an
# onboarding screen in the middle of a take.
for f in "$SHARED/claude/.credentials.json" "$SHARED/claude/.claude.json" "$SHARED/claude/settings.json"; do
  [[ -r "$f" ]] || {
    echo "error: $f is not readable by $(id -un) — the container cannot read it either" >&2
    exit 1
  }
done

echo "Verify with: jailbee exec <container> -- bash -lc 'claude -p \"reply with exactly: ok\"'"
