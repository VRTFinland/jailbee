# Video A -- feature flow. Every command below runs for real against the
# nested Incus daemon; nothing here is staged.
#
# The human's own beats are the content, not padding: checking `jailbee ls` for
# the container's name, detaching while the agent works, coming back to it. Do
# not "optimise" them out -- see the design spec's section 2.
#
# TWO RULES ABOUT Wait, both learned by watching renders fail:
#
# 1. Wait only on a line jailbee always prints verbatim ("created and started",
#    "passed"). This image's shell prompt is `>` (15-prompt.sh), so a
#    `$`-shaped prompt pattern never matches and fails the render at
#    WaitTimeout instead of asserting anything.
#
# 2. `Wait+Screen` matches the VISIBLE screen, so a Wait only works while its
#    target still fits on it. Measured: this tape's `jb new` Wait timed out at
#    300s with `Set Height 700` when a `jb ls` table had scrolled it out of
#    view, and the identical tape passed at `Height 1100`. The fix here is the
#    `clear` before each act that contains a Wait -- which is what a person
#    does anyway, and it keeps every frame legible. Do not remove them to save
#    a beat.
Source common.tape

Output a.webm
Output a.mp4

# Fits the tallest single act: the agent's full-screen TUI.
Set Height 700

Hide
Type "cd ~/gisgro-incus-env/.local/video-rig/jailbee-demo && clear" Enter
Show

# --- act 1: what is already running here -----------------------------------
Type "jb ls --fields name,base,state,network,mem" Enter
Wait+Screen /jailbee containers/
Sleep 4s

# --- act 2: a container for the new branch ---------------------------------
Type "clear" Enter
Sleep 1s

Type "jb new feat/health-endpoint" Enter
Wait+Screen@300s /created and started/
Sleep 3s

# "What was that container called again" -- the beat this video exists for.
Type "jb ls --fields name,base,state,network,mem" Enter
Sleep 5s

# --- act 3: hand the work to the agent -------------------------------------
Type "jb tmux feat-health-endpoint" Enter
Sleep 6s

# The agent is asked to commit as well: `jailbee git pull` at the end can only
# bring back work that exists as a commit in the container.
Type "Add a /health endpoint that returns the app version, with a test for it, then commit it." Enter
Sleep 8s

# Detach and do something else while it works. This is what a person does, and
# it is also why most of the dead time never reaches the video.
#
# Ctrl+a, NOT Ctrl+b: jailbee bind-mounts the host's ~/.tmux.conf into every
# container (the `host-tmux-conf` disk in <prefix>-binds), and this one sets
# `prefix C-a`. With Ctrl+b the detach silently does nothing and every command
# typed after it goes to the agent as a prompt instead — which is how this was
# found, from a `Pane is dead` render failure six minutes in. If the recording
# host's tmux.conf ever changes its prefix, both of these have to change too.
Ctrl+a
Type "d"
Sleep 2s

# Not `jb net status`: it lists every registered repo, which on this host
# includes jailbee's own dev repo and 25 lines of egress-pool detail that say
# nothing to a first-time viewer. This shows the same "is it really running?"
# instinct against something the video has actually built -- the dev server
# autostart brought up, on the port the site's own problem statement names.
Type "jb exec feat-health-endpoint -- curl -s localhost:8080/items" Enter
Sleep 6s

Type "jb ls --fields name,state,network,mem,wt,ahead_count" Enter
Sleep 5s

# --- act 4: come back to it ------------------------------------------------
Type "jb tmux feat-health-endpoint" Enter
# A fixed Sleep, not a Wait: the agent's window is a TUI with no stable "done"
# string to match. Tune this per take -- the proof that the work happened is
# the pytest run below, which does have a reliable Wait.
Sleep 45s

Ctrl+a
Type "d"
Sleep 2s

# --- act 5: check it, then take it home ------------------------------------
Type "clear" Enter
Sleep 1s

Type "jb exec feat-health-endpoint -- uv run pytest -q" Enter
Wait+Screen@240s /passed/
Sleep 4s

# A clear before this act's Wait, per rule 2 in the header: `jb git pull`
# prints roughly 25 lines and `✓ Merged` is the last of them, so on top of the
# pytest output above it lands right at the edge of a 700px screen -- the exact
# condition that made an earlier Wait time out for 300 seconds.
Type "clear" Enter
Sleep 1s

# --no-cleanup is not optional. Without it the command ends by asking
# "Destroy container 'feat-health-endpoint'? [y/N]", and the next beat's
# keystrokes are typed into that prompt instead of the shell -- found by
# watching the final frame of a render. It also keeps the container alive for
# video C, which reads the branch this one produced.
Type "jb git pull feat-health-endpoint --ff --no-cleanup" Enter
# Now a Wait, not a Sleep: the earlier note here said nobody had verified what
# this prints. A render showed it -- `✓ Merged '<branch>' from container
# '<name>' into 'main' (fast-forward).` -- so the fixed line exists and Wait is
# the honest choice.
Wait+Screen@180s /Merged/
Sleep 5s

Type "git log --oneline -3" Enter
Sleep 6s
