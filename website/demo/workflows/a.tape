# Video A -- feature flow. Every command below runs for real against the
# nested Incus daemon; nothing here is staged.
#
# The human's own beats are the content, not padding: checking `jailbee ls` for
# the container's name, detaching while the agent works, coming back to it,
# seeing the commit land, taking it home, cleaning up. Do not "optimise" them
# out -- see the design spec's section 2.
#
# FOUR RULES THIS TAPE FOLLOWS DELIBERATELY:
#
# 1. NO FLAGS THE COMMAND DOES NOT NEED. `jb ls` runs bare: nobody types
#    `--fields name,base,state,network,mem` at a terminal, and a video that
#    shows them teaches a habit no user has. The default column set is what a
#    real user sees, it carries WT / AHEAD ± / ↑ / MERGE, and it is 100
#    characters wide -- inside the 105 this geometry gives. If a future take
#    needs fewer columns, that belongs in the substrate's `.jailbee/config.yaml`
#    (`ls.hide`), which is where a real user would put it, not on the command
#    line. `jb git pull` and `jb destroy` likewise run bare, which is what
#    makes rule 3 possible.
#
# 2. NO `clear`, ANYWHERE. Each act stays visible under the next one for as
#    long as the screen holds it, so the viewer can see what produced the state
#    being acted on. `Set Height 900` is what pays for that. The one `clear`
#    this tape used to need was a workaround for `Wait+Screen` going stale
#    after a full-screen app; rule 4 replaces it.
#
# 3. INTERACTIVE PROMPTS ARE CONTENT. `jb git pull` asks whether to destroy the
#    container and `jb destroy` asks before it does; the tape lets both ask and
#    answers on camera. Suppressing them with `--no-cleanup` / `--force` made
#    the video quieter and less true at the same time.
#
# 4. WAIT ON A PROMPT WITH `Wait+Line`, ON PRINTED OUTPUT WITH `Wait+Screen`,
#    AND NEVER THE OTHER WAY AROUND. This is the whole of what four failed
#    renders taught, and none of it is in the VHS docs -- `Wait+Line` is not
#    even mentioned there, though vhs 0.11 accepts it:
#
#    * `Wait+Screen` matches the visible screen, so it sees completed output
#      lines -- but a full-screen app (the agent's TUI, via tmux) leaves that
#      capture permanently stale: it keeps reporting the pre-tmux screen no
#      matter what is printed afterwards. Measured twice, identically:
#      `Wait+Screen /passed/` burned its whole 240s timeout and named act 2's
#      `jb ls` table as the last thing it saw. So every `Wait+Screen` here is
#      before the first `jb tmux`, and there are none after it.
#    * `Wait+Line` matches the line the cursor is on, and that capture is still
#      live after tmux -- verified by probe. It cannot be used on completed
#      output (the line has scrolled past by the time it polls) but it is the
#      only thing that can match an interactive prompt, which is the one line
#      with no trailing newline and therefore absent from the screen capture
#      altogether. A `Wait+Screen` on prompt text always times out.
#    * Never wait on a shell prompt: this image's is `>` (15-prompt.sh), so a
#      `$`-shaped pattern fails the render instead of asserting anything.
#    * And no `Wait` is an assertion unless its pattern cannot match a failure.
#      `passed` matches `2 failed, 3 passed in 0.21s`. The take-is-valid check
#      lives in render.sh instead.
Source common.tape

Output a.webm
Output a.mp4

# Tall enough to hold three acts at once. Content scrolls past it later in the
# video, which is fine: nothing after the agent waits on the screen.
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
# bring back work that exists as a commit in the container. It runs the tests
# itself, which is why this tape does not: `jb exec -- pytest` is not something
# a person types when an agent is already doing it in the next window.
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

Type "jb ls" Enter
Sleep 5s

# --- act 4: come back to it ------------------------------------------------
Type "jb tmux feat-health-endpoint" Enter
# Deliberately longer than the agent needs, because the cut list speeds this
# span up ×6 in post: a generous wait costs ten seconds of screen time instead
# of sixty, and a too-short one costs the whole take. There is no Wait to be
# had here -- the agent's window is a TUI with no stable "done" string, and
# `Wait+Screen` is stale from here on anyway (rule 4).
Sleep 60s

Ctrl+a
Type "d"
Sleep 2s

# --- act 5: see it, take it home, clean up ---------------------------------
# The same bare `jb ls` for the third time, and now it says something new: ↑ 1
# and a non-clean AHEAD ±, because the agent committed inside the container.
# That is the column set earning its place.
Type "jb ls" Enter
Sleep 6s

# No --ff and no --no-cleanup. Without --ff this is jailbee's default: an
# explicit merge commit, made with `git merge --no-ff -m …`, so nothing opens
# an editor on camera. Without --no-cleanup the `pull:` policy stays at its
# default `prompt`, and the question below is the point.
Type "jb git pull feat-health-endpoint" Enter
Wait+Line@180s /Destroy container/
Sleep 4s

# Answered "no" here on purpose: the point of the next beat is that destroying
# is its own deliberate act, after looking at what landed.
Type "n" Enter
Sleep 3s

Type "git log --oneline -3" Enter
Sleep 6s

# And now, having seen the merge commit sitting on top of the agent's, throw
# the container away -- bare, so it asks, and answered on camera.
Type "jb destroy feat-health-endpoint" Enter
Wait+Line@120s /Destroy container/
Sleep 4s

Type "y" Enter
Sleep 5s
