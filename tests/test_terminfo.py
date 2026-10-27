"""Tests for making the host's terminal usable inside a container."""

import base64
from unittest.mock import MagicMock

from jailbee import terminfo
from jailbee.incus import IncusError

ENTRY = "xterm-fancy|Fancy terminal,\n\tam, colors#256,\n"


def _prepare(incus, environ, mocker, entry=ENTRY):
    mocker.patch.object(terminfo, "_host_entry", return_value=entry)
    return terminfo.terminal_env(incus, "c1", uid=1000, gid=1000, home="/home/dev", environ=environ)


def test_unset_or_dumb_term_touches_nothing(mocker):
    incus = MagicMock()
    for environ in ({}, {"TERM": ""}, {"TERM": "dumb"}):
        assert _prepare(incus, environ, mocker) == {}
    incus.exec.assert_not_called()


def test_baseline_terms_are_not_probed(mocker):
    """xterm-256color and friends ship with every image: no `incus exec`."""
    incus = MagicMock()
    for term in ("xterm-256color", "xterm", "screen-256color", "tmux-256color", "linux"):
        assert _prepare(incus, {"TERM": term}, mocker) == {}
    incus.exec.assert_not_called()


def test_known_term_is_left_alone(mocker):
    incus = MagicMock()
    assert _prepare(incus, {"TERM": "xterm-fancy"}, mocker) == {}
    # Only the probe ran, as the container user, so its ~/.terminfo counts.
    assert incus.exec.call_count == 1
    args, kwargs = incus.exec.call_args
    assert args[1] == ["infocmp", "xterm-fancy"]
    assert kwargs["uid"] == 1000
    assert kwargs["env"] == {"HOME": "/home/dev"}


def test_unknown_term_is_compiled_into_the_users_terminfo(mocker):
    incus = MagicMock()
    incus.exec.side_effect = [IncusError("no entry"), ""]
    assert _prepare(incus, {"TERM": "xterm-fancy"}, mocker) == {}
    push = incus.exec.call_args_list[1]
    script = push.args[1][-1]
    assert base64.b64encode(ENTRY.encode()).decode() in script
    assert "tic -x" in script
    assert '"$HOME/.terminfo"' in script
    assert push.kwargs["uid"] == 1000
    assert push.kwargs["env"] == {"HOME": "/home/dev"}


def test_host_without_the_entry_falls_back_to_xterm_256color(mocker):
    incus = MagicMock()
    incus.exec.side_effect = IncusError("no entry")
    assert _prepare(incus, {"TERM": "xterm-fancy"}, mocker, entry=None) == {
        "TERM": "xterm-256color"
    }
    assert incus.exec.call_count == 1  # nothing to push


def test_failed_compile_falls_back_to_xterm_256color(mocker):
    incus = MagicMock()
    incus.exec.side_effect = [IncusError("no entry"), IncusError("tic failed")]
    assert _prepare(incus, {"TERM": "xterm-fancy"}, mocker) == {"TERM": "xterm-256color"}


def test_colour_hints_follow_the_terminal(mocker):
    incus = MagicMock()
    environ = {
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
        "TERM_PROGRAM": "ghostty",
        "TERM_PROGRAM_VERSION": "1.2.0",
        "UNRELATED": "x",
    }
    assert _prepare(incus, environ, mocker) == {
        "COLORTERM": "truecolor",
        "TERM_PROGRAM": "ghostty",
        "TERM_PROGRAM_VERSION": "1.2.0",
    }


def test_host_entry_reads_infocmp(mocker):
    run = mocker.patch(
        "jailbee.terminfo.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="entry\n"),
    )
    assert terminfo._host_entry("xterm-fancy") == "entry\n"
    assert run.call_args.args[0] == ["infocmp", "-x", "xterm-fancy"]


def test_host_entry_is_none_when_infocmp_is_missing_or_fails(mocker):
    mocker.patch("jailbee.terminfo.subprocess.run", side_effect=FileNotFoundError)
    assert terminfo._host_entry("xterm-fancy") is None
    mocker.patch(
        "jailbee.terminfo.subprocess.run",
        return_value=MagicMock(returncode=1, stdout=""),
    )
    assert terminfo._host_entry("xterm-fancy") is None
