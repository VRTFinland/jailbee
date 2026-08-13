"""Tests for `gie submodule checkout`."""

from typer.testing import CliRunner

from jailbee.cli import app


def test_submodule_checkout_help_lists_command():
    result = CliRunner().invoke(app, ["submodule", "--help"])
    assert result.exit_code == 0
    assert "checkout" in result.output


def test_submodule_checkout_host_path(mocker, tmp_path):
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    host = mocker.patch(
        "jailbee.sync.checkout_submodules_on_host",
        return_value=("feat/foo", [("lib", "feat/foo")]),
    )
    resolve = mocker.patch("jailbee.cli._resolve_existing")

    result = CliRunner().invoke(app, ["submodule", "checkout"])

    assert result.exit_code == 0, result.output
    host.assert_called_once_with(cfg_mock, branch=None)
    resolve.assert_not_called()  # no name -> host, never touches a container
    assert "feat/foo" in result.output


def test_submodule_checkout_host_branch_override(mocker, tmp_path):
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    host = mocker.patch(
        "jailbee.sync.checkout_submodules_on_host",
        return_value=("feat/x", []),
    )

    result = CliRunner().invoke(app, ["submodule", "checkout", "-b", "feat/x"])

    assert result.exit_code == 0, result.output
    host.assert_called_once_with(cfg_mock, branch="feat/x")


def test_submodule_checkout_container_path(mocker, tmp_path):
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "sampleapp-feat-foo"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    ctr = mocker.patch(
        "jailbee.sync.checkout_submodules_in_container",
        return_value=("feat/foo", [("lib", "feat/foo")]),
    )

    result = CliRunner().invoke(app, ["submodule", "checkout", "feat-foo"])

    assert result.exit_code == 0, result.output
    ctr.assert_called_once()
    assert ctr.call_args.args[2] == "feat-foo"
    assert ctr.call_args.kwargs["branch"] is None


def test_submodule_checkout_reports_sync_error(mocker, tmp_path):
    from jailbee import sync

    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.sync.checkout_submodules_on_host",
        side_effect=sync.SyncError("Host is in detached HEAD; pass -b <branch> ..."),
    )

    result = CliRunner().invoke(app, ["submodule", "checkout"])

    assert result.exit_code == 1
    assert "detached HEAD" in result.output
