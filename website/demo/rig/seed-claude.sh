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

[[ -d "$SHARED/claude" ]] || {
  echo "error: $SHARED/claude does not exist — run rig/up.sh first" >&2
  exit 1
}
[[ -r "$SRC_DIR/.credentials.json" ]] || {
  echo "error: no readable $SRC_DIR/.credentials.json to seed from" >&2
  exit 1
}

install -m 0600 "$SRC_DIR/.credentials.json" "$SHARED/claude/.credentials.json"

python3 - "$SRC_JSON" "$SHARED/claude.json" <<'PY'
import json
import sys

src, dst = sys.argv[1], sys.argv[2]
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
with open(dst, "w") as fh:
    json.dump(out, fh, indent=2)
    fh.write("\n")
note = f" (absent: {missing})" if missing else ""
print(f"wrote {len(out)} keys, {dst.rsplit('/', 1)[-1]} is now "
      f"{len(json.dumps(out))} bytes{note}")
PY

echo "Verify with: jailbee exec <container> -- bash -lc 'claude -p \"reply with exactly: ok\"'"
