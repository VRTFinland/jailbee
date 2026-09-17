"""The PyPI update hint's surfaces: where it appears, on which stream, that
the opt-out reaches it, and that it can never fail the command it decorates."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

runner = CliRunner()

FIXTURES = Path(__file__).parent / "fixtures"

_HINT_SENTINEL = ["jailbee 9.9.9 is available (you are running 1.4.0).", "    Upgrade with: x"]


@pytest.fixture(autouse=True)
def _allow_update_check(monkeypatch) -> None:
    """Undo `conftest._block_update_check` — this file is about the check."""
    monkeypatch.delenv("JAILBEE_NO_UPDATE_CHECK", raising=False)


def _stub_hint(mocker, lines=None):
    """Patch the read path, not the DB: these tests are about plumbing —
    is it called, where does it print, what silences it."""
    return mocker.patch(
        "jailbee.update_check.consume_hint",
        return_value=_HINT_SENTINEL if lines is None else lines,
    )


def test_advise_update_writes_only_to_stderr(mocker, capsys) -> None:
    """`jailbee ls`'s table is parsed by scripts — the hint must not enter it."""
    from jailbee.cli import _advise_update

    _stub_hint(mocker)
    mocker.patch("jailbee.update_check.maybe_probe")
    _advise_update()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "9.9.9 is available" in captured.err


def test_advise_update_starts_a_probe_for_the_next_run(mocker) -> None:
    from jailbee.cli import _advise_update

    _stub_hint(mocker, [])
    probe = mocker.patch("jailbee.update_check.maybe_probe")
    _advise_update()

    assert probe.call_count == 1
    assert probe.call_args.kwargs["enabled"] is True


def test_the_config_opt_out_stops_both_the_hint_and_the_probe(mocker, capsys) -> None:
    from jailbee.cli import _advise_update
    from jailbee.global_config import GlobalConfig

    mocker.patch(
        "jailbee.update_check.load_global_config",
        return_value=(GlobalConfig(update_check=False), []),
    )
    hint = _stub_hint(mocker)
    probe = mocker.patch("jailbee.update_check.maybe_probe")
    _advise_update()

    assert hint.call_count == 0
    assert probe.call_args.kwargs["enabled"] is False
    assert capsys.readouterr().err == ""


def test_the_env_var_stops_the_hint(mocker, monkeypatch, capsys) -> None:
    """A script that wants no advisory output must not have to edit the
    user's config file to get it."""
    from jailbee.cli import _advise_update
    from jailbee.update_check import ENV_DISABLE

    monkeypatch.setenv(ENV_DISABLE, "1")
    hint = _stub_hint(mocker)
    mocker.patch("jailbee.update_check.maybe_probe")
    _advise_update()

    assert hint.call_count == 0
    assert capsys.readouterr().err == ""


def test_a_broken_state_database_cannot_fail_the_command(mocker, capsys) -> None:
    """Advice is a courtesy — the same contract `_advise_upgrade` holds to."""
    from jailbee.cli import _advise_update

    mocker.patch("jailbee.update_check.consume_hint", side_effect=RuntimeError("locked"))
    mocker.patch("jailbee.update_check.maybe_probe")

    _advise_update()  # must not raise

    assert capsys.readouterr().err == ""


def test_ls_shows_the_update_hint(mocker) -> None:
    from jailbee.cli import app

    mocker.patch("jailbee.lifecycle.list_containers", return_value=[])
    mocker.patch("jailbee.lifecycle.repo_has_submodules", return_value=False)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.update_check.maybe_probe")
    hint = _stub_hint(mocker)

    result = runner.invoke(app, ["ls", "--config", str(FIXTURES / "full_config.yaml")])

    assert result.exit_code == 0, result.output
    assert hint.call_count == 1


def test_shell_shows_the_update_hint(mocker) -> None:
    """`shell` hands the terminal to a container and never returns — the hint
    has to be printed before that, or not at all."""
    from jailbee.cli import app

    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.cli._resolve_attachable", return_value=(mocker.MagicMock(), "c"))
    mocker.patch("jailbee.cli._attach_shell", return_value=0)
    mocker.patch("jailbee.update_check.maybe_probe")
    hint = _stub_hint(mocker)

    result = runner.invoke(app, ["shell", "--config", str(FIXTURES / "full_config.yaml")])

    assert result.exit_code == 0, result.output
    assert hint.call_count == 1
