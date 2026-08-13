#!/usr/bin/env bash
# Shared helpers for the demo scene scripts. Sourced, never executed
# directly (no shebang execution path — `set -euo pipefail` is left to the
# scripts that source this).
#
# Clearing the screen here, as the first act of being sourced, is
# deliberate: the tapes `Type` the scene's own invocation (e.g.
# `../scenes/new.sh`) into the terminal before `Show`, and the shell echoes
# that line regardless of the tape's `Hide`/`Show` bracketing. A `clear`
# run *inside* the tape (before typing the invocation) can't help — VHS
# still shows the invocation line once `Show` fires. Clearing here instead,
# as the very first thing every scene script does once it starts running,
# erases that echoed invocation before the scene prints anything of its
# own, regardless of how the tape (or a human at a real prompt) invoked it.
printf '\033[2J\033[3J\033[H'
#
# `type_out` prints the prompt, pauses briefly, then prints the given
# command text — paced so a VHS capture reads like someone actually typing,
# not an instant dump. It never runs the command; the scene scripts print
# a hand-picked, honesty-checked reconstruction of what the real command
# would print (see README.md for the source of every line).
#
# `ok_line` reproduces `success()` in src/jailbee/tui.py:
#   console.print(f"[green]✓[/green] {msg}")
# — a green "✓ " prefix (the site's own --green, #74c47c) followed by the
# plain-coloured message.

PS_PROMPT='\033[38;2;240;169;43m~/gisgro\033[0m $ '

type_out() {
  printf '%b' "$PS_PROMPT"
  sleep 0.4
  printf '%s\n' "$1"
}

ok_line() {
  printf '\033[38;2;116;196;124m✓\033[0m %s\n' "$1"
}
