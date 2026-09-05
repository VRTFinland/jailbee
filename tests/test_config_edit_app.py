"""A pipe-driven smoke test of the real Application.

Deliberately thin — the interaction model is `state.py`'s and the drawing is
`render.py`'s, both tested directly (every transition `move`, `enter_section`,
`toggle_show_all` and friends can produce is exhaustively covered in
`test_config_edit_state.py`). What is left here, and what nothing else can
cover, is the wiring: that a keypress actually reaches its transition. The
`create_pipe_input` idiom is the one `tests/test_tui.py` already uses for the
forked questionary checkbox; `DummyOutput` proves only that the app runs and
quits, so the navigation tests below swap it for `_CapturingOutput`, which
also records what got painted, and assert on that.
"""

from __future__ import annotations

import pytest
from prompt_toolkit.output import DummyOutput

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


def test_a_save_writes_the_file_and_clears_the_pending_marks(editor):
    """`gpg` is not section index 0 in `repo_specs()` (`container_user` is —
    see `_index_of_section`'s other uses in this file), so the cursor has to
    be walked there with `j` first; a bare `Enter` would open the wrong
    section and stage nothing.
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


def test_space_toggles_a_boolean_and_the_title_counts_it(editor):
    """Enter the gpg section, Space on `enabled`, then save and quit.

    Deferred from task 8 (ruling R3): staging alone was already covered there
    (`test_a_toggle_that_is_not_saved_leaves_the_file_alone`); what only
    becomes testable once `save` exists is that Ctrl-S actually writes the
    toggle to disk. The name is task 8's own — it was never about asserting
    on the title bar's `modified:` counter (which embeds the full tmp_path
    and reliably wraps past `_CapturingOutput`'s 80 columns, dropping the
    digit — checked by hand), so this only asserts what task 8 already
    asserted, plus the save.
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
