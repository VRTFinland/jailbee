"""Tests for the console entry point's top-level error handling."""

from __future__ import annotations

import pytest

from jailbee.entry import main
from jailbee.incus import IncusError


def test_incus_error_is_reported_as_a_message_not_a_traceback(mocker, capsys):
    """An IncusError reaching the top is jailbee's own diagnosis, not a crash.

    It already names the failing command and carries whatever incus wrote to
    stderr, so Typer's traceback hook adds a screenful of jailbee internals
    on top and buries the one line that matters. A host with no `incus`
    binary hit this on every command.
    """
    mocker.patch("jailbee.macos.maybe_delegate")
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
    app = mocker.patch("jailbee.cli.app")

    main()

    app.assert_called_once_with()
