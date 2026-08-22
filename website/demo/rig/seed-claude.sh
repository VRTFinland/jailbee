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
SRC_DIR="${SRC_DIR:-$HOME/.claude}"
SRC_JSON="${SRC_JSON:-$HOME/.claude.json}"
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
[[ -r "$SRC_DIR/.credentials.json" ]] || {
  echo "error: no readable $SRC_DIR/.credentials.json to seed from" >&2
  exit 1
}
[[ -r "$SRC_JSON" ]] || {
  echo "error: no readable $SRC_JSON to take the account record from" >&2
  exit 1
}

install -m 0600 "$SRC_DIR/.credentials.json" "$SHARED/claude/.credentials.json"

python3 - "$SRC_JSON" "$SHARED/claude.json" "$CONTAINER_REPO" <<'PY'
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

# Confirm the result is readable by this user. The container maps this uid
# identically (the base profile's raw.idmap), so readable here means readable
# there — and a silent permission problem in these two files surfaces as an
# onboarding screen in the middle of a take.
for f in "$SHARED/claude/.credentials.json" "$SHARED/claude.json"; do
  [[ -r "$f" ]] || {
    echo "error: $f is not readable by $(id -un) — the container cannot read it either" >&2
    exit 1
  }
done

echo "Verify with: jailbee exec <container> -- bash -lc 'claude -p \"reply with exactly: ok\"'"
