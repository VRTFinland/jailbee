# Video D -- the dashboard. Every command runs for real against the nested
# Incus daemon. The two agents already at work were given their tasks by
# rig/stage-d.sh before this tape ran, and the page's caption says so.
#
# GEOMETRY IS A'S, EXACTLY (Source common.tape + Height 900). Two clips of the
# same size let the page's tabs swap content without the layout moving. If the
# table ever stops fitting, drop a column from the `dashboard:` block rig/up.sh
# writes -- never change the geometry here.
#
# FOUR THINGS THIS TAPE KNOWS THAT THE DASHBOARD'S SOURCE DOES NOT SAY:
#
# 1. NAVIGATE WITH j/k, NOT WITH ARROWS. VHS's `Down` never arrives: in
#    alt-screen mode its terminal emits ESC O B for an arrow, and
#    dashboard.KEY_BINDINGS maps ESC [ B and `j` only. The same keypress works
#    in a real tmux pty, which is how this was pinned down. j/k is also what
#    the dashboard's own hint line offers, so nothing here is a workaround a
#    viewer would notice.
#
# 2. THE HIGHLIGHT STARTS ON THE REPO GROUP HEADER, not on a container, and
#    Enter there FOLDS THE GROUP instead of opening a menu. Hence `j` twice
#    before the first Enter: past feat-crud, onto feat-count.
#
# 3. ROWS ARE NEWEST FIRST inside a group. The container this tape creates
#    appears at the TOP of jailbee-demo, which is why reaching it afterwards
#    means moving UP.
#
# 4. NOTHING INSIDE THE DASHBOARD CAN BE WAITED ON. It is a Rich
#    Live(screen=True) full-screen app from its first frame, so every beat here
#    is Sleep-driven (see a.tape's rule 4). The two exceptions are real
#    prompts, which is what Wait+Line matches: `n`'s two questions, and the
#    pause `jailbee new` prints when it is done.
Source common.tape

Output d.webm
Output d.mp4

Set Height 900

Hide
Type "cd ~/gisgro-incus-env/.local/video-rig/jailbee-demo && clear" Enter
Show

# --- act 1: what is going on, across both repos -----------------------------
Type "jb dashboard" Enter
Sleep 11s

# feat-count is the one with a commit on it (↑ 1): its agent is done. Two rows
# down, because the highlight starts on the group header.
Type "j"
Sleep 2s
Type "j"
Sleep 3s

# The menu, opened once so the whole vocabulary is on screen -- diff, send
# commits to host, destroy -- before the video starts using the shortcut for
# the one thing it needs. Attach tmux is the fifth entry for a container that
# is ahead with a clean tree and no PR; counted from a frame, not from
# menu_actions, because a background-job row would sit above it.
Enter
Sleep 4s
Type "j"
Sleep 1s
Type "j"
Sleep 1s
Type "j"
Sleep 1s
Type "j"
Sleep 2s
Enter
Sleep 8s

# --- act 2: the finished agent gets the next task ---------------------------
Type "Add a ?q= name filter to /items, with a test, then commit." Enter
Sleep 7s

# Ctrl+a, NOT Ctrl+b: jailbee bind-mounts the recording host's ~/.tmux.conf
# into every container and this one sets `prefix C-a`. With Ctrl+b the detach
# silently does nothing and every keystroke after it is typed into the agent.
Ctrl+a
Type "d"
Sleep 4s

# --- act 3: a look at the one that is still working -------------------------
# `t` this time, not the menu. The hint line at the bottom says `t tmux`, and
# this is the beat that shows it means what it says.
Type "k"
Sleep 2s
Type "t"
Sleep 9s

Ctrl+a
Type "d"
Sleep 4s

# --- act 4: new work starts in the same place -------------------------------
Type "n"
Wait+Line /New branch/
Type "feat/pagination" Enter
Wait+Line /Base branch/
# Enter alone: the prompt already offers [main], and typing what is already
# offered is not what a person does.
Enter
# And here nothing can be waited on at all, which cost one take to learn.
# `jailbee new`'s "press Enter to return to the dashboard" is printed WITH a
# trailing newline, so by the time it is on screen the cursor has moved to the
# blank line below it -- and Wait+Line matches the line the cursor is on. It
# burned its whole 300s timeout reporting an empty last value. Wait+Screen is
# no use either: it has been stale since the first tmux attach.
#
# So: a generous Sleep, sped up ×6 in the cut list. Pressing Enter early is
# harmless -- the terminal is in cooked mode here, so the keystroke waits in
# the buffer until readline() asks for it -- but exactly ONE Enter, because a
# second one would reach the dashboard's key loop and open a menu.
Sleep 80s
Enter
Sleep 6s

# --- act 5: hand it its task too --------------------------------------------
# Up, not down: the new container is the newest row in its group.
Type "k"
Sleep 2s
Type "t"
# Deliberately generous: the agent's startup banner scrolls away on its own,
# and this Sleep sits inside a ×6 span in the cut list, so waiting long costs
# about three seconds of screen time and waiting short costs the take.
Sleep 22s
Type "Add ?limit= and ?offset= pagination to /items, with a test, then commit." Enter
Sleep 7s

Ctrl+a
Type "d"
Sleep 5s

# --- act 6: three agents at work, and nothing to do but wait ----------------
Sleep 9s
Type "q"
Sleep 3s
