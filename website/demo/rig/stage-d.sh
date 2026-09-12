#!/usr/bin/env bash
# Build video D's opening state: two containers whose agents were given their
# tasks before the camera rolled — one finished, one still working.
#
# Closed loop, not a timed one. This blocks until feat-count's agent has
# committed, and only then gives feat-crud its (larger) task, so "still
# working" when the tape reaches it does not depend on guessing how long Haiku
# takes. Run it before EVERY take: a take consumes the agents' work and cannot
# reuse it.
#
#   rig/stage-d.sh && ./render.sh --record d
#
# The substrate reset is here too, even though D harvests nothing and cannot
# move the host clone itself: a previous video A take does move it, and
# render.sh then refuses to record at all.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
RIG="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUBSTRATE="${SUBSTRATE:-$REPO_ROOT/.local/video-rig/jailbee-demo}"
# The clone's path inside a container: jailbee clones into ~/<prefix> and the
# container user is always `dev`.
CONTAINER_REPO="${CONTAINER_REPO:-/home/dev/jailbee-demo}"
STATE_DB="${STATE_DB:-${XDG_STATE_HOME:-$HOME/.local/state}/jailbee/state.sqlite}"
COMMIT_TIMEOUT="${COMMIT_TIMEOUT:-420}"

COUNT_TASK='Add a GET /items/count endpoint that returns the number of items as JSON, with a test for it, then commit.'
CRUD_TASK='Add PUT /items/{id} and DELETE /items/{id} with validation and 404 handling, a test for every endpoint in the app, and a README section documenting the API, then commit.'

say() { printf '\n==> %s\n' "$1"; }
die() {
    echo "error: $*" >&2
    exit 1
}

[[ -d $SUBSTRATE/.git ]] || die "no substrate at $SUBSTRATE — run rig/substrate.sh"
command -v jailbee >/dev/null || die "jailbee is not on PATH (uv tool install -e $REPO_ROOT)"

cd "$SUBSTRATE"

say "Re-seeding Claude credentials"
# Every take, without exception. The seeded token is a snapshot of this
# container's own, which its Claude Code keeps rotating underneath, and an
# agent that finds it stale rewrites the SHARED credential as an empty stub —
# which breaks all three of D's containers at once. The seed also writes the
# settings.json that lets an agent work while nobody is attending it.
"$RIG/seed-claude.sh"

say "Clearing the previous take's containers"
# feat-warm included: a fourth row breaks the story, and the shared
# claude-install store it exists to keep warm is a host directory that outlives
# any container.
for c in feat-count feat-crud feat-pagination feat-warm; do
    jailbee destroy "$c" --force 2>/dev/null || true
done

say "Resetting the substrate to origin/main"
git reset --quiet --hard origin/main

say "Resetting the dashboard's stored columns"
# seed_view_state reads the global `dashboard:` block once per front-end and
# then never again, so deleting the row is what makes the next `jb dashboard`
# re-seed from the config rig/up.sh writes. Without it a take inherits whatever
# an earlier session left in the settings overlay. A missing database or table
# is not an error: there is simply nothing to reset.
python3 - "$STATE_DB" <<'PY'
import sqlite3
import sys
from pathlib import Path

db = Path(sys.argv[1])
if not db.exists():
    print(f"    no state database at {db} — nothing to reset")
    raise SystemExit(0)
con = sqlite3.connect(db)
try:
    con.execute("DELETE FROM view_prefs WHERE frontend = 'tui'")
    con.commit()
    print("    view_prefs row for the TUI deleted")
except sqlite3.OperationalError as exc:
    print(f"    nothing to reset ({exc})")
finally:
    con.close()
PY

send_task() {
    local container=$1 task=$2
    # -l sends the text literally, so nothing in a prompt is read as a key
    # name. Enter goes in a second call after a beat: typed as one burst, the
    # newline arrives while Claude Code is still laying out the text and is
    # swallowed.
    jailbee exec "$container" -- tmux send-keys -t autostart:claude -l "$task"
    sleep 2
    jailbee exec "$container" -- tmux send-keys -t autostart:claude Enter
}

head_of() {
    jailbee exec "$1" -- bash -lc "git -C $CONTAINER_REPO rev-parse HEAD" | tr -d '\r\n'
}

say "Creating feat-count and feat-crud"
jailbee new feat/count
jailbee new feat/crud

say "Verifying the agent is authenticated inside a container"
# Cheaper than discovering it from the frames of a finished take, which is how
# this failure was found the first time.
reply=$(jailbee exec feat-count -- bash -lc 'claude -p "reply with exactly: ok"' | tr -d '[:space:]')
[[ $reply == ok ]] || die "the agent answered '$reply', not 'ok' — re-run rig/seed-claude.sh"

say "Giving feat-count its task"
before=$(head_of feat-count)
send_task feat-count "$COUNT_TASK"

say "Waiting for feat-count's agent to commit (timeout ${COMMIT_TIMEOUT}s)"
deadline=$((SECONDS + COMMIT_TIMEOUT))
while [[ $(head_of feat-count) == "$before" ]]; do
    if ((SECONDS >= deadline)); then
        die "feat-count did not commit within ${COMMIT_TIMEOUT}s. Look at its window:
    jailbee tmux feat-count
  'Login expired' there means the credential went stale mid-run: re-run
  rig/seed-claude.sh and stage again."
    fi
    sleep 5
done
echo "    committed after $((SECONDS))s: $(jailbee exec feat-count -- bash -lc "git -C $CONTAINER_REPO log --oneline -1")"

say "Giving feat-crud its task"
# Deliberately last, and deliberately the largest of the three: it has to be
# visibly mid-work when the tape reaches it, seconds into the recording.
send_task feat-crud "$CRUD_TASK"

say "Ready to record: ./render.sh --record d"
