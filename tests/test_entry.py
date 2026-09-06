"""Tests for the console entry point's top-level error handling."""

from __future__ import annotations

import sys

import pytest

from jailbee.entry import main, rewrite_app_argv
from jailbee.incus import IncusError


def test_incus_error_is_reported_as_a_message_not_a_traceback(mocker, capsys):
    """An IncusError reaching the top is jailbee's own diagnosis, not a crash.

    It already names the failing command and carries whatever incus wrote to
    stderr, so Typer's traceback hook adds a screenful of jailbee internals
    on top and buries the one line that matters. A host with no `incus`
    binary hit this on every command.
    """
    mocker.patch("jailbee.macos.maybe_delegate")
    # An option-shaped argv takes rewrite_app_argv's early-return branch, so
    # this test doesn't depend on however the real test runner was invoked.
    mocker.patch.object(sys, "argv", ["jailbee", "--help"])
    mocker.patch(
        "jailbee.cli.app",
        side_effect=IncusError("`incus` not found in PATH — Incus is not installed"),
    )

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "not found in PATH" in err
    assert "Traceback" not in err


def test_successful_run_is_left_alone(mocker):
    """The handler must not swallow the normal path or its exit code."""
    mocker.patch("jailbee.macos.maybe_delegate")
    mocker.patch.object(sys, "argv", ["jailbee", "--help"])
    app = mocker.patch("jailbee.cli.app")

    main()

    app.assert_called_once_with()


def test_main_applies_the_rewrite_before_running_the_app(mocker):
    """`main()` must actually route sys.argv through rewrite_app_argv.

    The two tests above deliberately use `--help`-shaped argv, which takes
    `rewrite_app_argv`'s early-return branch — they would still pass even if
    `main()` never called it at all. This one exercises the rewrite path
    itself, so removing the wiring line in `main()` fails it.
    """
    mocker.patch("jailbee.macos.maybe_delegate")
    mocker.patch("jailbee.entry._command_names", return_value=set())
    mocker.patch("jailbee.entry._top_level_app_names", return_value={"figma"})
    mocker.patch.object(sys, "argv", ["jailbee", "figma", "--flag"])
    app = mocker.patch("jailbee.cli.app")

    main()

    app.assert_called_once_with()
    assert sys.argv == ["jailbee", "apps", "run", "figma", "--flag"]


def test_a_registered_command_is_left_alone(mocker):
    load = mocker.patch("jailbee.entry._top_level_app_names")
    assert rewrite_app_argv(["ls", "--all"]) == ["ls", "--all"]
    # A known command must not even reach the config load.
    assert not load.called


def test_an_option_is_left_alone(mocker):
    load = mocker.patch("jailbee.entry._top_level_app_names")
    assert rewrite_app_argv(["--help"]) == ["--help"]
    assert not load.called


def test_empty_argv_is_left_alone():
    assert rewrite_app_argv([]) == []


def test_a_top_level_app_is_rewritten(mocker):
    mocker.patch("jailbee.entry._top_level_app_names", return_value={"figma"})
    assert rewrite_app_argv(["figma", "--flag"]) == ["apps", "run", "figma", "--flag"]


def test_an_unknown_name_falls_through_to_typers_own_error(mocker):
    mocker.patch("jailbee.entry._top_level_app_names", return_value={"figma"})
    assert rewrite_app_argv(["lss"]) == ["lss"]


def test_a_broken_config_does_not_break_unrelated_commands(mocker):
    # Outside a repo, or with a config that fails validation, an unknown
    # first argument must still reach Typer's own error rather than a
    # traceback from the config loader.
    mocker.patch("jailbee.entry._top_level_app_names", side_effect=RuntimeError("boom"))
    assert rewrite_app_argv(["lss"]) == ["lss"]


def test_command_names_come_from_the_live_typer_app():
    from jailbee.entry import _command_names

    names = _command_names()
    assert {"ls", "new", "shell", "exec", "apps"} <= names
