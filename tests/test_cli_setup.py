"""`jailbee setup`'s CLI surface, and where the one-shot hint appears.

The steps themselves are covered by tests/test_setup_command.py; these
tests are about plumbing — which flags reach `run_setup`, what the command
records, and which commands print the hint (and on which stream).
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

runner = CliRunner()

FIXTURES = Path(__file__).parent / "fixtures"

_HINT = ["Post-install steps that have not been done on this machine:", "    - x: y"]


def _stub_hint(mocker, lines=None):
    return mocker.patch(
        "jailbee.setup_command.consume_hint",
        return_value=_HINT if lines is None else lines,
    )


def _stub_run(mocker, ran=("completions", "timer", "skills")):
    mocker.patch("jailbee.setup_command.record_setup")
    return mocker.patch("jailbee.setup_command.run_setup", return_value=list(ran))


# --------------------------------------------------------------------------
# the command
# --------------------------------------------------------------------------


def test_setup_yes_runs_every_step_without_asking(mocker) -> None:
    run = _stub_run(mocker)
    mocker.patch("jailbee.setup_command.detect_shell", return_value="bash")
    from jailbee.cli import app

    result = runner.invoke(app, ["setup", "--yes"])

    assert result.exit_code == 0, result.output
    kwargs = run.call_args.kwargs
    assert kwargs["confirm"] is None, "--yes must ask nothing"
    assert kwargs["shells"] == ["bash"]
    assert list(kwargs["keys"]) == ["completions", "timer", "skills"]


def test_setup_is_interactive_by_default(mocker) -> None:
    run = _stub_run(mocker)
    mocker.patch("jailbee.setup_command.detect_shell", return_value="bash")
    from jailbee.cli import app

    result = runner.invoke(app, ["setup"], input="n\nn\nn\n")

    assert result.exit_code == 0, result.output
    assert callable(run.call_args.kwargs["confirm"])


def test_setup_only_restricts_the_steps(mocker) -> None:
    run = _stub_run(mocker, ran=["skills"])
    mocker.patch("jailbee.setup_command.detect_shell", return_value="bash")
    from jailbee.cli import app

    result = runner.invoke(app, ["setup", "--yes", "--only", "skills", "--only", "timer"])

    assert result.exit_code == 0, result.output
    assert sorted(run.call_args.kwargs["keys"]) == ["skills", "timer"]


def test_setup_rejects_an_unknown_step(mocker) -> None:
    run = _stub_run(mocker)
    from jailbee.cli import app

    result = runner.invoke(app, ["setup", "--yes", "--only", "profiles"])

    assert result.exit_code == 2
    assert "profiles" in result.output
    run.assert_not_called()


def test_setup_shell_flag_overrides_detection(mocker) -> None:
    run = _stub_run(mocker)
    detect = mocker.patch("jailbee.setup_command.detect_shell", return_value="bash")
    from jailbee.cli import app

    result = runner.invoke(app, ["setup", "--yes", "--shell", "fish", "--shell", "zsh"])

    assert result.exit_code == 0, result.output
    assert run.call_args.kwargs["shells"] == ["fish", "zsh"]
    detect.assert_not_called()


def test_setup_rejects_an_unsupported_shell(mocker) -> None:
    run = _stub_run(mocker)
    from jailbee.cli import app

    result = runner.invoke(app, ["setup", "--yes", "--shell", "csh"])

    assert result.exit_code == 2
    assert "csh" in result.output
    run.assert_not_called()


def test_setup_records_the_run_even_when_every_step_is_declined(mocker) -> None:
    """Having run `setup` and declined is a decision — the hint must not
    come back and ask again."""
    record = mocker.patch("jailbee.setup_command.record_setup")
    mocker.patch("jailbee.setup_command.run_setup", return_value=[])
    mocker.patch("jailbee.setup_command.detect_shell", return_value="bash")
    from jailbee import __version__
    from jailbee.cli import app

    result = runner.invoke(app, ["setup", "--yes"])

    assert result.exit_code == 0, result.output
    assert record.call_args.args[1] == __version__


def test_setup_points_at_the_host_setup_docs(mocker) -> None:
    """The steps here are the ones jailbee can do itself; the firewall, UID
    delegation and Incus itself are the doc's job."""
    _stub_run(mocker)
    mocker.patch("jailbee.setup_command.detect_shell", return_value="bash")
    from jailbee.cli import app

    result = runner.invoke(app, ["setup", "--yes"])

    assert "docs/installation.md" in result.output
    assert "jb doctor" in result.output


def test_setup_mentions_linger_after_installing_the_timer(mocker) -> None:
    _stub_run(mocker, ran=["timer"])
    mocker.patch("jailbee.setup_command.detect_shell", return_value="bash")
    tip = mocker.patch("jailbee.setup_command.linger_tip")
    from jailbee.cli import app

    result = runner.invoke(app, ["setup", "--yes"])

    assert result.exit_code == 0, result.output
    tip.assert_called_once_with()


def test_setup_skips_the_linger_tip_when_the_timer_was_not_touched(mocker) -> None:
    _stub_run(mocker, ran=["skills"])
    mocker.patch("jailbee.setup_command.detect_shell", return_value="bash")
    tip = mocker.patch("jailbee.setup_command.linger_tip")
    from jailbee.cli import app

    runner.invoke(app, ["setup", "--yes", "--only", "skills"])

    tip.assert_not_called()


def test_setup_needs_no_repo_config(mocker) -> None:
    """It sets up the *machine*: running it from an unconfigured directory
    (which is where a fresh install starts) must work."""
    _stub_run(mocker)
    mocker.patch("jailbee.setup_command.detect_shell", return_value="bash")
    load = mocker.patch("jailbee.cli._load_or_exit")
    from jailbee.cli import app

    result = runner.invoke(app, ["setup", "--yes"])

    assert result.exit_code == 0, result.output
    load.assert_not_called()


# --------------------------------------------------------------------------
# the hint
# --------------------------------------------------------------------------


def test_advise_setup_writes_only_to_stderr(mocker, capsys) -> None:
    """`jailbee ls`'s table is parsed by scripts — the hint must not enter it."""
    from jailbee.cli import _advise_setup

    _stub_hint(mocker)
    _advise_setup()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Post-install steps" in captured.err


def test_ls_asks_for_the_setup_hint(mocker) -> None:
    from jailbee.cli import app

    mocker.patch("jailbee.lifecycle.list_containers", return_value=[])
    mocker.patch("jailbee.lifecycle.repo_has_submodules", return_value=False)
    mocker.patch("jailbee.incus.Incus")
    hint = _stub_hint(mocker)

    result = runner.invoke(app, ["ls", "--config", str(FIXTURES / "full_config.yaml")])

    assert result.exit_code == 0, result.output
    assert hint.call_count == 1


def test_shell_asks_for_the_setup_hint_before_attaching(mocker) -> None:
    from jailbee.cli import app

    mocker.patch("jailbee.cli._resolve_attachable", return_value=(mocker.MagicMock(), "c1"))
    attach = mocker.patch("jailbee.cli._attach_shell", return_value=0)
    hint = _stub_hint(mocker)

    calls = mocker.MagicMock()
    calls.attach_mock(hint, "hint")
    calls.attach_mock(attach, "attach")

    result = runner.invoke(app, ["shell", "c1", "--config", str(FIXTURES / "full_config.yaml")])

    assert result.exit_code == 0, result.output
    assert [name for name, _, _ in calls.mock_calls] == ["hint", "attach"]


def test_a_broken_state_db_does_not_break_the_command(mocker) -> None:
    """The hint is a courtesy. Anything going wrong in it must be swallowed."""
    from jailbee.cli import app

    mocker.patch("jailbee.lifecycle.list_containers", return_value=[])
    mocker.patch("jailbee.lifecycle.repo_has_submodules", return_value=False)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.setup_command.consume_hint", side_effect=RuntimeError("db is locked"))

    result = runner.invoke(app, ["ls", "--config", str(FIXTURES / "full_config.yaml")])

    assert result.exit_code == 0, result.output
    assert "db is locked" not in result.output


def test_nothing_missing_prints_nothing(mocker, capsys) -> None:
    from jailbee.cli import _advise_setup

    _stub_hint(mocker, lines=[])
    _advise_setup()

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


# --------------------------------------------------------------------------
# the read-only listing
# --------------------------------------------------------------------------


def test_status_reports_the_steps_without_installing(mocker) -> None:
    run = _stub_run(mocker)
    mocker.patch("jailbee.setup_command.detect_shell", return_value="bash")
    report = mocker.patch("jailbee.setup_command.report_status")
    from jailbee.cli import app

    result = runner.invoke(app, ["setup", "--status"])

    assert result.exit_code == 0, result.output
    run.assert_not_called()
    assert list(report.call_args.args[0]) == ["completions", "timer", "skills"]
    assert report.call_args.args[1] == ["bash"]


def test_status_honours_only_and_shell(mocker) -> None:
    _stub_run(mocker)
    report = mocker.patch("jailbee.setup_command.report_status")
    from jailbee.cli import app

    result = runner.invoke(app, ["setup", "--status", "--only", "timer", "--shell", "fish"])

    assert result.exit_code == 0, result.output
    assert list(report.call_args.args[0]) == ["timer"]
    assert report.call_args.args[1] == ["fish"]


def test_status_and_yes_contradict_each_other(mocker) -> None:
    run = _stub_run(mocker)
    report = mocker.patch("jailbee.setup_command.report_status")
    from jailbee.cli import app

    result = runner.invoke(app, ["setup", "--status", "--yes"])

    assert result.exit_code == 2
    run.assert_not_called()
    report.assert_not_called()


def test_status_still_rejects_an_unknown_step(mocker) -> None:
    report = mocker.patch("jailbee.setup_command.report_status")
    from jailbee.cli import app

    result = runner.invoke(app, ["setup", "--status", "--only", "profiles"])

    assert result.exit_code == 2
    report.assert_not_called()


# --------------------------------------------------------------------------
# the interactive offer
# --------------------------------------------------------------------------


def _stub_pending(mocker, steps=None):
    from jailbee.setup_command import StepStatus

    rows = (
        [
            StepStatus(
                key="timer", title="egress refresh timer", installed=False, detail="missing: /x"
            )
        ]
        if steps is None
        else steps
    )
    return mocker.patch("jailbee.setup_command.hint_pending", return_value=rows)


def _terminal(mocker):
    mocker.patch("jailbee.cli._setup_offer_allowed", return_value=True)
    mocker.patch("jailbee.setup_command.linger_tip")
    mocker.patch("jailbee.setup_command.detect_shell", return_value="bash")


def _ls(mocker, *args, input=None):
    from jailbee.cli import app

    mocker.patch("jailbee.lifecycle.list_containers", return_value=[])
    mocker.patch("jailbee.lifecycle.repo_has_submodules", return_value=False)
    mocker.patch("jailbee.incus.Incus")
    return runner.invoke(
        app, ["ls", "--config", str(FIXTURES / "full_config.yaml"), *args], input=input
    )


def test_ls_offers_to_run_setup_on_a_terminal(mocker) -> None:
    _terminal(mocker)
    _stub_pending(mocker)
    run = _stub_run(mocker)

    result = _ls(mocker, input="y\n")

    assert result.exit_code == 0, result.output
    assert "egress refresh timer" in result.output
    run.assert_called_once()
    assert list(run.call_args.kwargs["keys"]) == ["completions", "timer", "skills"]


def test_declining_the_offer_installs_nothing_and_lists_anyway(mocker) -> None:
    _terminal(mocker)
    _stub_pending(mocker)
    run = _stub_run(mocker)

    result = _ls(mocker, input="n\n")

    assert result.exit_code == 0, result.output
    run.assert_not_called()


def test_ls_json_never_stops_to_ask(mocker) -> None:
    """`--format json` is for a parser, terminal or not."""
    _terminal(mocker)
    pending = _stub_pending(mocker)
    hint = _stub_hint(mocker, lines=[])

    result = _ls(mocker, "--format", "json")

    assert result.exit_code == 0, result.output
    pending.assert_not_called()
    assert hint.call_count == 1


def test_a_pipe_gets_the_one_shot_hint_instead_of_a_question(mocker) -> None:
    mocker.patch("jailbee.cli._setup_offer_allowed", return_value=False)
    pending = _stub_pending(mocker)
    hint = _stub_hint(mocker)

    result = _ls(mocker)

    assert result.exit_code == 0, result.output
    pending.assert_not_called()
    assert hint.call_count == 1


def test_shell_never_stops_to_ask(mocker) -> None:
    """Mid-workflow: the terminal is about to belong to the container."""
    from jailbee.cli import app

    _terminal(mocker)
    pending = _stub_pending(mocker)
    hint = _stub_hint(mocker)
    mocker.patch("jailbee.cli._resolve_attachable", return_value=(mocker.MagicMock(), "c1"))
    mocker.patch("jailbee.cli._attach_shell", return_value=0)

    result = runner.invoke(app, ["shell", "c1", "--config", str(FIXTURES / "full_config.yaml")])

    assert result.exit_code == 0, result.output
    pending.assert_not_called()
    assert hint.call_count == 1


def test_a_failing_step_does_not_take_down_the_command(mocker) -> None:
    _terminal(mocker)
    _stub_pending(mocker)
    mocker.patch("jailbee.setup_command.record_setup")
    mocker.patch("jailbee.setup_command.run_setup", side_effect=RuntimeError("systemctl exploded"))

    result = _ls(mocker, input="y\n")

    assert result.exit_code == 0, result.output
    assert "systemctl exploded" in result.output


def test_dashboard_offers_to_run_setup(mocker) -> None:
    from jailbee.cli import app
    from jailbee.config import ConfigNotFoundError

    _terminal(mocker)
    _stub_pending(mocker)
    run = _stub_run(mocker)
    mocker.patch("jailbee.config.load_repo_config", side_effect=ConfigNotFoundError("none"))
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.dashboard.run", return_value=0)

    result = runner.invoke(app, ["dashboard"], input="y\n")

    assert result.exit_code == 0, result.output
    run.assert_called_once()
