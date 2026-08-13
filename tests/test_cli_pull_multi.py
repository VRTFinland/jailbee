"""CLI tests for multi-select `gie git pull`."""

from __future__ import annotations

from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.lifecycle import ContainerInfo, ResolvedContainer


def _info(name: str, mode: str = "clone", state: str = "Running") -> ContainerInfo:
    return ContainerInfo(
        name=name,
        state=state,
        network=None,
        ip=None,
        memory_limit=None,
        repo="myrepo",
        mode=mode,
    )


def _wire(mocker, tmp_path, *, containers, picked):
    """Common wiring: cfg, list_containers, picker, short_name, TTY."""
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "myrepo"
    cfg_mock.pull.destroy_container = "never"
    cfg_mock.pull.delete_branch = "never"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=containers,
    )
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch(
        "jailbee.tui.pick_containers_multi",
        return_value=picked,
    )

    def _short(_cfg, full: str) -> str:
        return full.removeprefix("myrepo-")

    mocker.patch("jailbee.lifecycle.short_name", side_effect=_short)
    return cfg_mock


def test_pull_multi_fail_fast_does_not_attempt_remaining(mocker, tmp_path):
    from jailbee.sync import SyncError

    _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-a"), _info("myrepo-feat-b"), _info("myrepo-feat-c")],
        picked=["myrepo-feat-a", "myrepo-feat-b", "myrepo-feat-c"],
    )
    do_pull = mocker.patch(
        "jailbee.cli._do_single_pull",
        side_effect=[None, SyncError("boom on b"), None],
    )

    result = CliRunner().invoke(app, ["git", "pull"])

    assert result.exit_code == 1
    shorts_called = [c.args[2] for c in do_pull.call_args_list]
    assert shorts_called == ["feat-a", "feat-b"]
    combined = result.stdout + (result.stderr or "")
    assert "boom on b" in combined
    assert "feat-c" in combined
    assert "not attempted" in combined.lower()


def test_pull_multi_all_succeed(mocker, tmp_path):
    _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-a"), _info("myrepo-feat-b")],
        picked=["myrepo-feat-a", "myrepo-feat-b"],
    )
    do_pull = mocker.patch("jailbee.cli._do_single_pull")

    result = CliRunner().invoke(app, ["git", "pull"])

    assert result.exit_code == 0, result.output
    shorts = [c.args[2] for c in do_pull.call_args_list]
    assert shorts == ["feat-a", "feat-b"]


def test_pull_multi_empty_selection_exits_zero(mocker, tmp_path):
    _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-a"), _info("myrepo-feat-b")],
        picked=[],
    )
    do_pull = mocker.patch("jailbee.cli._do_single_pull")

    result = CliRunner().invoke(app, ["git", "pull"])

    assert result.exit_code == 0, result.output
    assert "Nothing selected" in result.output
    do_pull.assert_not_called()


def test_pull_multi_user_cancels(mocker, tmp_path):
    _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-a"), _info("myrepo-feat-b")],
        picked=None,
    )
    do_pull = mocker.patch("jailbee.cli._do_single_pull")

    result = CliRunner().invoke(app, ["git", "pull"])

    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    assert "Aborted" in combined
    do_pull.assert_not_called()


def test_pull_multi_single_container_auto_selects(mocker, tmp_path):
    """One eligible container -> skip the picker, pull from it directly.

    Confirmation is disabled here: this test is about the auto-select
    mechanic, not the confirmation prompt added on top of it (see
    test_pull_confirms_when_the_single_container_was_auto_selected).
    """
    cfg_mock = _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-a")],
        picked=None,  # picker must NOT be called
    )
    cfg_mock.confirm.auto_target = False
    picker = mocker.patch(
        "jailbee.tui.pick_containers_multi",
        return_value=None,
    )
    do_pull = mocker.patch("jailbee.cli._do_single_pull")

    result = CliRunner().invoke(app, ["git", "pull"])

    assert result.exit_code == 0, result.output
    picker.assert_not_called()
    shorts = [c.args[2] for c in do_pull.call_args_list]
    assert shorts == ["feat-a"]
    assert "Only one eligible container" in result.output


def test_pull_multi_filters_out_mount_mode(mocker, tmp_path):
    cfg_mock = _wire(
        mocker,
        tmp_path,
        containers=[
            _info("myrepo-feat-a", mode="clone"),
            _info("myrepo-mount", mode="mount"),
        ],
        picked=["myrepo-feat-a"],
    )
    cfg_mock.confirm.auto_target = False
    do_pull = mocker.patch("jailbee.cli._do_single_pull")

    CliRunner().invoke(app, ["git", "pull"])

    # Single pullable container after mount filter -> auto-selected.
    shorts = [c.args[2] for c in do_pull.call_args_list]
    assert shorts == ["feat-a"]


def test_pull_multi_empty_pullable_list_errors(mocker, tmp_path):
    _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-mount", mode="mount")],
        picked=None,
    )
    do_pull = mocker.patch("jailbee.cli._do_single_pull")

    result = CliRunner().invoke(app, ["git", "pull"])

    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "No containers eligible for pull" in combined
    do_pull.assert_not_called()


def test_pull_multi_current_applies_to_all(mocker, tmp_path):
    _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-a"), _info("myrepo-feat-b")],
        picked=["myrepo-feat-a", "myrepo-feat-b"],
    )
    mocker.patch("jailbee.git.get_current_branch", return_value="dev")
    do_pull = mocker.patch("jailbee.cli._do_single_pull")

    result = CliRunner().invoke(app, ["git", "pull", "--current"])

    assert result.exit_code == 0, result.output
    intos = [c.kwargs["into"] for c in do_pull.call_args_list]
    assert intos == ["dev", "dev"]


def _fake_pull_plan():
    from jailbee.sync import BridgePlan, RefSummary

    return BridgePlan(
        direction="pull",
        container_short="feat-only",
        container_full="myrepo-feat-only",
        container_state="Running",
        source=RefSummary(label="feat/foo", oid="a" * 40, subject="WIP parser"),
        target=RefSummary(label="main", oid="b" * 40, subject="Release 1.2"),
        action="merge",
        incoming=3,
        notes=(),
    )


def test_pull_confirms_when_the_single_container_was_auto_selected(mocker, tmp_path):
    cfg_mock = _wire(mocker, tmp_path, containers=[_info("myrepo-feat-only")], picked=None)
    cfg_mock.confirm.auto_target = True
    mocker.patch("jailbee.sync.plan_pull", return_value=_fake_pull_plan())
    do_pull = mocker.patch("jailbee.cli._do_single_pull")

    result = CliRunner().invoke(app, ["git", "pull"], input="y\n")

    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "Pull  container ──▶ host" in combined
    assert "feat/foo" in combined
    assert "main" in combined
    do_pull.assert_called_once()


def test_pull_declined_confirmation_does_not_merge(mocker, tmp_path):
    cfg_mock = _wire(mocker, tmp_path, containers=[_info("myrepo-feat-only")], picked=None)
    cfg_mock.confirm.auto_target = True
    mocker.patch("jailbee.sync.plan_pull", return_value=_fake_pull_plan())
    do_pull = mocker.patch("jailbee.cli._do_single_pull")

    result = CliRunner().invoke(app, ["git", "pull"], input="n\n")

    assert result.exit_code != 0
    do_pull.assert_not_called()


def test_pull_no_confirm_flag_skips_the_plan(mocker, tmp_path):
    cfg_mock = _wire(mocker, tmp_path, containers=[_info("myrepo-feat-only")], picked=None)
    cfg_mock.confirm.auto_target = True
    plan_pull = mocker.patch("jailbee.sync.plan_pull")
    do_pull = mocker.patch("jailbee.cli._do_single_pull")

    result = CliRunner().invoke(app, ["git", "pull", "--no-confirm"])

    assert result.exit_code == 0
    plan_pull.assert_not_called()
    do_pull.assert_called_once()


def test_pull_picker_selection_is_not_confirmed(mocker, tmp_path):
    cfg_mock = _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-a"), _info("myrepo-feat-b")],
        picked=["myrepo-feat-a", "myrepo-feat-b"],
    )
    cfg_mock.confirm.auto_target = True
    plan_pull = mocker.patch("jailbee.sync.plan_pull")
    mocker.patch("jailbee.cli._do_single_pull")

    result = CliRunner().invoke(app, ["git", "pull"])

    assert result.exit_code == 0
    plan_pull.assert_not_called()


def test_pull_off_tty_prints_the_plan_and_proceeds(mocker, tmp_path):
    """No prompt is possible; the block still lands in the log."""
    cfg_mock = _wire(mocker, tmp_path, containers=[_info("myrepo-feat-only")], picked=None)
    cfg_mock.confirm.auto_target = True
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
    mocker.patch(
        "jailbee.lifecycle.resolve_container_for_interactive_detailed",
        return_value=ResolvedContainer(name="myrepo-feat-only", auto_selected=True),
    )
    mocker.patch("jailbee.sync.plan_pull", return_value=_fake_pull_plan())
    do_pull = mocker.patch("jailbee.cli._do_single_pull")

    result = CliRunner().invoke(app, ["git", "pull"])

    assert result.exit_code == 0
    assert "Pull  container ──▶ host" in (result.stdout + (result.stderr or ""))
    do_pull.assert_called_once()


def test_pull_off_tty_mount_mode_auto_selected_skips_the_confirmation(mocker, tmp_path):
    """M5: off a TTY, pull's fall-through resolves via
    resolve_container_for_interactive_detailed, which — unlike the TTY
    multi-select path just above — does not filter mount mode out. A lone
    mount-mode container must not get a plan block for an operation mount
    mode can't do.
    """
    cfg_mock = _wire(
        mocker, tmp_path, containers=[_info("myrepo-mount", mode="mount")], picked=None
    )
    cfg_mock.confirm.auto_target = True
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
    incus_cls = mocker.patch("jailbee.incus.Incus")
    incus_cls.return_value.config_get.return_value = "mount"
    mocker.patch(
        "jailbee.lifecycle.resolve_container_for_interactive_detailed",
        return_value=ResolvedContainer(name="myrepo-mount", auto_selected=True),
    )
    plan_pull = mocker.patch("jailbee.sync.plan_pull")
    do_pull = mocker.patch("jailbee.cli._do_single_pull")

    result = CliRunner().invoke(app, ["git", "pull"])

    assert result.exit_code == 0
    plan_pull.assert_not_called()
    do_pull.assert_called_once()


def test_pull_confirm_flag_overrides_a_disabled_config(mocker, tmp_path):
    cfg_mock = _wire(mocker, tmp_path, containers=[_info("myrepo-feat-only")], picked=None)
    cfg_mock.confirm.auto_target = False
    mocker.patch("jailbee.sync.plan_pull", return_value=_fake_pull_plan())
    do_pull = mocker.patch("jailbee.cli._do_single_pull")

    result = CliRunner().invoke(app, ["git", "pull", "--confirm"], input="y\n")

    assert result.exit_code == 0
    assert "Pull  container ──▶ host" in (result.stdout + (result.stderr or ""))
    do_pull.assert_called_once()


def test_pull_unbuildable_plan_skips_the_prompt_and_still_pulls(mocker, tmp_path):
    """A plan is a preview: an IncusError while building one must not fail
    the command — the pull proceeds and produces its own precise error.
    """
    from jailbee.incus import IncusError

    cfg_mock = _wire(mocker, tmp_path, containers=[_info("myrepo-feat-only")], picked=None)
    cfg_mock.confirm.auto_target = True
    mocker.patch("jailbee.sync.plan_pull", side_effect=IncusError("incus list failed"))
    do_pull = mocker.patch("jailbee.cli._do_single_pull")

    result = CliRunner().invoke(app, ["git", "pull"])

    assert result.exit_code == 0
    assert "Pull  container ──▶ host" not in (result.stdout + (result.stderr or ""))
    do_pull.assert_called_once()


def test_pull_unbuildable_plan_value_error_skips_the_prompt_and_still_pulls(mocker, tmp_path):
    """Same tolerance, the other caught exception: resolve_container_name
    raises ValueError for a container that vanished between listing and now.
    """
    cfg_mock = _wire(mocker, tmp_path, containers=[_info("myrepo-feat-only")], picked=None)
    cfg_mock.confirm.auto_target = True
    mocker.patch("jailbee.sync.plan_pull", side_effect=ValueError("no such container"))
    do_pull = mocker.patch("jailbee.cli._do_single_pull")

    result = CliRunner().invoke(app, ["git", "pull"])

    assert result.exit_code == 0
    assert "Pull  container ──▶ host" not in (result.stdout + (result.stderr or ""))
    do_pull.assert_called_once()
