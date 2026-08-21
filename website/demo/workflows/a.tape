# Video A -- feature flow. Every command below runs for real against the
# nested Incus daemon; nothing here is staged.
#
# The human's own beats are the content, not padding: checking `jailbee ls` for
# the container's name, detaching while the agent works, coming back to it. Do
# not "optimise" them out -- see the design spec's section 2.
#
# THREE RULES THIS TAPE FOLLOWS DELIBERATELY:
#
# 1. NO FLAGS THE COMMAND DOES NOT NEED. `jb ls` runs bare: nobody types
#    `--fields name,base,state,network,mem` at a terminal, and a video that
#    shows them teaches a habit no user has. The default column set is what a
#    real user sees, it happens to carry WT / AHEAD ± / MERGE, and it is 100
#    characters wide -- inside the 105 this geometry gives. If a future take
#    needs fewer columns, that belongs in the substrate's `.jailbee/config.yaml`
#    (`ls.hide`), which is where a real user would put it, not on the command
#    line.
#
# 2. NO `clear`. Each act stays visible under the next one, so the viewer can
#    still see what produced the state being acted on. This is why `Set Height`
#    is 900 rather than 700: with nothing wiping the screen, the extra rows are
#    real content instead of the empty bottom third the 700px take had.
#
# 3. INTERACTIVE PROMPTS ARE CONTENT. `jb git pull` ends by asking whether to
#    destroy the container; the tape lets it ask and answers on camera. That
#    is a feature of the tool, and hiding it behind `--no-cleanup` made the
#    video quieter and less true at the same time.
#
# THE ONE RULE ABOUT Wait, learned by watching renders fail: wait only on a
# line jailbee always prints verbatim ("created and started", "passed",
# "Destroy container"). This image's shell prompt is `>` (15-prompt.sh), so a
# `$`-shaped prompt pattern never matches and fails the render at WaitTimeout
# instead of asserting anything. Every Wait below also targets the LAST thing
# its command puts on screen, which is what makes rule 2 safe: `Wait+Screen`
# only matches while its target is on the visible screen, so a target that is
# still scrolling upward is a 300-second timeout waiting to happen.
Source common.tape

Output a.webm
Output a.mp4

# Tall enough that three acts of history stay on screen at once, now that
# nothing clears it. Also the frame the agent's full-screen TUI gets.
Set Height 900

Hide
Type "cd ~/gisgro-incus-env/.local/video-rig/jailbee-demo && clear" Enter
Show

# --- act 1: what is already running here -----------------------------------
Type "jb ls" Enter
Wait+Screen /jailbee containers/
Sleep 4s

# --- act 2: a container for the new branch ---------------------------------
Type "jb new feat/health-endpoint" Enter
Wait+Screen@300s /created and started/
Sleep 3s

# "What was that container called again" -- the beat this video exists for.
# The same bare `jb ls` as act 1, one row longer: the table changing under the
# same command is the point, which is also why no column list is pinned.
Type "jb ls" Enter
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

# Third bare `jb ls`, and the reason the default columns are worth keeping:
# WT and ↑ have moved since act 2 because the agent has been committing.
Type "jb ls" Enter
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
Type "jb exec feat-health-endpoint -- uv run pytest -q" Enter
Wait+Screen@240s /passed/
Sleep 4s

# No --ff and no --no-cleanup. Without --ff this is jailbee's default: an
# explicit merge commit, made with `git merge --no-ff -m …`, so nothing opens
# an editor on camera. Without --no-cleanup the `pull:` policy stays at its
# default `prompt`, which is the beat below.
Type "jb git pull feat-health-endpoint" Enter
# The Wait targets the prompt, not `✓ Merged`, because the prompt is what ends
# up at the bottom of the screen: the command prints the bridge direction, the
# fetch summary, `✓ Merged '<branch>' … into 'main'` and `HEAD now at <sha>`,
# and only then asks. Waiting on `Merged` would be waiting on a line that four
# more lines have already pushed upward.
Wait+Screen@180s /Destroy container/
Sleep 4s

# Answered on camera, and answered "no": the container is still what video C
# reads the produced branch out of, and declining is the honest half of the
# question to show -- jailbee asks before it destroys. `[y/N]` would also
# accept a bare Enter; typing the `n` makes the choice legible in the frame.
Type "n" Enter
Sleep 3s

Type "git log --oneline -3" Enter
Sleep 6s
