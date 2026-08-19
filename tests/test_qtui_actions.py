from pathlib import Path

import pytest

from jailbee.qtui import actions as a
from jailbee.qtui.terminal import TerminalSpec


def test_build_action_non_interactive():
    ac = a.build_action("start", "p-foo", Path("/repo/.gie/config.yaml"))
    assert ac.argv == ["jailbee", "start", "p-foo", "--config", "/repo/.gie/config.yaml"]
    assert ac.launch == "detached"
    assert ac.confirm is False


def test_build_action_shell_is_interactive():
    ac = a.build_action("shell", "p-foo", Path("/repo/.gie/config.yaml"))
    assert ac.launch == "terminal"
    assert ac.confirm is False


def test_build_action_destroy_requires_confirm():
    ac = a.build_action("destroy", "p-foo", Path("/repo/.gie/config.yaml"))
    assert ac.confirm is True
    assert ac.launch == "detached"


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
    assert ac.launch == "detached"
    assert ac.confirm is False


def test_net_loose_without_answers_carries_no_duration_flag():
    """The duration is one of the GUI-collected answers now (see
    ``AppController._collect_answers``), so ``build_action`` itself never
    invents one."""
    ac = a.build_action("net loose", "p-foo", Path("/x/config.yaml"))
    assert "--for" not in ac.argv


def test_net_loose_duration_arrives_as_extra_flags():
    ac = a.build_action("net loose", "p-foo", Path("/x/config.yaml"), extra_flags=["--for", "2h"])
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


def test_launch_mode_classifies_the_verbs():
    """shell/tmux need a real TTY, so they still get a terminal window. The
    printing verbs do not: the GUI shows their output itself, and a terminal
    emulator would close over it. Everything else self-detaches or has nothing
    to say."""
    for verb in ("shell", "tmux"):
        assert a.launch_mode(verb) == "terminal", verb
    for verb in (
        "pr",
        "git push",
        "git push --pr",
        "git pull",
        "git diff",
        "job log",
        "job log --follow",
    ):
        assert a.launch_mode(verb) == "output", verb
    for verb in (
        "ide",
        "chrome",
        "start",
        "stop",
        "restart",
        "destroy",
        "net loose",
        "job clear",
        "pr --open",
    ):
        assert a.launch_mode(verb) == "detached", verb


def test_build_action_sets_the_launch_mode():
    ac = a.build_action("git diff", "alpha-x", Path("/repo/.jailbee/config.yaml"))
    assert ac.launch == "output"
    assert ac.argv == [
        "jailbee",
        "git",
        "diff",
        "alpha-x",
        "--config",
        "/repo/.jailbee/config.yaml",
    ]


def test_resolve_launch_only_wraps_terminal_verbs():
    spec = TerminalSpec(binary="xterm", run_args=["-e"])
    output = a.build_action("git diff", "alpha-x", Path("/c.yaml"))
    interactive = a.build_action("shell", "alpha-x", Path("/c.yaml"))

    assert a.resolve_launch(output, spec) == output.argv  # not wrapped
    assert a.resolve_launch(interactive, spec)[:2] == ["xterm", "-e"]
    # An output verb needs no terminal at all, so a host without one still works
    assert a.resolve_launch(output, None) == output.argv


def test_build_action_appends_the_answers_as_flags():
    ac = a.build_action(
        "git push",
        "alpha-x",
        Path("/repo/.jailbee/config.yaml"),
        extra_flags=["--merge", "--current"],
    )
    assert ac.argv == [
        "jailbee",
        "git",
        "push",
        "alpha-x",
        "--config",
        "/repo/.jailbee/config.yaml",
        "--merge",
        "--current",
    ]


def test_pr_refresh_keeps_its_flag_ahead_of_the_container_name():
    """`--pr` is part of the verb, not an answer: it must stay where Typer
    reads it as an option, with the container name still the sole positional."""
    ac = a.build_action(
        "git push --pr",
        "alpha-x",
        Path("/repo/.jailbee/config.yaml"),
        extra_flags=["--rebase"],
    )
    assert ac.argv == [
        "jailbee",
        "git",
        "push",
        "--pr",
        "alpha-x",
        "--config",
        "/repo/.jailbee/config.yaml",
        "--rebase",
    ]
    assert ac.launch == "output"
    assert ac.confirm is False


def test_git_pull_needs_confirmation_but_no_force():
    """--force is destroy's way of skipping a prompt the detached child cannot
    answer. `jailbee git pull <name>` has no such prompt, and --force there
    means something else entirely."""
    pull = a.build_action("git pull", "alpha-x", Path("/c.yaml"))
    destroy = a.build_action("destroy", "alpha-x", Path("/c.yaml"))

    assert pull.confirm is True
    assert "--force" not in pull.argv
    assert destroy.confirm is True
    assert "--force" in destroy.argv


def test_extra_flags_come_after_the_force_flag():
    """Order matters only for readability, but a flag landing between the verb
    and its container name would be a parse hazard, so pin it."""
    ac = a.build_action("destroy", "alpha-x", Path("/c.yaml"), extra_flags=["--quiet"])
    assert ac.argv[-2:] == ["--force", "--quiet"]
