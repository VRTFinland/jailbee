# Recording a workflow clip

Everything needed to make another clip for the site, from a container that has
never done it before. The clips are real recordings of real `jailbee` commands
against a **nested** Incus daemon inside this repo's own dev container, so a
retake costs minutes and needs no host session.

The shipped clip is video A (`workflows/a.tape` → `../assets/media/a.*`), on
the site at [`#demos`](https://jailbee.gisgro.io/#demos).

## From nothing to a clip

Five commands, in this order. The first three are one-time per container; the
last two are the loop.

```bash
cd website/demo

rig/substrate.sh     # the small repo the videos are recorded against
rig/up.sh            # daemon, bridge, profiles, golden image  (~4m30s cold)
rig/seed-claude.sh    # an authenticated agent inside the containers

./render.sh --record a   # record video A     (~9 min)
./render.sh a            # re-cut it without re-recording  (~1 min)
```

`jailbee` must be on `PATH` as an **editable** install (`uv tool install -e .`
from the repo root). A non-editable install bakes a stale copy of the
provisioning tree into the golden image, and the failures that follow point
nowhere near the cause.

`rig/up.sh` is idempotent, and **two of its steps do not survive a container
restart** (the `/dev/dri` mask and the Chrome chmod). Re-running it after a
restart costs about a second — do it before every session. `rig/README.md` has
the full table of what it does and why each step exists.

## Before every take

Three things, every single time. Each one has cost a wasted take.

```bash
# 1. Re-seed the agent's credentials, and verify them INSIDE a container.
rig/seed-claude.sh
jailbee exec feat-warm -- bash -lc 'claude -p "reply with exactly: ok"'

# 2. Reset the substrate. Note the -C: see "Two git repos" below.
git -C ../../.local/video-rig/jailbee-demo reset --hard origin/main

# 3. Destroy the container the tape creates, if a failed take left one.
(cd ../../.local/video-rig/jailbee-demo && jailbee destroy feat-health-endpoint --force)
```

Why each:

1. **The seeded credential is a snapshot** of the maintainer's own OAuth token,
   and the Claude Code running *in this dev container* keeps rotating that
   token underneath it. Worse: when the agent inside a demo container finds a
   stale token it writes the **shared** `.credentials.json` back as an empty
   stub (509 bytes becomes 281, `expiresAt` 0) — so one failed take breaks
   every container until the seed is re-run. The symptom on camera is the
   agent's window saying `Login expired`, and the take renders and exits 0
   anyway.
2. **A successful take leaves its own merge on the substrate's `main`.** Video
   A's premise is that `/health` does not exist yet, so recording against an
   un-reset substrate records the opposite of what the tape claims.
   `render.sh --record` refuses to start until `main` is back at
   `origin/main`, which is the only reason that mistake is cheap.
3. `jailbee new` fails on an existing container. Video A destroys its own on
   camera, so this is only needed after a take that did not finish.

`feat-warm` is a container kept running on purpose: it keeps the shared
`claude-install` store warm, so a new container pays ~1.5s for Claude instead
of 39s. Do not destroy it.

## After every take: is it valid?

A render that exits 0 is not a valid take. `render.sh --record` checks the one
thing that cannot be faked — a tape containing `git pull` must have moved the
substrate — and fails with instructions when it did not. Beyond that, **look
at the frames**. Every failure so far was visible only there:

```bash
ffprobe -v error -show_entries format=duration -of default=nw=1 workflows/a.webm
ffmpeg -i workflows/a.webm -ss 45 -frames:v 1 /tmp/f.png -y   # note the order
```

**`-ss` goes AFTER `-i`.** The fast form seeks to the nearest keyframe, and
these renders have sparse keyframes across exactly the static stretches worth
inspecting — it silently returns a frame from many seconds earlier. Two wrong
readings of where the acts were came from getting this backwards.

## Cutting dead time

`cuts/<name>.cuts` is a per-clip list of spans to speed up. Times are seconds
into the **raw** render under `workflows/`. Find them rather than guessing:

```bash
ffmpeg -i workflows/a.webm -vf freezedetect=n=-60dB:d=3 -map 0:v -f null -
```

Every sped-up span is stamped with a visible `×N` badge for exactly as long as
it runs. That is not decoration and not optional: dead time may be removed,
but the viewer has to be told. `render.sh` also dumps the frame on each side
of every boundary into `workflows/<name>.boundaries/`, because a hand-measured
time is only valid for the take it was measured against — look at those two
frames after any change to the tape.

`render.sh --help`-style detail lives in the script's header comment, which is
the authority on the cut-list format.

## The rules that cost failed renders

The authority is the header comment of `workflows/a.tape`, which states each
rule next to the beat that depends on it. In short, and none of it is in the
VHS documentation:

- **`Ctrl+a`, not `Ctrl+b`** — the host's `~/.tmux.conf` is bind-mounted into
  every container and sets `prefix C-a`. With `Ctrl+b` the detach silently
  does nothing and every later keystroke is typed into the agent as a prompt;
  the render dies minutes later with `Pane is dead`.
- **`Wait+Screen` for printed output, `Wait+Line` for prompts, never the
  reverse.** `Wait+Screen` goes permanently stale after a full-screen app (the
  agent's TUI) — it keeps reporting the pre-tmux screen — and it cannot see an
  interactive prompt at all, a prompt being the one line with no trailing
  newline. `Wait+Line` matches the line the cursor is on, stays live after
  tmux, and is undocumented in VHS's manual though 0.11 accepts it.
- **No `Wait` is an assertion unless its pattern cannot match a failure.**
  `passed` matches `2 failed, 3 passed in 0.21s`.
- **Interactive prompts are content.** Let commands ask, and answer on camera.
  Suppressing questions with `--no-cleanup` / `--force` makes a video quieter
  and less true at the same time.
- **No flags a person would not type.** Nobody types
  `jb ls --fields name,base,state,network,mem`. Column choice, if a take needs
  it, belongs in the substrate's `ls.hide`.
- **`Output` paths must be relative**, and VHS reports an absolute one as three
  unrelated syntax errors about the path components.

## Two git repos

There are always two: this repo, and the substrate at
`.local/video-rig/jailbee-demo`. The substrate's `main` is reset before every
take, so write every substrate command as `git -C <substrate> …`. A bare
`git reset --hard origin/main` that lands in the jailbee repo instead moves
the working branch to `origin/main` and destroys every uncommitted file — that
has happened, and recovery was `git reflog`. **Commit tape and script edits
before starting a take.**

## Honesty rules

These bind every clip, and they are why the videos are worth making at all:

- Every command and every line of output is executed for real. No
  transcription, no reconstruction.
- The agent in the recording is real, and no caption claims **what** it wrote
  — only that it ran inside the boundary. That is what makes a
  nondeterministic agent affordable: any take where it produced something is a
  valid take.
- **No caption states a duration.** Recording happens against a nested Incus
  daemon, and nested timings are not host timings. This is also why the golden
  image build is not in any clip.
- Nothing is sped up without the `×N` marker on screen.
- `Hide`/`Show` covers VHS's own shell startup, never content.
- The shell prompt says nothing about the dogfood container or the
  maintainer's machine.

## Adding a second clip

Two jobs, not one. The page currently ships one clip and therefore no tab
chooser, and `tests/test_website.py::test_a_second_clip_brings_the_demo_tabs_back`
fails the moment a second `<video>` appears without the radio-group markup —
restore it from `a68553e`, which pulled it, with matched labels, panels and
exactly one `checked`.

The rest of the site contract, also enforced by tests: every clip
click-to-play (`controls`, `preload="none"`, `poster`, `muted`, `playsinline`,
and **no** `loop`/`autoplay`, which belonged to the retired hero loops), and
every clip the page names must actually be committed. Editing
`../assets/style.css` also means updating the `?v=` cache-buster in **both**
`index.html` and `comparison.html`; the test fails with the value to paste.

`README.md` at the repo root shows the clip too, as a poster linked to the
site — not a `<video>`, because that file is also the PyPI long description
and PyPI's sanitiser strips the tag. Its image URL is absolute and points at
`main`, so it 404s until the work lands there.

## What is where

| | |
|---|---|
| `substrate/` | the demo app, committed — `rig/substrate.sh` turns it into a git repo with a local bare origin |
| `rig/` | one-time environment setup; `rig/README.md` explains every step and why it exists |
| `workflows/*.tape` | the tapes. `common.tape` is the shared look; each video sizes its own `Height` |
| `cuts/*.cuts` | per-clip speed-up lists, timed against one specific take |
| `render.sh` | record, cut, poster, and the take-is-valid check |
| `../assets/media/` | what the page actually serves, committed |

Raw renders under `workflows/` and the boundary frames are gitignored: they
are intermediates, and the clip that ships is the one in `../assets/media/`.
