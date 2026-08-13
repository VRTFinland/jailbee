#!/usr/bin/env bash
# `jb git push` sending a host branch into a container's clone — the
# container-as-git-remote bridge. Every line's source is in README.md.
#
# Uses `--plain` (transport only, no merge/rebase/reset in the container)
# so the scene shows exactly one deterministic path through
# `_do_single_push()` rather than depending on `push.default_action`
# (which defaults to "ask" and would need a TTY prompt to resolve).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
. "$here/_lib.sh"

# The caption on the page (website/index.html, #demo-git) shows
# `jb git push feat/invoice-pdf` — a branch-style name with a slash. The
# `push` command's positional argument is a *container* name
# (`completion.complete_container`), which never contains a slash; the
# container from the other three scenes is `feat-invoice-pdf`. This script
# types the real, working form. See README.md / task-5-report.md for this
# mismatch, flagged for the caption to be reconciled rather than bent here.
type_out 'jb git push feat-invoice-pdf --plain'
sleep 0.5

# cli.py `_print_bridge_direction()`, called from `_do_single_push()`
# before the detailed summary:
#   info(f"{src} ({src_side}) ──▶ {dst} ({dst_side})")
# `src` is the resolved push source (push.default_source defaults to
# "base" — the container's base branch label, "main" for this demo
# container per generate.py's synthetic data); `dst` is `container_ref`,
# which `sync.push_to_container()` sets to `refs/jailbee/host/<source>`.
printf '%s\n' 'main (host) ──▶ refs/jailbee/host/main (container)'
sleep 0.4

# cli.py `_print_push_summary()`:
#   info(f"Pushed '{result.source}' ({result.source_ref}) from host into "
#        f"container '{short}' as {result.container_ref} ({delta}).")
# `source_ref` is `refs/remotes/origin/main` (push.push_from defaults to
# "origin"). The OIDs are synthetic demo data (same category as
# generate.py's invented ContainerInfo rows) standing in for a real
# `old_oid[:7] -> new_oid[:7]` delta.
printf '%s\n' "Pushed 'main' (refs/remotes/origin/main) from host into container 'feat-invoice-pdf' as refs/jailbee/host/main (5f3d914 -> 9c1a7be)."
sleep 0.4

# cli.py `_do_single_push()`, plain-action branch:
#   success("Push complete.")
ok_line 'Push complete.'
