#!/usr/bin/env bash
# strict -> blocked curl -> loose -> the same curl succeeding, for one
# container. Every printed line's source is in README.md — read that file's
# "net.sh" section before touching this, especially the note on the
# blocked-curl gap: the exact failure text a strict-mode ACL produces is
# NOT established anywhere in this repo's docs or code, so it is not shown
# here (see README.md; do not add an invented error message).
#
# `net loose` is shown with `--for 45m` deliberately: with no `--for` /
# `--no-revert` and a TTY, `net_loose` (cli.py, 4775-4787) runs
# `_prompt_loose_ttl()` — a `questionary.select` menu — *before* `_switch()`
# prints the success line, the same "this command has a hidden interactive
# step" hazard `git.sh` sidesteps with `--plain`. `--for 45m` also puts the
# revert duration the caption promises somewhere in the clip, since
# `_switch()`'s own success line never states one (see README.md).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
. "$here/_lib.sh"

# cli.py `_switch()`, called by `net_strict`: the only line a successful
# `jb net strict` prints.
#   success(f"Container '{short_name(cfg, resolved)}' is now on network: {mode}")
type_out 'jb net strict feat-invoice-pdf'
sleep 0.5
ok_line "Container 'feat-invoice-pdf' is now on network: strict"
sleep 0.3

# Blocked request from inside the container. network.py's strict-mode ACL
# is default-deny via Incus's NIC-level "default-reject" (see network.py,
# near line 129) — deny, not silent drop — but neither docs/security.md,
# docs/troubleshooting.md nor the code spells out what curl's own stderr
# text would be for that. Per the honesty rule, no error text is invented:
# `-s` (no `-S`) suppresses it, and `-w '%{http_code}'` — curl's own
# documented behaviour, not jailbee's — writes "000" whenever no HTTP
# response arrived. "000" here is not derived from curl's manual in the
# abstract: it is what this exact command
# (`curl -s -o /dev/null -w '%{http_code}\n' --max-time 8 <url>`) has been
# observed to print from inside a real strict-mode container. See
# README.md for what's still not established (the underlying rejection
# mechanism, and curl's stderr text if `-S` were added back).
type_out "curl -s -o /dev/null -w '%{http_code}\n' --max-time 8 https://pypi.org/simple/"
sleep 1.8
printf '000\n'
sleep 0.3

# cli.py `_switch()` again, via `net_loose`, with `--for 45m` so this
# doesn't hit `_prompt_loose_ttl()`'s interactive TTL menu (net_loose,
# cli.py 4775-4787: with no --for/--no-revert and a TTY, the menu runs
# *before* this line). The same success() line as strict — this is the
# *entire* confirmation the real command prints; no revert deadline is
# included in it (see README.md). "45m" is the only place in this clip the
# caption's "reverts on its own after a set time" is visible at all.
type_out 'jb net loose feat-invoice-pdf --for 45m'
sleep 0.5
ok_line "Container 'feat-invoice-pdf' is now on network: loose"
sleep 0.3

# Loose mode is full NAT (no ACL): the same request now completes. "200" is
# a fact about pypi.org's own simple index (a live, external service), not
# a jailbee claim — shown for the same reason as above: it's the one thing
# that can be asserted without guessing at network-stack-specific text.
type_out "curl -s -o /dev/null -w '%{http_code}\n' --max-time 8 https://pypi.org/simple/"
sleep 0.8
printf '200\n'
