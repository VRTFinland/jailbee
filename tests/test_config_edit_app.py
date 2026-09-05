"""A pipe-driven smoke test of the real Application.

Deliberately thin — the interaction model is `state.py`'s and the drawing is
`render.py`'s, both tested directly. What is left here is the wiring: that keys
reach transitions, that the app exits, and that nothing is written unless a
save was asked for. The `create_pipe_input` + `DummyOutput` idiom is the one
`tests/test_tui.py` already uses for the forked questionary checkbox.
"""

from __future__ import annotations

import pytest

from jailbee.config_edit.layers import read_layers, resolve
from jailbee.config_edit.schema import repo_specs


@pytest.fixture
def editor(tmp_path):
    """Yield `run(keys) -> exit code` against a two-layer fixture on disk."""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

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


def test_q_quits_cleanly_and_writes_nothing(editor):
    before = editor.repo.read_text()
    assert editor("q") == 0
    assert editor.repo.read_text() == before


def test_ctrl_c_quits(editor):
    assert editor("\x03") == 0


def test_navigation_does_not_crash(editor):
    # down, down, up, enter a section, escape, show-all, quit
    assert editor("jjk\r\x1b" "a" "q") == 0


def test_the_editor_survives_a_missing_repo_config(tmp_path):
    """`jb config edit` in a repo with no config file opens an empty layer."""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

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
