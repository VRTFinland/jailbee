# Demo scenes

What builds the terminal output shown on the website — with one exception
that never reaches the page (`generated/net-switch.txt`, see below).
Nothing here is invented — each file under `generated/` is either produced
by JailBee's own code or transcribed verbatim from it, and
`tests/test_website.py` pins both against the generator so a code change
that would make a scene stop matching reality fails the suite instead of
leaving a stale claim on the page.

This file documents what Task 4 (the table scenes) built, then what Task 5
(the four scene scripts VHS records) added below.

## Which scenes are generated vs. hand-transcribed

- **Generated** (real render, invented data): `generated/ls.txt`,
  `generated/net-switch.txt` — see the two sections below. Both are pinned
  against `website/demo/generate.py` by
  `tests/test_website.py::test_generated_scenes_match_what_jailbees_renderer_produces_today`.
- **Hand-transcribed** (the four scripts in this directory): `new.sh`,
  `parallel.sh`, `net.sh`, `git.sh`, plus the shared `_lib.sh` they source.
  Nothing in them touches Incus, a database, or the network — every line
  they print is either `cat`ed from a generated file (`parallel.sh`) or a
  literal copy of a string built by a named function in `src/jailbee/`, with
  that function cited in a comment directly above the line in the script.
  There is no pinning test for these four — the transcription is verified by
  reading the source at the time of writing, not re-derived at test time —
  so a future change to one of the cited functions can make a script stop
  matching reality without a test failing. If you touch `cli.py`'s
  `new_cmd`, `_switch`, `_do_single_push`, `_print_push_summary`, or
  `_print_bridge_direction`, or `lifecycle.py`'s `new_container` /
  `resolve_clone_ref`, re-check the corresponding script by hand.

Timings in all four scripts are compressed on purpose: the `sleep` calls
mark where the real command spends time (a host fetch, the golden-image
copy + clone, etc.) without making a viewer sit through the real ~1 minute.
The page's own caption for `#demo-new` states the real duration
("About a minute in real time"), so the clip doesn't have to.

## Replacing a reconstruction with a real capture

Every script here is a hand-checked reconstruction, not a live recording —
that's the whole point of the honesty rule, but it also means each one is a
standing claim that can go stale the moment the cited function's output
changes (see the "no pinning test for these four" note above). Any of them
can be swapped for an actual terminal session without touching Task 6's
VHS tape at all:

1. Record the real command on a host with a working `jailbee`/`jb` install
   and a real Incus daemon, e.g.:
   ```
   script -q -c 'jb new feat/x' /tmp/new.txt
   ```
   (`script` captures the full terminal session — prompt, command, and
   real output — byte for byte, including the ANSI a real Rich `Console`
   emits.)
2. Pass the captured file to Task 6's `render.sh --transcript /tmp/new.txt`
   instead of one of these `.sh` scripts. `render.sh` accepts either a
   reconstruction script (this directory) or a raw transcript file
   interchangeably, so a real capture takes a reconstruction's place
   without editing the VHS tape itself.

This is the intended way to close any of the gaps flagged below (the
blocked-`curl` mechanism, the `net loose` revert deadline) once someone can
run the real command against a real strict-mode container and capture what
actually happens, rather than by editing the reconstruction's guesses.

### `new.sh`

Reconstructs `jb new feat/invoice-pdf` with the *default* config — no
project's `.jailbee/config.yaml` autostart steps. Sources, in order:

- `→ Fetching origin/main on host...` — `lifecycle.resolve_clone_ref()`,
  reached because the branch doesn't exist yet, no `--base` was given, and
  `new.clone_from` defaults to `"origin"`.
- `→ Creating 'feat-invoice-pdf' from base image 'gisgro-base' (new branch
  'feat/invoice-pdf' off 'origin/main')...` — `lifecycle.new_container()`,
  the one status line before `incus init`/`start`/clone. `gisgro-base`
  comes from `config.py`'s computed `golden.alias` default
  (`"<container_prefix>-base"`).
- `✓ Container 'feat-invoice-pdf' created and started` —
  `cli.py new_cmd()`, right after `new_container()` returns:
  `success(f"Container '{short_name(cfg, created)}' created and started")`.

**What's deliberately not shown:** `cfg.autostart.on_create` is empty in a
default/no-target-repo config, so `lifecycle.run_autostart()` returns before
printing anything (`if not steps: return`). Autostart step names are
entirely project-config-defined text, not jailbee's own strings, so
inventing plausible-looking step names (e.g. "install deps") for this demo
would be exactly the kind of fabricated output this whole approach exists
to avoid. The page's caption ("provisioned and ready to work in") is true of
the golden-image copy + clone this scene does show; it does not imply
autostart steps ran.

### `parallel.sh`

Trivial by design (see Step 3 of the plan): prints the prompt, `jb ls
--fields ...`, then `cat`s `generated/ls.txt` — see that section below.

**Typed command matches the table's actual fields.** The script types the
literal `--fields` list `generate.py`'s `FIELDS` constant renders with
(`name,base,state,network,ttl,mem,wt,ahead_count,pr`), not bare `jb ls`.
Bare `jb ls` prints the *default* column set — CREATED and IP included,
both `default_table=True` — which is wider and different from this table;
a reader who typed what an earlier version of this scene showed would have
gotten a table that didn't match.

**Resolved: the table did wrap at the original 11-column `FIELDS`.** At
`Width 1200`/`FontSize 18` the recording terminal is roughly 105 columns,
and the original set (`name,mode,base,state,network,ttl,mem,wt,ahead_count,conflict,pr`)
rendered at 111 visible columns — this was flagged as untestable without a
real `vhs` render, and once rendered, it did wrap, breaking the box-drawing
borders. Fixed by dropping `mode` (always `clone` in this demo — every
`jailbee` clone is a clone) and `conflict` (always `ok` — merge-conflict
detection has nothing to show here) from `FIELDS`. The remaining nine
fields render at 95 visible columns, comfortably under budget; the MEM
column's `used / limit` form is still the widest part of it. `parallel.sh`
and the `#demo-parallel` `<code>` label in `website/index.html` were
updated to match — all three stay byte-identical.

### `net.sh`

Reconstructs `jb net strict feat-invoice-pdf` → a blocked `curl` → `jb net
loose feat-invoice-pdf --for 45m` → the same `curl` succeeding, all against
one container (unlike `generated/net-switch.txt`, which pairs strict/loose
across the *two* different containers `ls.txt` uses — see that section).
Sources:

- Both `✓ Container 'feat-invoice-pdf' is now on network: <mode>` lines —
  `cli.py _switch()`: `success(f"Container '{short_name(cfg, resolved)}' is
  now on network: {mode}")`, reached from both `net_strict` and `net_loose`.

**`net loose` is shown with `--for 45m`, not bare.** `net_loose` (`cli.py`,
4775-4787): with no `--for`/`--no-revert` given and stdin a TTY,
`LooseAutoRevert.enabled` defaults to `True` (`config.py:642`), so the
command runs `_prompt_loose_ttl()` — an interactive `questionary.select`
menu — *before* `_switch()` prints the success line. A bare `jb net loose
feat-invoice-pdf` in this scene would therefore be showing a command that
behaves differently for a reader who types it than for this recording.
`--for 45m` avoids the menu (same fix as `git.sh`'s `--plain`) and, as a
side effect, is the only place in the clip where the caption's "reverts on
its own after a set time" duration is visible at all — `_switch()`'s own
success line never states one.

**Gap, per the plan's Step 4 instruction:** the plan asked for the `net
loose` confirmation *line* to include "the revert deadline." It doesn't —
`_switch()` prints only the line above, for both modes, with no
TTL/deadline text; `--for 45m` above is visible in the *command*, not in
the confirmation. The auto-revert deadline as a computed time (e.g. "reverts
at 14:32") is surfaced only by `jb net status`'s `_print_loose_status()`
tail (`cli.py`, near line 4929), which — like `net_status_cmd` generally —
shells out to `systemctl`, opens a real SQLite session, and calls
`list_containers()` against a live `Incus()`, so it can't be synthesized
here any more than `generated/net-switch.txt` can (see that section). This
script prints the real confirmation as-is; flagged in `task-5-report.md`
for the maintainer to decide whether to fix the plan's expectation or the
caption.

**Gap: the blocked `curl`'s exact output.** Neither `docs/security.md`,
`docs/troubleshooting.md`, nor the code (`network.py`, near line 129, calls
the mechanism Incus's NIC-level "default-reject") states what curl's own
stderr text or exit code would be when strict mode blocks a request —
reject vs. drop, RST vs. timeout are not established anywhere in this repo.
No error text or exit code is invented for that. What *is* shown is `"000"`
from `curl -s -o /dev/null -w '%{http_code}\n' --max-time 8
https://pypi.org/simple/` (note: `-s`, not `-sS` — `-S` would re-enable
curl's own error text on stderr, which is exactly the unestablished part
this line must not show). This is not a fact derived from curl's manual in
the abstract: **`"000"` is what this exact command has been observed to
print from inside a real strict-mode container** — `--max-time 8` and the
dropped `-S` match that observation, not a plausible-sounding neighbour of
it. The same command against loose mode (full NAT, no ACL) prints `"200"`,
a fact about pypi.org's own live simple index, not a jailbee claim. Using
`-w '%{http_code}'` instead of the plan's literal bare `curl -sS
https://pypi.org/simple/` is a deliberate substitution, flagged in
`task-5-report.md` as a concern for the maintainer, since it changes the
exact command shown (though not what it demonstrates: strict blocks the
request, loose lets it through).

### `git.sh`

Reconstructs `jb git push feat-invoice-pdf --plain` (transport only — no
merge/rebase/reset in the container). `--plain` was chosen over the
config-driven default (`push.default_action` defaults to `"ask"`, which
needs a TTY prompt) to make the scene deterministic. Sources:

- `main (host) ──▶ refs/jailbee/host/main (container)` —
  `cli.py _print_bridge_direction()`:
  `info(f"{src} ({src_side}) ──▶ {dst} ({dst_side})")`.
- `Pushed 'main' (refs/remotes/origin/main) from host into container
  'feat-invoice-pdf' as refs/jailbee/host/main (5f3d914 -> 9c1a7be).` —
  `cli.py _print_push_summary()`. The OIDs are synthetic demo data, same
  category as `generate.py`'s invented `ContainerInfo` rows.
- `✓ Push complete.` — `cli.py _do_single_push()`, plain-action branch:
  `success("Push complete.")`.

**Caption mismatch, found and fixed (not bent to fit):** the page's caption
(`website/index.html`, `#demo-git`) originally showed `jb git push
feat/invoice-pdf` — a branch-style name with a slash. `push`'s positional
argument is a *container* name (`completion.complete_container`), which is
never slash-separated, so the caption taught a command that would fail for
a reader who typed it. This script always typed the real, working form
(`feat-invoice-pdf`, same container as the other three scenes) rather than
matching the caption; the caption itself was corrected in fix round 1 to
`jb git push feat-invoice-pdf` to match. Checked against all other command
strings on the page (the three other demo captions, all seven
install-section commands including the `[gui]` extra) — this was the only
wrong one.

## `generated/ls.txt` — generated

Produced by `website/demo/generate.py`'s `render_ls()`, which calls
`jailbee.table_format.emit()` with `jailbee.lifecycle.ls_field_specs()` —
the exact same rendering path `jailbee ls` uses — over three synthetic
`jailbee.lifecycle.ContainerInfo` rows (no real Incus container, no
network, no filesystem state). `tests/test_website.py`'s
`test_generated_scenes_match_what_jailbees_renderer_produces_today` re-runs
`render_ls()` and asserts it still equals the committed file byte for byte;
if a column is renamed or dropped in `ls_field_specs()`, that test fails
until someone re-runs `uv run python website/demo/generate.py` and commits
the result.

**The fixture rows must obey the same rules a real render does, not just
the same code path.** `state` is exactly `"Running"` / `"Stopped"` —
`lifecycle.list_containers()` copies Incus's `status` string verbatim
(`lifecycle.py`, `state=raw.get("status", "Unknown")`), and that exact
capitalisation feeds two more rules a fixture can silently violate:
`_mem_cell` (`lifecycle.py`, near line 1889) shows `used / limit` only when
`state == "Running"` and `memory_usage` is not `None` — the first row sets
`memory_usage` to demonstrate that; the other running row and the stopped
row leave it unset, so they show the bare `memory_limit`, also a real
outcome. `list_containers()` also only probes git status for a `Running`
container (`lifecycle.py`, `if c.state != "Running": continue`), so the
stopped row's `git_status` is `None`, not a hand-picked "clean" — WT, ↑ and
MERGE render as `—` for it, the same as a real stopped container's row
would.

## `generated/net-switch.txt` — transcribed, not generated

This is **not** a render of `jailbee net status`. `net_status_cmd`
(`src/jailbee/cli.py`, near line 4855) cannot be reproduced from synthetic
data without stubbing real infrastructure:

- it runs a real `subprocess.run(["systemctl", "--user", "is-active",
  "jailbee-net-refresh.timer"], ...)`;
- it opens a real SQLite session through `sqlmodel`'s
  `Session(get_engine())` and queries the `RegisteredRepo`, `RefreshState`,
  and `PoolIP` tables;
- its tail, `_print_loose_status()`, calls `list_containers()` against a
  real `Incus()` instance.

None of that is a `table_format.emit()` render over `FieldSpec` rows the
way `ls` is — it's `typer.echo` lines built directly from live
database/subprocess results. Faking it would mean either stubbing `Incus`
and a database (ruled out — this generator touches neither) or inventing
timer state, repo rows, and pool sizes wholesale, which is exactly the kind
of invented output this whole approach exists to avoid.

So `render_net_switch()` transcribes something else that *is* real and
*is* deterministic: the success message `jailbee net strict`/`jailbee net
loose` print when they finish. That message comes from `_switch()`
(`src/jailbee/cli.py`, line 4710):

```python
success(f"Container '{short_name(cfg, resolved)}' is now on network: {mode}")
```

which renders through `success()` (`src/jailbee/tui.py`, line 26) as:

```python
console.print(f"[green]✓[/green] {msg}")
```

`render_net_switch()` reproduces this literally for the same two
containers `render_ls()` uses (one switching to `strict`, one to `loose`),
so the two scenes tell one consistent story without a live daemon. The
pinning test covers this transcription the same way it covers `ls.txt`: if
either source string changes, the test fails until the file is updated to
match.

**Nothing on the page consumes this file.** `net.sh` (below) prints its own
copies of the same `_switch()` confirmation lines, transcribed and cited
independently, so `#demo-net`'s clip never `cat`s `net-switch.txt` the way
`parallel.sh` `cat`s `ls.txt`. `generated/net-switch.txt` exists purely as a
canary on `_switch()`'s exact string, pinned by the same test as `ls.txt` —
useful for catching a change to that string early, but not something a
visitor ever sees rendered. Keep the file and the test for that reason; just
don't describe it as displayed material.
