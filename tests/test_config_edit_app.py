"""A pipe-driven smoke test of the real Application.

Deliberately thin — the interaction model is `state.py`'s and the drawing is
`render.py`'s, both tested directly (every transition `move`, `enter_crumb`,
`toggle_show_all` and friends can produce is exhaustively covered in
`test_config_edit_state.py`). What is left here, and what nothing else can
cover, is the wiring: that a keypress actually reaches its transition. The
`create_pipe_input` idiom is the one `tests/test_tui.py` already uses for the
forked questionary checkbox; `DummyOutput` proves only that the app runs and
quits, so the navigation tests below swap it for `_CapturingOutput`, which
also records what got painted, and assert on that.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from prompt_toolkit.layout.processors import ConditionalProcessor, PasswordProcessor
from prompt_toolkit.output import DummyOutput

from jailbee.config_edit import render
from jailbee.config_edit import state as st
from jailbee.config_edit.layers import read_layers, resolve
from jailbee.config_edit.schema import repo_specs

# A miscounted keystroke sequence in this file doesn't fail an assertion — it
# leaves `run_editor`'s `Application` waiting on a modal (a field prompt, the
# save confirmation, or task 9's dirty-quit warning) with the pipe's input
# exhausted, so the whole pytest process hangs rather than reporting a
# failure. Scoped to this file rather than `[tool.pytest.ini_options]`, which
# would change how every other suite runs. Every test here (call, not
# collection/setup) measured under 0.1s across three full-file runs — 10s
# leaves roughly a 100x margin, generous enough to absorb a slow CI box
# without waiting minutes to notice a real hang.
#
# Default method ("signal", SIGALRM) rather than "thread": verified against
# the worst case this file can produce — a mutation that leaves *no*
# reachable key binding at all (`confirm` set forever, so `~confirming`
# blocks `q` and the sole `y` binding is filtered to never match) — under a
# bare `uv run pytest tests/test_config_edit_app.py -q`, no external
# wrapper. It self-terminated at ~11s with the expected `pytest-timeout`
# failure both as the single selected test and as part of the whole file
# (task 9 report, "Fix round 2/5"), so there was nothing here for the
# asyncio event loop to swallow. Left as "signal" rather than switched to
# "thread" on that evidence — see the report before changing this back.
pytestmark = pytest.mark.timeout(10)


class _CapturingOutput(DummyOutput):
    """A `DummyOutput` that remembers every fragment prompt_toolkit painted.

    Plain `DummyOutput` (the idiom `tests/test_tui.py` uses for the
    questionary checkbox) discards everything, which is exactly wrong for
    proving a keypress reached its transition — `_bindings` could lose every
    navigation entry and a test built on it would not notice, since it only
    checks the exit code. The renderer calls `write`/`write_raw` with the
    actual visible fragment text on every redraw, so appending every call
    gives the same text a real terminal would have shown across the *whole*
    run, not just the final frame — confirmed by hand against a real
    pipe-driven session before relying on it here. Everything else is
    inherited from `DummyOutput` unchanged.
    """

    def __init__(self) -> None:
        self.chunks: list[str] = []

    def write(self, data: str) -> None:
        self.chunks.append(data)

    def write_raw(self, data: str) -> None:
        self.chunks.append(data)

    def screen_text(self) -> str:
        return "".join(self.chunks)


def _index_of_section(specs, name: str) -> int:
    """How many `j` presses from the top of the section list reach `name`."""
    state = st.open_editor(layer="repo", specs=specs, origins={})
    return st.sections(state).index(name)


@pytest.fixture
def editor(tmp_path):
    """Yield `run(keys) -> exit code` against a two-layer fixture on disk."""
    from prompt_toolkit.input import create_pipe_input

    from jailbee.config_edit.app import run_editor

    repo = tmp_path / "repo" / ".jailbee" / "config.yaml"
    repo.parent.mkdir(parents=True)
    repo.write_text("gpg:\n  enabled: false\n")
    glob = tmp_path / "global.yaml"
    glob.write_text("ssh:\n  enabled: true\n")

    with create_pipe_input() as pipe:

        def run(keys: str, *, layer="repo", policy="patch") -> int:
            pipe.send_text(keys)
            layer_set = read_layers(repo, glob)
            specs = repo_specs()
            return run_editor(
                layer=layer,
                layer_set=layer_set,
                specs=specs,
                origins=resolve(specs, layer_set),
                policy=policy,
                input=pipe,
                output=DummyOutput(),
            )

        run.repo = repo
        run.glob = glob
        yield run


@pytest.fixture
def rendered(tmp_path):
    """Yield `run(keys) -> str` — same fixture as `editor`, but returns every
    fragment the renderer painted instead of the exit code.

    This is what makes navigation testable: `editor`'s `DummyOutput` can only
    ever prove the app didn't crash and eventually quit, never that a
    particular keypress reached its transition.
    """
    from prompt_toolkit.input import create_pipe_input

    from jailbee.config_edit.app import run_editor

    repo = tmp_path / "repo" / ".jailbee" / "config.yaml"
    repo.parent.mkdir(parents=True)
    repo.write_text("gpg:\n  enabled: false\n")
    glob = tmp_path / "global.yaml"
    glob.write_text("ssh:\n  enabled: true\n")

    with create_pipe_input() as pipe:

        def run(keys: str) -> str:
            pipe.send_text(keys)
            layer_set = read_layers(repo, glob)
            specs = repo_specs()
            output = _CapturingOutput()
            run_editor(
                layer="repo",
                layer_set=layer_set,
                specs=specs,
                origins=resolve(specs, layer_set),
                policy="patch",
                input=pipe,
                output=output,
            )
            return output.screen_text()

        yield run


def _editor(tmp_path, *, repo=None, global_=None, layer="repo", policy="patch"):
    """Build an `Editor` directly, bypassing `run_editor`'s `Application`.

    `Editor` is a plain dataclass, so tests that want to call its methods one
    at a time and inspect `editor.state`/`editor.message` between calls don't
    need a real terminal or a key-binding loop — that is what the pipe-driven
    `editor`/`rendered` fixtures above are for, and they stay as they are.

    `repo` is written out as `.jailbee/config.yaml` before the layers are
    read, so the real schema (`repo_specs()`) resolves origins against it
    exactly the way `run_editor` would. `global_` is the same for
    `global.yaml`, defaulting to empty — needed by any test that checks how a
    repo-layer collection interacts with entries inherited from the global
    layer (`inherited_entries`, spec's append-not-replace rule for lists).
    """
    import yaml

    from jailbee.config_edit.app import Editor

    repo_path = tmp_path / "repo" / ".jailbee" / "config.yaml"
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    repo_path.write_text(yaml.safe_dump(repo or {}, sort_keys=False))
    global_path = tmp_path / "global.yaml"
    global_path.write_text(yaml.safe_dump(global_ or {}, sort_keys=False))

    layer_set = read_layers(repo_path, global_path)
    specs = repo_specs()
    return Editor(
        layer_set=layer_set,
        state=st.open_editor(layer=layer, specs=specs, origins=resolve(specs, layer_set)),
        policy=policy,
    )


def _descend(editor, *crumbs):
    """Walk `editor.state`'s trail down through `crumbs`, bypassing `enter`.

    For tests that want to land on a particular collection or entry without
    depending on where the cursor happens to sit — `enter` itself, cursor
    position included, is exercised separately.
    """
    for crumb in crumbs:
        editor.state = st.enter_crumb(editor.state, crumb)


def _cursor_to(editor, label):
    """Set `editor.state.index` to the visible row whose `spec.label` matches.

    For tests that want the cursor on a specific field without depending on
    its position among its section's other fields.
    """
    rows = st.visible_specs(editor.state)
    index = next(i for i, spec in enumerate(rows) if spec.label == label)
    editor.state = replace(editor.state, index=index)


def _masking_enabled(area) -> bool:
    """Whether `area`'s widget actually hides its input, not just what it was
    asked to do.

    `_Prompt.password` only mirrors the `password=` `_open_prompt` passed to
    `TextArea`'s constructor — it is bookkeeping, not proof. A test built on
    it alone stays green even if the `password=` argument itself is dropped
    from the `TextArea(...)` call, because nothing then re-derives `password`
    from the widget (confirmed: that exact mutation passed 230/230 before
    this helper existed). `TextArea(password=...)` actually works by putting
    a `ConditionalProcessor(PasswordProcessor, filter=Always()/Never())` into
    `area.control.input_processors` — this walks that list and reads the
    filter directly, the same thing prompt_toolkit's renderer consults on
    every keystroke.
    """
    for proc in area.control.input_processors:
        if isinstance(proc, ConditionalProcessor) and isinstance(proc.processor, PasswordProcessor):
            return bool(proc.filter())
    raise AssertionError("no PasswordProcessor on this TextArea at all")


def test_q_quits_cleanly_and_writes_nothing(editor):
    before = editor.repo.read_text()
    assert editor("q") == 0
    assert editor.repo.read_text() == before


def test_ctrl_c_quits(editor):
    assert editor("\x03") == 0


def test_enter_opens_the_section_the_cursor_is_actually_on(rendered):
    """Entering `ssh` must actually put its cursor on `ssh.enabled` — nothing
    does while the section list has focus, where the field pane only ever
    shows the "pick a section" placeholder.

    Overshoots the target by one `j` and corrects with a `k`, so a broken
    `k` binding lands one section further (`jetbrains`, which immediately
    follows `ssh` in `repo_specs()` and also has a basic `enabled` field)
    and fails this test too, not just a deleted `enter`. Asserting the
    *dotted* path (`ssh.enabled`, as `render.help_pane` shows it) rather
    than the bare label `"enabled"` is what makes that discrimination real:
    both sections' `enabled` rows share the bare label, so a bare-label
    assertion would pass even from the wrong section — confirmed by
    deleting `k` and watching this assertion (only this one, once it reads
    the dotted path) fail.
    """
    specs = repo_specs()
    idx = _index_of_section(specs, "ssh")
    text = rendered(f"{'j' * (idx + 1)}k\rq")
    assert "ssh.enabled" in text


def test_show_all_reveals_an_advanced_field_hidden_by_default(rendered):
    """`ssh.seed_from_host` is `advanced`, so it stays out of the basic view
    even once `ssh` is open — `a` is the only key that can put it on screen.
    """
    specs = repo_specs()
    idx = _index_of_section(specs, "ssh")
    text = rendered(f"{'j' * (idx + 1)}k\raq")
    assert "seed_from_host" in text


def test_escape_leaves_an_open_section_before_quitting(rendered):
    """Enter `ssh`, then escape — the section must actually close before the
    app quits, or its field-pane content lingers into the final frame.

    Piped input arrives all at once (no per-keystroke delay), so
    prompt_toolkit's `Application` coalesces the whole key string into a
    single render right before quitting, after the one startup-only paint —
    confirmed by hand against a real pipe-driven run. That is why this test
    cannot assert on the "pick a section" placeholder's mere *presence*:
    that text is also the very first frame's content regardless of what
    `escape` does, present or absent. What discriminates is whether
    `ssh.enabled` (drawn only once the section is genuinely open) ever
    reaches that final frame: a working `escape` returns to the section
    list before quit, so it is never drawn at all; a deleted `escape`
    leaves the field pane showing it right up to exit.
    """
    specs = repo_specs()
    idx = _index_of_section(specs, "ssh")
    text = rendered(f"{'j' * (idx + 1)}k\r\x1bq")
    assert "ssh.enabled" not in text


def test_the_editor_survives_a_missing_repo_config(tmp_path):
    """`jb config edit` in a repo with no config file opens an empty layer."""
    from prompt_toolkit.input import create_pipe_input

    from jailbee.config_edit.app import run_editor

    repo = tmp_path / "fresh" / ".jailbee" / "config.yaml"
    glob = tmp_path / "global.yaml"
    layer_set = read_layers(repo, glob)
    specs = repo_specs()
    with create_pipe_input() as pipe:
        pipe.send_text("q")
        assert (
            run_editor(
                layer="repo",
                layer_set=layer_set,
                specs=specs,
                origins=resolve(specs, layer_set),
                policy="patch",
                input=pipe,
                output=DummyOutput(),
            )
            == 0
        )
    assert not repo.exists()


def test_a_toggle_that_is_not_saved_leaves_the_file_alone(rendered, tmp_path):
    """Space stages `gpg.enabled`'s flip — drawn as a `-> true` suffix on its
    row — but nothing reaches the file without Ctrl-S; saving is task 9, so
    only the staging half of "toggle" is testable here.

    Ends `qq`, not `q`: task 9's quit binding now warns once when something
    is staged and unsaved (`editor.dirty()`), so the first `q` only shows
    that warning and the second is what actually exits — a lone `q` here
    hangs the pipe rather than failing an assertion (confirmed by hand).
    """
    specs = repo_specs()
    idx = _index_of_section(specs, "gpg")
    repo = tmp_path / "repo" / ".jailbee" / "config.yaml"
    before = repo.read_text()
    text = rendered(f"{'j' * idx}\r qq")
    assert "→ true" in text
    assert repo.read_text() == before


def test_r_stages_a_reset_without_saving(rendered, tmp_path):
    """`gpg.enabled` is a key the repo layer actually holds, so `r` stages a
    delete — `render._staged_suffix` draws that as `-> reset` on the row.
    Saving it (so the key is actually gone from the file) is task 9's Ctrl-S;
    here only the staging half of "reset" is testable, same as the toggle
    test above.

    Ends `qq` for the same reason as the toggle test above: a staged reset
    is still unsaved, so the quit warning eats the first `q`.
    """
    specs = repo_specs()
    idx = _index_of_section(specs, "gpg")
    repo = tmp_path / "repo" / ".jailbee" / "config.yaml"
    before = repo.read_text()
    text = rendered(f"{'j' * idx}\rrqq")
    assert "→ reset" in text
    assert repo.read_text() == before


def test_enter_opens_a_prompt_and_escape_cancels_it(rendered, tmp_path):
    """Enter opens `container_prefix`'s section (first Enter), then the modal
    editor on the field itself (second Enter — a bool field never gets here
    since Enter toggles it directly instead of opening a prompt). Typed text
    stages once committed (Enter again, since it is not a multiline field) —
    visible as a `-> q` marker. Escape throws the same typed text away
    instead of staging it. Neither run saves: that happens only via the
    browsing-mode Ctrl-S task 9 adds.

    The typed character is deliberately `q`, not some other placeholder: `q`
    also quits the app, but only while browsing (`kb.add("q", filter=browsing)`
    in `_bindings`) — with the prompt open, `editing` is true and `browsing`
    is false, so this `q` must land in the text buffer rather than exit the
    app early. Without that gating, this run would quit right after typing
    `q` and never reach the trailing commit/quit keys, so the "-> q" marker
    would never appear — that is what actually exercises the gating, since
    every other prompt test in this file happens to avoid the letter.

    `committed` ends `qq`, not `q`: once the typed value is staged, task 9's
    quit binding warns once before exiting (`editor.dirty()`), so a lone
    trailing `q` here would hang the pipe. `cancelled` needs no such change —
    Escape discards the typed text without staging anything, so quitting
    there is never blocked.
    """
    specs = repo_specs()
    idx = _index_of_section(specs, "container_prefix")
    repo = tmp_path / "repo" / ".jailbee" / "config.yaml"
    before = repo.read_text()

    committed = rendered(f"{'j' * idx}\r\rq\rqq")
    assert "→ q" in committed

    cancelled = rendered(f"{'j' * idx}\r\rq\x1bq")
    assert "→ q" not in cancelled
    assert "●" not in cancelled

    assert repo.read_text() == before


def test_search_finds_a_field_in_another_section(rendered):
    """`/` reaches `ssh.enabled` directly from the section list, with no
    section ever entered by hand — proving the search binding itself works,
    not just that Enter can open a field once already inside a section.
    """
    text = rendered("/ssh.enabled\rq")
    assert "ssh.enabled" in text


def test_a_field_that_cannot_be_edited_here_says_so(rendered, tmp_path):
    """`github` is banned from a repo config; the row explains rather than
    acts. Search for it, Enter on the first hit, quit. No crash, no write.
    """
    repo = tmp_path / "repo" / ".jailbee" / "config.yaml"
    before = repo.read_text()
    text = rendered("/github\r\rq")
    assert "is host-local and is rejected in a repo config" in text
    assert "●" not in text
    assert repo.read_text() == before


def test_a_save_writes_the_toggle_and_leaves_the_rest_of_the_file_alone(editor):
    """`gpg` is not section index 0 in `repo_specs()` (`container_user` is —
    see `_index_of_section`'s other uses in this file), so the cursor has to
    be walked there with `j` first; a bare `Enter` would open the wrong
    section and stage nothing.

    Named for what it checks and no more: this fixture paints into a
    `DummyOutput`, so no assertion here can see a pending mark. The marks are
    `render.field_pane`'s and are covered in `test_config_edit_render.py`.
    """
    specs = repo_specs()
    idx = _index_of_section(specs, "gpg")
    assert editor(f"{'j' * idx}\r \x13q") == 0
    text = editor.repo.read_text()
    assert "enabled: true" in text
    # The patch policy left the rest of the file alone.
    assert text.startswith("gpg:")


def test_a_save_with_nothing_staged_writes_nothing(editor):
    before = editor.repo.read_text()
    assert editor("\x13q") == 0
    assert editor.repo.read_text() == before
    assert not (editor.repo.parent / "config.yaml.bak").exists()


def test_a_save_keeps_a_backup(editor):
    specs = repo_specs()
    idx = _index_of_section(specs, "gpg")
    assert editor(f"{'j' * idx}\r \x13q") == 0
    assert (editor.repo.parent / "config.yaml.bak").read_text() == "gpg:\n  enabled: false\n"


def test_an_invalid_value_is_refused_before_anything_is_written(tmp_path):
    """The real loader rejects a bad container_prefix; nothing reaches disk.

    Navigates by section rather than the brief's `/container_prefix` search:
    `container_prefix`'s own description text — "Defaults to
    `~/.local/share/jailbee/shared/<container_prefix>`" — is quoted verbatim
    inside `shared_dir`'s description too, so that search string matches five
    other fields' descriptions before it ever reaches `container_prefix`
    itself (checked by hand: `shared_dir`, `share_local`, `golden.alias`,
    `jetbrains.share_idea`, `github.api_tokens` all precede it in schema
    order), and the brief's bare `/container_prefix\\r\\r` lands the edit on
    `shared_dir` instead. `container_prefix` is a top-level leaf and so is
    its own one-field section (`state.sections`' docstring), reached the same
    way `test_enter_opens_a_prompt_and_escape_cancels_it` already does.
    """
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from jailbee.config_edit.app import run_editor

    repo = tmp_path / "repo" / ".jailbee" / "config.yaml"
    repo.parent.mkdir(parents=True)
    repo.write_text("container_prefix: fine\n")
    glob = tmp_path / "global.yaml"
    layer_set = read_layers(repo, glob)
    specs = repo_specs()
    idx = _index_of_section(specs, "container_prefix")
    with create_pipe_input() as pipe:
        # Enter the container_prefix section, Enter to edit it, clear the
        # preloaded "fine", type an illegal value, Enter to commit, Ctrl-S to
        # save, quit twice (the rejected edit stays staged, so a lone `q`
        # only hits the unsaved-changes warning and hangs the pipe — the
        # second `q` is what actually exits; confirmed by hand).
        pipe.send_text(f"{'j' * idx}\r\r" + "\x15" + "Not A Prefix" + "\r\x13qq")
        run_editor(
            layer="repo",
            layer_set=layer_set,
            specs=specs,
            origins=resolve(specs, layer_set),
            policy="patch",
            input=pipe,
            output=DummyOutput(),
        )
    assert repo.read_text() == "container_prefix: fine\n"


def test_a_regenerate_over_a_commented_file_needs_a_confirmation(tmp_path):
    """The diff preview is mandatory when hand-written comments would be lost.

    Declining leaves `ssh.enabled`'s toggle still staged (declining a save
    does not discard the edit), so the run needs a second `q`: the first hits
    the new unsaved-changes warning and only the second actually exits —
    confirmed by hand that a single trailing `q` here hangs the pipe rather
    than failing an assertion, since nothing further is queued once the
    warning holds the app open.
    """
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from jailbee.config_edit.app import run_editor
    from jailbee.config_edit.schema import global_specs

    repo = tmp_path / "repo" / ".jailbee" / "config.yaml"
    repo.parent.mkdir(parents=True)
    glob = tmp_path / "global.yaml"
    glob.write_text("# my own note\nssh:\n  enabled: false\n")
    layer_set = read_layers(repo, glob)
    specs = global_specs()

    with create_pipe_input() as pipe:
        # Toggle ssh.enabled, save, answer `n` to the confirmation, quit twice.
        pipe.send_text("/ssh.enabled\r \x13nqq")
        run_editor(
            layer="global",
            layer_set=layer_set,
            specs=specs,
            origins=resolve(specs, layer_set),
            policy="regenerate",
            input=pipe,
            output=DummyOutput(),
        )
    assert "# my own note" in glob.read_text()


def test_y_accepts_the_regenerate_confirmation_and_writes_it(tmp_path):
    """`y` is the write side of the same mandatory confirmation the decline
    test above exercises — the design point of spec 2.5 is that dropping a
    hand-written comment is not silently refused, only gated on an explicit
    yes. Asserting merely "the file changed" would pass even if the wrong
    thing changed; this checks the specific comment `build_plan` reported as
    dropped is actually gone, and the edited value actually landed, so a
    broken `y` binding (or one that fires but writes the wrong plan) is
    caught either way.
    """
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from jailbee.config_edit.app import run_editor
    from jailbee.config_edit.schema import global_specs

    repo = tmp_path / "repo" / ".jailbee" / "config.yaml"
    repo.parent.mkdir(parents=True)
    glob = tmp_path / "global.yaml"
    glob.write_text("# my own note\nssh:\n  enabled: false\n")
    layer_set = read_layers(repo, glob)
    specs = global_specs()

    with create_pipe_input() as pipe:
        # Toggle ssh.enabled, save, answer `y` to the confirmation, quit.
        # A single trailing `q` suffices here (unlike the decline test): a
        # successful save reloads and clears `dirty()`, so the quit warning
        # never fires.
        pipe.send_text("/ssh.enabled\r \x13yq")
        run_editor(
            layer="global",
            layer_set=layer_set,
            specs=specs,
            origins=resolve(specs, layer_set),
            policy="regenerate",
            input=pipe,
            output=DummyOutput(),
        )
    text = glob.read_text()
    assert "# my own note" not in text
    assert "enabled: true" in text


def test_space_toggles_a_boolean_and_ctrl_s_writes_it(editor):
    """Enter the gpg section, Space on `enabled`, then save and quit.

    Deferred from task 8 (ruling R3): staging alone was already covered there
    (`test_a_toggle_that_is_not_saved_leaves_the_file_alone`); what only
    becomes testable once `save` exists is that Ctrl-S actually writes the
    toggle to disk. Task 8's name for this claimed the title bar's `modified:`
    counter too; nothing here asserts on it, and nothing can — the counter
    embeds the full tmp_path and reliably wraps past `_CapturingOutput`'s 80
    columns, dropping the digit (checked by hand). The name now says what is
    actually checked.
    """
    specs = repo_specs()
    idx = _index_of_section(specs, "gpg")
    assert editor(f"{'j' * idx}\r \x13q") == 0
    assert "enabled: true" in editor.repo.read_text()


def test_r_stages_a_reset_of_a_key_the_layer_holds(editor):
    """`gpg.enabled` is in the repo file, so `r` stages a delete, and saving
    actually removes the key from disk.

    Complements (rather than repeats) `test_r_stages_a_reset_without_saving`:
    that one proves the staged `"→ reset"` marker and a byte-identical file
    (Ctrl-S was unbound in task 8); this one proves the save side — the key
    is actually gone once Ctrl-S runs.
    """
    specs = repo_specs()
    idx = _index_of_section(specs, "gpg")
    assert editor(f"{'j' * idx}\rr\x13q") == 0
    assert "enabled" not in editor.repo.read_text()


def test_the_confirmation_prints_the_comment_lines_it_would_drop(tmp_path):
    """The mandatory confirmation shows the at-risk lines, not just a count.

    Spec 2.5 makes this confirmation non-dismissible precisely so hand-written
    text is not lost silently — and the diff pane below the header renders from
    line 0 with no scrolling and no cursor, so on a real `global.yaml` (several
    hundred diff lines) the dropped comments are off screen. The header is the
    only place the user can actually read them.

    Asserted against `_diff_text`'s own fragments rather than a pipe-driven
    run: the diff *itself* quotes every dropped line as a `-` row, so a
    screen-text assertion would pass with the header showing nothing but a
    count. Here the plan carries an empty diff, so the lines can only come
    from the header.
    """
    from jailbee.config_edit.app import Editor, _diff_text
    from jailbee.config_edit.save import SavePlan

    plan = SavePlan(
        path=tmp_path / "global.yaml",
        policy="regenerate",
        old_text="",
        new_text="",
        diff="",
        dropped_comments=("# keep me", "# and me"),
    )
    layer_set = read_layers(tmp_path / "repo.yaml", tmp_path / "global.yaml")
    editor = Editor(
        layer_set=layer_set,
        state=st.open_editor(layer="global", specs=(), origins={}),
        policy="regenerate",
        confirm=plan,
    )
    text = "".join(fragment for _style, fragment in _diff_text(editor)())
    assert "# keep me" in text
    assert "# and me" in text
    assert "[y/N]" in text


def test_the_confirmation_summarises_a_flood_of_dropped_comments(tmp_path):
    """Past `_MAX_DROPPED_SHOWN` the header counts the rest instead of listing
    it — the diff underneath must not be pushed off the pane by its own header.
    """
    from jailbee.config_edit.app import _MAX_DROPPED_SHOWN, Editor, _diff_text
    from jailbee.config_edit.save import SavePlan

    dropped = tuple(f"# note {i}" for i in range(_MAX_DROPPED_SHOWN + 3))
    plan = SavePlan(
        path=tmp_path / "global.yaml",
        policy="regenerate",
        old_text="",
        new_text="",
        diff="",
        dropped_comments=dropped,
    )
    layer_set = read_layers(tmp_path / "repo.yaml", tmp_path / "global.yaml")
    editor = Editor(
        layer_set=layer_set,
        state=st.open_editor(layer="global", specs=(), origins={}),
        policy="regenerate",
        confirm=plan,
    )
    text = "".join(fragment for _style, fragment in _diff_text(editor)())
    assert dropped[0] in text
    assert dropped[-1] not in text
    assert "and 3 more" in text


def test_a_read_only_diff_cannot_be_committed_with_y(editor):
    """`d` opens the plan for reading; `y` must not write it.

    `y`'s binding is filtered on `not editor.diff_open`, which is the whole of
    that guarantee — the confirm modal and the read-only diff share `confirm`,
    so without the filter `d` would become a second, unannounced save key.
    """
    specs = repo_specs()
    idx = _index_of_section(specs, "gpg")
    before = editor.repo.read_text()
    # Enter gpg, Space to stage, `d` to open the diff, `y` (must do nothing),
    # Escape to close it, then two `q` (the edit is still staged and unsaved).
    assert editor(f"{'j' * idx}\r dy\x1bqq") == 0
    assert editor.repo.read_text() == before


def test_a_write_error_is_reported_and_the_session_survives(editor, mocker):
    """An unwritable file must not take the editor down with a traceback.

    `commit` raising `OSError` is ordinary — a read-only file, a root-owned
    `global.yaml`, a full disk. The key handler has no `except` of its own, so
    the guard has to be in `_write`: the run still exits cleanly through `q`,
    and the staged edit is still staged (which is what makes the second `q`
    necessary — the unsaved-changes warning eats the first).
    """
    mocker.patch("jailbee.config_edit.save.commit", side_effect=OSError(13, "Permission denied"))
    specs = repo_specs()
    idx = _index_of_section(specs, "gpg")
    before = editor.repo.read_text()
    assert editor(f"{'j' * idx}\r \x13qq") == 0
    assert editor.repo.read_text() == before


def test_an_unloadable_rendering_is_reported_instead_of_written(editor, mocker):
    """`build_plan`'s YAML guard reaches the message line, not a traceback.

    `layers.validate` checks the staged *mapping* and structurally cannot see a
    rendering defect, so `RenderedYamlError` is the last thing between a broken
    renderer and a file no `load_config` can read. It must behave like every
    other refusal: nothing written, session alive, edit still staged.
    """
    from jailbee.config_edit.save import RenderedYamlError

    mocker.patch(
        "jailbee.config_edit.save.build_plan",
        side_effect=RenderedYamlError("rendered unreadable YAML"),
    )
    specs = repo_specs()
    idx = _index_of_section(specs, "gpg")
    before = editor.repo.read_text()
    assert editor(f"{'j' * idx}\r \x13qq") == 0
    assert editor.repo.read_text() == before


def test_n_closes_a_read_only_diff(rendered):
    """`n` is filtered on `confirming` alone, so it answers the read-only diff
    too — the `[y/N]` prompt trains the user to press it, and `escape` used to
    be the only key that worked there.

    Both runs end with Ctrl-C rather than `q`: piped input is coalesced into a
    single render right before the app quits, so what the capture sees is the
    frame at exit, and `q` is filtered out while a diff is open (which is what
    the unfixed binding would have turned into a hang).
    """
    specs = repo_specs()
    idx = _index_of_section(specs, "gpg")

    still_open = rendered(f"{'j' * idx}\r d\x03")
    assert "Esc to close" in still_open

    closed = rendered(f"{'j' * idx}\r dn\x03")
    assert "Esc to close" not in closed


def test_enter_opens_a_collection_and_then_an_entry(tmp_path):
    """The one `Enter` key descends section list -> collection -> entry.

    Cursor is moved onto `host_mounts` explicitly rather than relying on it
    being the first section: `repo_specs()` is the real `Config` schema, and
    nothing here should depend on its field declaration order.
    """
    editor = _editor(tmp_path, repo={"host_mounts": [{"host": "/a", "container": "/data"}]})
    editor.state = st.move(editor.state, st.sections(editor.state).index("host_mounts"))

    editor.enter()  # section list -> host_mounts (a section of one)
    assert st.screen(editor.state).kind == "collection"

    editor.enter()  # -> entry 0
    assert st.screen(editor.state).kind == "entry"
    assert editor.state.trail == ("host_mounts", 0)


def test_escape_out_of_an_invalid_entry_is_refused_once_then_discards(tmp_path):
    editor = _editor(tmp_path, repo={"host_ports": [{"name": "web"}]})
    _descend(editor, "host_ports", 0)

    editor.back()
    assert st.screen(editor.state).kind == "entry"  # still here
    assert "port" in editor.message

    editor.back()
    assert st.screen(editor.state).kind == "collection"


def test_discarding_an_existing_invalid_entry_drops_its_staged_edits(tmp_path):
    """The second `Esc` does not just move the trail: it also drops whatever
    was staged inside the entry (`discard_under`), not merely leave it
    dangling under an index the collection screen no longer highlights.
    """
    editor = _editor(tmp_path, repo={"host_ports": [{"name": "web"}]})
    _descend(editor, "host_ports", 0)
    editor.state = st.stage(editor.state, ("host_ports", 0, "name"), "typed")

    editor.back()  # first press: still invalid (port is still missing), refused
    editor.back()  # second press: discards the staged edit and leaves

    assert st.screen(editor.state).kind == "collection"
    assert editor.state.staged == {}


def test_escape_out_of_a_valid_entry_leaves_at_the_first_press(tmp_path):
    editor = _editor(tmp_path, repo={"host_ports": [{"name": "web", "port": 8080}]})
    _descend(editor, "host_ports", 0)

    editor.back()

    assert st.screen(editor.state).kind == "collection"
    assert editor.message == ""


def test_the_footer_changes_on_a_collection_screen(tmp_path):
    editor = _editor(tmp_path, repo={"host_mounts": []})
    editor.state = st.move(editor.state, st.sections(editor.state).index("host_mounts"))
    editor.enter()

    text = "".join(t for _, t in render.footer(editor.state))
    assert "n new" in text


def test_a_real_escape_keypress_refuses_to_leave_a_broken_entry(tmp_path):
    """The state-level refuse-once behaviour above is exercised on a bare
    `Editor`, never through a key binding — so it would still pass even if
    `escape` were never wired to `Editor.back` at all. This is the one test
    in the file that presses the real key, through the real `Application`,
    so that hazard cannot slip through.
    """
    from prompt_toolkit.input import create_pipe_input

    from jailbee.config_edit.app import run_editor

    repo = tmp_path / "repo" / ".jailbee" / "config.yaml"
    repo.parent.mkdir(parents=True)
    repo.write_text("host_ports:\n  - name: web\n")
    glob = tmp_path / "global.yaml"
    glob.write_text("")

    specs = repo_specs()
    idx = _index_of_section(specs, "host_ports")

    def run(keys: str) -> str:
        layer_set = read_layers(repo, glob)
        output = _CapturingOutput()
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            run_editor(
                layer="repo",
                layer_set=layer_set,
                specs=specs,
                origins=resolve(specs, layer_set),
                policy="patch",
                input=pipe,
                output=output,
            )
        return output.screen_text()

    # host_ports is itself a section of one, like host_mounts in the tests
    # above: the first Enter lands straight on the collection screen, the
    # second on its only entry.
    once = run(f"{'j' * idx}\r\r\x1bq")
    assert "incomplete" in once

    # A second `Escape` must actually leave to the collection screen — not
    # merely still show the refusal, which a binding that re-clears the
    # message before every call to `back` (the pre-task-7 wiring) would also
    # produce, forever. The collection screen's own footer (`n new`, `x
    # delete`) is what proves it, since `back`'s own message-based bookkeeping
    # can't tell the two apart from the outside.
    twice = run(f"{'j' * idx}\r\r\x1b\x1bq")
    assert "x delete" in twice


def test_typing_into_an_entry_cancels_the_collection_s_pending_reset(tmp_path):
    """The rule `stage`'s own docstring names but cannot announce itself
    ("Whether to *say* so belongs to `app.py`; this module is pure"): editing
    a field inside a collection that has a pending reset staged cancels that
    reset. `state.py`'s side of it is proven pure and correct directly in
    `test_config_edit_state.py`; this is the one place the message that
    reaches the screen is proven to follow it.
    """
    editor = _editor(tmp_path, repo={"host_mounts": [{"host": "/a", "container": "/data"}]})
    editor.state = st.set_query(editor.state, "host_mounts")
    rows = st.visible_specs(editor.state)
    editor.state = st.move(
        editor.state, next(i for i, s in enumerate(rows) if s.path == ("host_mounts",))
    )
    editor.reset()
    assert editor.state.staged == {("host_mounts",): st.UNSET}

    editor.state = st.set_query(editor.state, "")
    _descend(editor, "host_mounts", 0)
    editor.edit_current()
    assert editor.prompt is not None
    editor.prompt.area.text = "/typed"
    editor.commit_prompt()

    assert "cancels the pending reset of host_mounts" in editor.message


def test_enter_on_a_boolean_field_also_cancels_the_collection_s_pending_reset(tmp_path):
    """`Enter` reaches a bool field through `edit_current`'s own `BOOL`
    branch, not through `toggle`'s `Space` binding — both must say the same
    thing when they cancel a pending reset, not just the one bound to `Space`.
    """
    editor = _editor(tmp_path, repo={"host_mounts": [{"host": "/a", "container": "/data"}]})
    editor.state = st.set_query(editor.state, "host_mounts")
    rows = st.visible_specs(editor.state)
    editor.state = st.move(
        editor.state, next(i for i, s in enumerate(rows) if s.path == ("host_mounts",))
    )
    editor.reset()
    assert editor.state.staged == {("host_mounts",): st.UNSET}

    editor.state = st.set_query(editor.state, "")
    _descend(editor, "host_mounts", 0)
    rows = st.visible_specs(editor.state)
    editor.state = st.move(
        editor.state, next(i for i, s in enumerate(rows) if s.label == "readonly")
    )

    editor.edit_current()  # a bool field toggles directly, no prompt opens

    assert editor.prompt is None
    assert "cancels the pending reset of host_mounts" in editor.message


def test_resetting_a_collection_discards_pending_edits_inside_it_and_says_so(tmp_path):
    """The other half of the same rule: resetting a collection that has
    pending edits inside it discards them.
    """
    editor = _editor(tmp_path, repo={"host_mounts": [{"host": "/a", "container": "/data"}]})
    _descend(editor, "host_mounts", 0)
    editor.edit_current()
    assert editor.prompt is not None
    editor.prompt.area.text = "/typed"
    editor.commit_prompt()
    assert editor.state.staged == {("host_mounts", 0, "host"): "/typed"}

    editor.state = st.set_query(editor.state, "host_mounts")
    rows = st.visible_specs(editor.state)
    editor.state = st.move(
        editor.state, next(i for i, s in enumerate(rows) if s.path == ("host_mounts",))
    )

    editor.reset()

    assert "Discarded pending edits inside host_mounts" in editor.message
    assert editor.state.staged == {("host_mounts",): st.UNSET}


def test_n_adds_an_entry_and_opens_its_form(tmp_path):
    editor = _editor(tmp_path, repo={"host_mounts": []})
    _descend(editor, "host_mounts")

    editor.new_entry_here()

    assert st.screen(editor.state).kind == "entry"
    assert editor.state.trail == ("host_mounts", 0)
    assert editor.new_entry == ("host_mounts", 0)


def test_n_on_a_map_asks_for_the_key_first(tmp_path):
    editor = _editor(tmp_path, repo={"agents": {}})
    _descend(editor, "agents")

    editor.new_entry_here()

    assert editor.prompt is not None
    assert st.screen(editor.state).kind == "collection"  # not yet created
    editor.prompt.area.text = "codex"
    editor.commit_prompt()
    assert editor.state.trail == ("agents", "codex")


def test_n_leaves_an_invalid_new_entry_removed_not_merely_discarded(tmp_path):
    """`n` then an invalid entry then two `Esc`: the entry `n` created this
    session must be removed outright, not merely have its (nonexistent)
    staged edits dropped and the entry left half-made. Until this task wired
    `n`, `new_entry` was always `None` and `_discard_entry`'s "remove it"
    branch had never run.
    """
    editor = _editor(tmp_path, repo={"host_ports": []})
    _descend(editor, "host_ports")

    editor.new_entry_here()
    assert st.screen(editor.state).kind == "entry"
    assert editor.new_entry == ("host_ports", 0)

    editor.back()  # first Esc: entry is invalid (no name/port), refused
    assert st.screen(editor.state).kind == "entry"

    editor.back()  # second Esc: discards it outright
    assert st.screen(editor.state).kind == "collection"
    assert editor.new_entry is None
    assert editor.state.staged[("host_ports",)] == []


def test_x_deletes_the_entry_under_the_cursor(tmp_path):
    editor = _editor(tmp_path, repo={"host_mounts": [{"host": "/a"}, {"host": "/b"}]})
    _descend(editor, "host_mounts")

    editor.delete_entry_here()

    assert editor.state.staged[("host_mounts",)] == [{"host": "/b"}]


def test_x_refuses_an_inherited_entry(tmp_path):
    """A repo list appends to the global one; the global entries are not ours."""
    editor = _editor(
        tmp_path,
        repo={"host_mounts": [{"host": "/mine"}]},
        global_={"host_mounts": [{"host": "/theirs"}]},
    )
    _descend(editor, "host_mounts")

    editor.delete_entry_here()

    assert editor.state.staged[("host_mounts",)] == []  # only /mine was ours


def test_x_re_clamps_the_cursor_past_the_new_end(tmp_path):
    """Deleting the last entry must move the cursor back onto the new last
    one rather than leaving it pointing past the end of the shorter list.
    """
    editor = _editor(
        tmp_path, repo={"host_mounts": [{"host": "/a"}, {"host": "/b"}, {"host": "/c"}]}
    )
    _descend(editor, "host_mounts")
    editor.state = st.move(editor.state, 2)  # cursor on the last entry

    editor.delete_entry_here()

    assert editor.state.staged[("host_mounts",)] == [{"host": "/a"}, {"host": "/b"}]
    assert editor.state.index == 1  # clamped onto the new last entry


def test_x_does_nothing_on_a_field_screen(tmp_path):
    editor = _editor(tmp_path, repo={})
    _descend(editor, "defaults")

    editor.delete_entry_here()

    assert "collection" in editor.message.casefold()


def test_shift_j_moves_an_entry_down(tmp_path):
    editor = _editor(tmp_path, repo={"host_mounts": [{"host": "/a"}, {"host": "/b"}]})
    _descend(editor, "host_mounts")

    editor.move_entry_here(1)

    assert editor.state.staged[("host_mounts",)] == [{"host": "/b"}, {"host": "/a"}]
    assert editor.state.index == 1  # the cursor follows the entry it moved


def test_a_rejected_move_past_the_end_does_not_move_the_cursor(tmp_path):
    """`move_entry` is a no-op past either end and stages nothing then, so
    the cursor must not move either — even though the collection is already
    staged from the first, successful `J` a moment earlier. That is exactly
    the case that trips up a predicate based on `staged.get(spec.path) is
    not None` rather than whether *this particular call* changed anything:
    the collection was already staged going in, so such a predicate would
    read the second, rejected move as having moved and drag the cursor one
    past the last entry.
    """
    editor = _editor(tmp_path, repo={"host_mounts": [{"host": "/a"}, {"host": "/b"}]})
    _descend(editor, "host_mounts")

    editor.move_entry_here(1)  # stages the collection; cursor now on index 1
    assert editor.state.index == 1

    editor.move_entry_here(1)  # index 1 is the last entry: a second J is a no-op

    assert editor.state.index == 1  # unchanged — the second move was rejected
    assert editor.state.staged[("host_mounts",)] == [{"host": "/b"}, {"host": "/a"}]


def test_j_on_a_map_says_it_has_no_order(tmp_path):
    editor = _editor(tmp_path, repo={"agents": {"codex": {}}})
    _descend(editor, "agents")

    editor.move_entry_here(1)

    assert "no order" in editor.message


def test_the_collection_keys_do_nothing_on_a_field_screen(tmp_path):
    editor = _editor(tmp_path, repo={})
    _descend(editor, "defaults")

    editor.new_entry_here()

    assert editor.prompt is None
    assert "collection" in editor.message.casefold()


def test_setting_a_token_uses_a_hidden_input_and_stages_the_whole_map(tmp_path):
    editor = _editor(
        tmp_path, global_={"github": {"api_tokens": {"gisgro": "ghp_old"}}}, layer="global"
    )
    _descend(editor, "github", "api_tokens")

    editor.enter()

    assert editor.prompt is not None
    assert editor.prompt.password is True
    assert _masking_enabled(editor.prompt.area)  # the widget itself, not just the bookkeeping
    # Never seeded with the stored token (spec 3.4): the prompt opens empty,
    # and the token appears nowhere on it — text or label.
    assert editor.prompt.area.text == ""
    assert "ghp_old" not in editor.prompt.area.text
    assert "ghp_old" not in editor.prompt.label
    editor.prompt.area.text = "ghp_new"
    editor.commit_prompt()

    assert editor.state.staged[("github", "api_tokens")] == {"gisgro": "ghp_new"}


def test_a_staged_token_is_masked_in_the_diff(tmp_path):
    """The whole point of Task 1, asserted end to end."""
    editor = _editor(
        tmp_path, global_={"github": {"api_tokens": {"gisgro": "ghp_old"}}}, layer="global"
    )
    _descend(editor, "github", "api_tokens")
    editor.enter()
    editor.prompt.area.text = "ghp_brandnew"
    editor.commit_prompt()

    editor.show_diff()

    assert editor.confirm is not None
    assert "ghp_brandnew" not in editor.confirm.diff
    assert "ghp_old" not in editor.confirm.diff


def test_the_prompt_label_names_the_key_but_never_the_value(tmp_path):
    """`_Prompt.label` is painted on every redraw while the prompt is open —
    the exact kind of place the one rule (never paint a token) has to hold.
    """
    editor = _editor(
        tmp_path, global_={"github": {"api_tokens": {"gisgro": "ghp_old"}}}, layer="global"
    )
    _descend(editor, "github", "api_tokens")

    editor.enter()

    assert editor.prompt is not None
    assert "github.api_tokens.gisgro" in editor.prompt.label
    assert "ghp_old" not in editor.prompt.label


def test_an_empty_token_refuses_and_says_so(tmp_path):
    editor = _editor(
        tmp_path, global_={"github": {"api_tokens": {"gisgro": "ghp_old"}}}, layer="global"
    )
    _descend(editor, "github", "api_tokens")
    editor.enter()

    editor.prompt.area.text = "   "
    editor.commit_prompt()

    assert editor.prompt is not None  # kept open, nothing staged
    assert "token is required" in editor.message.casefold()
    assert ("github", "api_tokens") not in editor.state.staged


def test_n_on_a_secret_map_asks_for_the_key_then_hides_the_value(tmp_path):
    editor = _editor(
        tmp_path, global_={"github": {"api_tokens": {"gisgro": "ghp_old"}}}, layer="global"
    )
    _descend(editor, "github", "api_tokens")

    editor.new_entry_here()
    assert editor.prompt is not None
    assert editor.prompt.password is False  # this prompt names the key, not a token
    assert not _masking_enabled(editor.prompt.area)
    editor.prompt.area.text = "personal"
    editor.commit_prompt()

    # The key exists (with a placeholder), and a *second*, hidden prompt is
    # now open on its value rather than any entry form.
    assert editor.state.staged[("github", "api_tokens")] == {"gisgro": "ghp_old", "personal": ""}
    assert editor.prompt is not None
    assert editor.prompt.password is True
    assert _masking_enabled(editor.prompt.area)
    assert editor.prompt.secret_key == "personal"
    assert editor.prompt.area.text == ""
    assert "ghp_old" not in editor.prompt.area.text
    assert "ghp_old" not in editor.prompt.label

    editor.prompt.area.text = "ghp_brandnew"
    editor.commit_prompt()

    assert editor.state.staged[("github", "api_tokens")] == {
        "gisgro": "ghp_old",
        "personal": "ghp_brandnew",
    }


def test_esc_on_a_freshly_created_secret_entry_removes_the_empty_placeholder(tmp_path):
    """`n` on a secret map stages `{key: ""}` before the value is even typed
    (there is no form to hold it meanwhile) — abandoning the value prompt
    must not leave that placeholder behind as if it were a real, empty
    token.
    """
    editor = _editor(
        tmp_path, global_={"github": {"api_tokens": {"gisgro": "ghp_old"}}}, layer="global"
    )
    _descend(editor, "github", "api_tokens")
    editor.new_entry_here()
    editor.prompt.area.text = "personal"
    editor.commit_prompt()
    assert editor.state.staged[("github", "api_tokens")] == {"gisgro": "ghp_old", "personal": ""}

    editor.cancel_prompt()

    assert editor.prompt is None
    assert editor.state.staged[("github", "api_tokens")] == {"gisgro": "ghp_old"}


def test_esc_on_a_freshly_created_secret_entry_in_a_previously_absent_map_stages_nothing(
    tmp_path,
):
    """The stronger case: when `github.api_tokens` did not exist on this
    layer's file at all, cancelling the only entry `n` just created must
    leave *nothing* staged — not `{}`. A staged `{}` still writes an empty
    `api_tokens: {}` on save, which is not "as if `n` had never been
    pressed" for a layer that had nothing to begin with.
    """
    editor = _editor(tmp_path, global_={}, layer="global")
    _descend(editor, "github", "api_tokens")
    editor.new_entry_here()
    editor.prompt.area.text = "gisgro"
    editor.commit_prompt()
    assert editor.state.staged[("github", "api_tokens")] == {"gisgro": ""}

    editor.cancel_prompt()

    assert editor.prompt is None
    assert ("github", "api_tokens") not in editor.state.staged
    assert not editor.dirty()


def test_esc_on_an_existing_secret_entry_leaves_it_untouched(tmp_path):
    """The removal above is scoped to the entry `n` just created — cancelling
    a prompt opened on an *existing* key must not delete that key."""
    editor = _editor(
        tmp_path, global_={"github": {"api_tokens": {"gisgro": "ghp_old"}}}, layer="global"
    )
    _descend(editor, "github", "api_tokens")
    editor.enter()  # opens the hidden prompt on the existing "gisgro" key

    editor.cancel_prompt()

    assert editor.prompt is None
    assert ("github", "api_tokens") not in editor.state.staged


def test_edit_current_refuses_a_secret_map_directly_rather_than_leaking_it(tmp_path):
    """`enter()` is the only route the shipped UI takes to a drill-down row,
    and it never calls `edit_current` for one — but `edit_current` is public
    and takes nothing from `enter()` about how it got called, so it must
    refuse a secret map on its own rather than trust that invariant. Without
    its own `is_drilldown` guard, `edit_block` (which now lets a secret map
    through) plus `spec.kind in _MAP_KINDS` matching `STR_MAP` would hand
    `values.map_to_text` — every token in the map — to a plain multiline
    prompt. Found by direct call during this task's leak audit.
    """
    editor = _editor(
        tmp_path, global_={"github": {"api_tokens": {"gisgro": "ghp_REALSECRET"}}}, layer="global"
    )
    editor.state = st.toggle_show_all(st.enter_crumb(editor.state, "github"))
    rows = st.visible_specs(editor.state)
    editor.state = st.move(
        editor.state, next(i for i, s in enumerate(rows) if s.label == "api_tokens")
    )

    editor.edit_current()

    assert editor.prompt is None
    assert "not editable here" in editor.message.casefold()


def test_n_x_j_k_are_wired_through_the_real_application(tmp_path):
    """Presses the real keys through a real `Application`, so a binding that
    is never wired cannot pass its unit test — `_editor` calls `Editor`'s
    methods directly and would not catch that.

    `host_mounts` is a section of one, so the first `Enter` lands straight on
    its collection screen. `n` appends an entry and opens its form; typing
    `host`/`container` and Enter/Tab-Enter fills the two required fields (the
    entry form validates on the way out, so it must be complete before the
    Escape that leaves it counts as valid). Back on the collection screen with
    one real entry, `n` again appends a second, which is then deleted with
    `x`; `J`/`K` are exercised on the two entries that remain from the first
    fixture-seeded row plus the one just added.
    """
    from prompt_toolkit.input import create_pipe_input

    from jailbee.config_edit.app import run_editor

    repo = tmp_path / "repo" / ".jailbee" / "config.yaml"
    repo.parent.mkdir(parents=True)
    repo.write_text(
        "host_mounts:\n  - host: /a\n    container: /data-a\n  - host: /b\n    container: /data-b\n"
    )
    glob = tmp_path / "global.yaml"
    glob.write_text("")

    specs = repo_specs()
    idx = _index_of_section(specs, "host_mounts")

    def run(keys: str) -> str:
        layer_set = read_layers(repo, glob)
        output = _CapturingOutput()
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            run_editor(
                layer="repo",
                layer_set=layer_set,
                specs=specs,
                origins=resolve(specs, layer_set),
                policy="patch",
                input=pipe,
                output=output,
            )
        return output.screen_text()

    # host_mounts collection screen (two entries), n adds a third and opens
    # it, escape leaves the (incomplete) entry, escape again discards it —
    # back on the collection screen with the original two (/a, /b; cursor on
    # /a at index 0). J swaps /a down to index 1 and the cursor follows it
    # there; x then deletes the entry under the cursor — /a, now at index 1 —
    # leaving only /b.
    # Ends `qq`, not `q`: the delete leaves an unsaved edit staged, so the
    # first `q` only hits the unsaved-changes warning and the second is what
    # actually exits — a lone `q` here hangs the pipe rather than failing.
    text = run(f"{'j' * idx}\r" + "n\x1b\x1b" + "J" + "x" + "qq")
    assert "/b" in text
    assert "/a" not in text
    assert "J/K move" in text  # the footer of a live collection screen


def test_editing_scratch_config_stages_the_parsed_mapping(tmp_path):
    """`scratch.config` (`FieldKind.OPAQUE`) is global-only — absent from
    `repo_specs()` entirely, not merely disabled there — so this cannot use
    the shared `_editor` helper, which always builds its state from
    `repo_specs()`. Built by hand instead, the same way `_editor` itself does,
    but with `global_specs()`.
    """
    import yaml

    from jailbee.config_edit.app import Editor
    from jailbee.config_edit.schema import global_specs

    repo_path = tmp_path / "repo" / ".jailbee" / "config.yaml"
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    repo_path.write_text(yaml.safe_dump({}, sort_keys=False))
    global_path = tmp_path / "global.yaml"
    global_path.write_text(
        yaml.safe_dump({"scratch": {"config": {"memory": "4GiB"}}}, sort_keys=False)
    )

    layer_set = read_layers(repo_path, global_path)
    specs = global_specs()
    editor = Editor(
        layer_set=layer_set,
        state=st.open_editor(layer="global", specs=specs, origins=resolve(specs, layer_set)),
        policy="patch",
    )
    editor.state = st.toggle_show_all(editor.state)  # scratch.config is advanced
    _descend(editor, "scratch")
    _cursor_to(editor, "config")

    editor.edit_current()
    assert editor.prompt is not None
    assert editor.prompt.multiline is True
    editor.prompt.area.text = "memory: 8GiB\n"
    editor.commit_prompt()

    assert editor.state.staged[("scratch", "config")] == {"memory": "8GiB"}


def test_editing_scratch_config_keeps_the_prompt_open_on_a_parse_error(tmp_path):
    """A parse failure must not silently drop what was typed."""
    import yaml

    from jailbee.config_edit.app import Editor
    from jailbee.config_edit.schema import global_specs

    repo_path = tmp_path / "repo" / ".jailbee" / "config.yaml"
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    repo_path.write_text(yaml.safe_dump({}, sort_keys=False))
    global_path = tmp_path / "global.yaml"
    global_path.write_text(
        yaml.safe_dump({"scratch": {"config": {"memory": "4GiB"}}}, sort_keys=False)
    )

    layer_set = read_layers(repo_path, global_path)
    specs = global_specs()
    editor = Editor(
        layer_set=layer_set,
        state=st.open_editor(layer="global", specs=specs, origins=resolve(specs, layer_set)),
        policy="patch",
    )
    editor.state = st.toggle_show_all(editor.state)  # scratch.config is advanced
    _descend(editor, "scratch")
    _cursor_to(editor, "config")

    editor.edit_current()
    editor.prompt.area.text = "just a string\n"
    editor.commit_prompt()

    assert editor.prompt is not None
    assert "mapping" in editor.message
    assert editor.state.staged == {}
