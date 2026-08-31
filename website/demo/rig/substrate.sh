#!/usr/bin/env bash
# Create the demo substrate: the small repo the workflow videos are recorded
# against. Run this FIRST in a fresh container — `up.sh` refuses to do anything
# without it.
#
#   ./substrate.sh
#
# What it builds, under .local/video-rig/ (gitignored, so it never enters the
# jailbee repo's history and never reaches PyPI):
#
#   jailbee-demo/       a working copy, `main` at the commit video A starts from
#   jailbee-demo.git    a local bare origin, so `origin/main` exists
#
# The local bare origin is not a convenience: `jailbee new` clones from a ref
# and `jailbee git pull` compares against `origin/main`, and the substrate is
# deliberately not on GitHub — a public demo repo would be one more thing to
# keep honest, and this container's PAT cannot write to one anyway.
#
# The file tree comes from ../substrate/, which IS committed: the app is
# reviewable source rather than a heredoc, and a change to it is a change
# someone can read in a diff. The `45-uv.sh` install snippet is copied from
# the jailbee repo's own `.jailbee/install.d/` instead of being duplicated —
# the substrate needs uv in its golden image, and one copy of that snippet is
# enough. (The plan for these videos assumed uv was already in every golden
# image. It is not: `45-uv.sh` is jailbee's own repo snippet, and without it
# the substrate's `sync-deps` autostart step dies with exit 127.)
#
# Two commits, not one, and their messages matter: `git log --oneline -3` is
# on camera at the end of video A, so the tip message is something a viewer
# reads. They reproduce the history the shipped clip was recorded against.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SOURCE="$REPO_ROOT/website/demo/substrate"
SUBSTRATE="${SUBSTRATE:-$REPO_ROOT/.local/video-rig/jailbee-demo}"
ORIGIN="${ORIGIN:-${SUBSTRATE}.git}"
UV_SNIPPET="$REPO_ROOT/.jailbee/install.d/45-uv.sh"

say() { printf '\n==> %s\n' "$1"; }
die() {
    echo "error: $*" >&2
    exit 1
}

[[ -d $SOURCE ]] || die "no substrate source at $SOURCE"
[[ -f $UV_SNIPPET ]] || die "no uv snippet at $UV_SNIPPET — the substrate needs it in its image"

# Non-destructive on purpose: the substrate accumulates recorded takes, and
# `main` moving past `origin/main` is the normal state after a take rather than
# damage. Resetting it is one command and belongs to whoever is recording.
if [[ -e $SUBSTRATE ]]; then
    die "$SUBSTRATE already exists. To reset it for a take:
    git -C $SUBSTRATE reset --hard origin/main
  To rebuild it from scratch, remove it and its origin first:
    rm -rf $SUBSTRATE $ORIGIN"
fi

git config --get user.email >/dev/null || die "git has no user.email — the substrate's commits need one"

say "Creating the bare origin at $ORIGIN"
mkdir -p "$(dirname "$ORIGIN")"
git init --quiet --bare --initial-branch=main "$ORIGIN"

say "Laying out the substrate at $SUBSTRATE"
mkdir -p "$SUBSTRATE"
# -a so the dotfiles come too: .gitignore and .jailbee/ are the whole point.
cp -a "$SOURCE/." "$SUBSTRATE/"
mkdir -p "$SUBSTRATE/.jailbee/install.d"
cp "$UV_SNIPPET" "$SUBSTRATE/.jailbee/install.d/"

git -C "$SUBSTRATE" init --quiet --initial-branch=main
git -C "$SUBSTRATE" remote add origin "$ORIGIN"

say "Committing"
# Commit one: everything but the API tests. Commit two: the tests, whose
# message is the line `git log --oneline -3` shows on camera under the
# agent's own commit.
git -C "$SUBSTRATE" add -A
git -C "$SUBSTRATE" reset --quiet -- tests/test_api.py
git -C "$SUBSTRATE" commit --quiet -m "feat: a tiny FastAPI app to demonstrate jailbee"
git -C "$SUBSTRATE" add -- tests/test_api.py
git -C "$SUBSTRATE" commit --quiet -m "test: make the tests fail when the implementation is wrong"

say "Publishing main to the bare origin"
git -C "$SUBSTRATE" push --quiet --set-upstream origin main

say "Substrate ready"
git -C "$SUBSTRATE" log --oneline | cat
echo
echo "Next: ./up.sh, then ./seed-claude.sh — see ../README.md for the whole loop."
