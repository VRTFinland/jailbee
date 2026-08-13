#!/usr/bin/env bash
# Reconstruction of a successful `jb new feat/invoice-pdf` with the default
# config (no project-specific `.jailbee/config.yaml` autostart steps — see
# README.md for why none are shown). Every printed line is copied from the
# code that emits it; see README.md in this directory for the source of
# each one. Timings are compressed: the real command takes about a minute,
# and the page's caption says so — this clip does not have to.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
. "$here/_lib.sh"

type_out 'jb new feat/invoice-pdf'
sleep 0.6

# lifecycle.resolve_clone_ref() (src/jailbee/lifecycle.py): the branch
# doesn't exist yet, no --base was given, and `new.clone_from` defaults to
# "origin", so it fetches origin/<default_branch> on the host first.
#   info(f"→ Fetching origin/{fetch_branch} on host...")
# Real time: a host `git fetch`.
printf '%s\n' '→ Fetching origin/main on host...'
sleep 1.5

# lifecycle.new_container() (src/jailbee/lifecycle.py): the one status line
# printed before `incus init` / profile assignment / `incus start` / the
# in-container `git clone --shared`.
#   info(f"→ Creating '{short_name(cfg, name)}' from base image "
#        f"'{opts.from_base}' ({branch_note})...")
# `from_base` defaults to `golden.alias`, which config.py computes as
# "<container_prefix>-base" — "gisgro-base" for this demo's container
# prefix. `branch_note` for a new branch off the origin tip reads
# "new branch '<branch>' off 'origin/<source_branch>'".
# Real time: the golden-image copy, profile/limit assignment, container
# start, and the clone itself — most of the real ~1 minute lives here.
printf '%s\n' "→ Creating 'feat-invoice-pdf' from base image 'gisgro-base' (new branch 'feat/invoice-pdf' off 'origin/main')..."
sleep 2.5

# cli.py new_cmd(), immediately after new_container() returns:
#   success(f"Container '{short_name(cfg, created)}' created and started")
ok_line "Container 'feat-invoice-pdf' created and started"
