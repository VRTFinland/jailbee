#!/usr/bin/env bash
# `jb ls --fields ...` over three synthetic containers, rendered by
# JailBee's own table code — see README.md ("generated/ls.txt — generated").
#
# The typed command must name the same `--fields` list `generate.py`'s
# FIELDS constant renders with, not bare `jb ls`: the default column set
# (CREATED, IP, ...) doesn't match this table, so a reader who typed the
# bare command would get a different result than this clip shows.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
. "$here/_lib.sh"

type_out 'jb ls --fields name,base,state,network,ttl,mem,wt,ahead_count,pr'
sleep 0.5
cat "$here/generated/ls.txt"
