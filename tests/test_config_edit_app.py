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
    """Entering `ssh` (its one basic field is `enabled`) must actually put
    that field on screen — nothing does while the section list has focus,
    where the field pane only ever shows the "pick a section" placeholder.

    Overshoots the target by one `j` and corrects with a `k`, so a deleted
    `k` binding lands on the wrong section (whose fields do not say
    "enabled") and fails this test too, not just a deleted `up`.
    """
    specs = repo_specs()
    idx = _index_of_section(specs, "ssh")
    text = rendered(f"{'j' * (idx + 1)}k\rq")
    assert "enabled" in text


def test_show_all_reveals_an_advanced_field_hidden_by_default(rendered):
    """`ssh.seed_from_host` is `advanced`, so it stays out of the basic view
    even once `ssh` is open — `a` is the only key that can put it on screen.
    """
    specs = repo_specs()
    idx = _index_of_section(specs, "ssh")
    text = rendered(f"{'j' * (idx + 1)}k\raq")
    assert "seed_from_host" in text


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
