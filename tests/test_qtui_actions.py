from pathlib import Path

import pytest

from jailbee.qtui import actions as a
from jailbee.qtui.terminal import TerminalSpec


def test_build_action_non_interactive():
    ac = a.build_action("start", "p-foo", Path("/repo/.gie/config.yaml"))
    assert ac.argv == ["jailbee", "start", "p-foo", "--config", "/repo/.gie/config.yaml"]
    assert ac.interactive is False
    assert ac.confirm is False


def test_build_action_shell_is_interactive():
    ac = a.build_action("shell", "p-foo", Path("/repo/.gie/config.yaml"))
    assert ac.interactive is True
    assert ac.confirm is False


def test_build_action_destroy_requires_confirm():
    ac = a.build_action("destroy", "p-foo", Path("/repo/.gie/config.yaml"))
    assert ac.confirm is True
    assert ac.interactive is False


def test_build_action_destroy_passes_force():
    """The GUI confirms via its own dialog before dispatching, so the CLI
    must run non-interactively with ``--force``. Without it, ``gie destroy``
    calls ``typer.confirm`` on a stdin the detached Popen child can't answer,
    aborting the destroy silently."""
    ac = a.build_action("destroy", "p-foo", Path("/repo/.gie/config.yaml"))
    assert ac.argv == [
        "jailbee",
        "destroy",
        "p-foo",
        "--config",
        "/repo/.gie/config.yaml",
        "--force",
    ]


def test_build_action_attach_verbs_pass_force_without_confirming():
    """The attach verbs skip the CLI's "continue anyway?" question — the card
    the operator clicked already showed the failed job — but they are not
    confirm verbs: no Qt dialog stands between the click and the launch.

    `ide`/`chrome` need this as much as the interactive two: their detached
    child inherits the GUI's stdin, which is a TTY whenever the dashboard was
    started from a terminal, so the prompt would hang unseen.
    """
    for verb in ("shell", "tmux", "ide", "chrome"):
        ac = a.build_action(verb, "p-foo", Path("/repo/.gie/config.yaml"))
        assert ac.confirm is False
        assert ac.argv == [
            "jailbee",
            verb,
            "p-foo",
            "--config",
            "/repo/.gie/config.yaml",
            "--force",
        ]


def test_resolve_launch_non_interactive_returns_argv_unchanged():
    ac = a.build_action("stop", "p-foo", Path("/x/config.yaml"))
    assert a.resolve_launch(ac, None) == ac.argv


def test_resolve_launch_interactive_wraps_in_terminal():
    ac = a.build_action("shell", "p-foo", Path("/x/config.yaml"))
    spec = TerminalSpec(binary="xterm", run_args=["-e"])
    assert a.resolve_launch(ac, spec) == ["xterm", "-e", *ac.argv]


def test_resolve_launch_interactive_without_terminal_raises():
    ac = a.build_action("tmux", "p-foo", Path("/x/config.yaml"))
    with pytest.raises(a.TerminalNotFoundError):
        a.resolve_launch(ac, None)


def test_build_action_multi_token_verb_splits_into_argv():
    ac = a.build_action("net loose", "p-foo", Path("/x/config.yaml"))
    assert ac.argv == ["jailbee", "net", "loose", "p-foo", "--config", "/x/config.yaml"]
    assert ac.interactive is False
    assert ac.confirm is False


def test_net_loose_requests_a_duration_prompt():
    ac = a.build_action("net loose", "p-foo", Path("/x/config.yaml"))
    assert ac.duration_prompt is True
    assert "--for" not in ac.argv


def test_net_loose_with_duration_appends_for_and_stops_prompting():
    ac = a.build_action("net loose", "p-foo", Path("/x/config.yaml"), duration="2h")
    assert ac.argv == [
        "jailbee",
        "net",
        "loose",
        "p-foo",
        "--config",
        "/x/config.yaml",
        "--for",
        "2h",
    ]
    assert ac.duration_prompt is False


def test_other_verbs_do_not_request_a_duration():
    for verb in ("start", "stop", "net strict", "destroy", "shell"):
        assert a.build_action(verb, "p-foo", Path("/x/config.yaml")).duration_prompt is False
