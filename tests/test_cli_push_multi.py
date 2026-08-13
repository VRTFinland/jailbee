"""CLI tests for multi-select `gie git push`."""

from __future__ import annotations

from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.lifecycle import ContainerInfo


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


def _wire(mocker, tmp_path, *, containers, picked, action="plain", source="default-branch"):
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "myrepo"
    cfg_mock.push.default_action = action
    cfg_mock.push.default_source = source
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
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")

    def _short(_cfg, full: str) -> str:
        return full.removeprefix("myrepo-")

    mocker.patch("jailbee.lifecycle.short_name", side_effect=_short)
    return cfg_mock


def test_push_multi_continue_on_error_with_summary(mocker, tmp_path):
    from jailbee.sync import SyncError

    _wire(
        mocker,
        tmp_path,
        containers=[
            _info("myrepo-feat-a"),
            _info("myrepo-feat-b"),
            _info("myrepo-feat-c"),
        ],
        picked=["myrepo-feat-a", "myrepo-feat-b", "myrepo-feat-c"],
        action="plain",
    )

    def _push_side_effect(_cfg, _incus, short, *, source, action, **_ref_opts):
        if short == "feat-b":
            raise SyncError("Container working tree is dirty.")
        return f"pushed '{source}' -> refs/jailbee/host/{source} (new)"

    do_push = mocker.patch(
        "jailbee.cli._do_single_push",
        side_effect=_push_side_effect,
    )

    result = CliRunner().invoke(app, ["git", "push"])

    assert result.exit_code == 1
    shorts = [c.args[2] for c in do_push.call_args_list]
    assert shorts == ["feat-a", "feat-b", "feat-c"]

    combined = result.stdout + (result.stderr or "")
    assert "Summary:" in combined
    assert "✓ feat-a" in combined
    assert "✗ feat-b" in combined
    assert "Container working tree is dirty" in combined
    assert "✓ feat-c" in combined
    assert "2 succeeded, 1 failed" in combined


def test_push_multi_resolves_source_and_action_once(mocker, tmp_path):
    _wire(
        mocker,
        tmp_path,
        containers=[
            _info("myrepo-feat-a"),
            _info("myrepo-feat-b"),
            _info("myrepo-feat-c"),
        ],
        picked=["myrepo-feat-a", "myrepo-feat-b", "myrepo-feat-c"],
        action="ask",
        source="ask",
    )
    pick_source = mocker.patch(
        "jailbee.cli._pick_push_source",
        return_value="main",
    )
    pick_action = mocker.patch(
        "jailbee.cli._pick_push_action",
        return_value="plain",
    )
    mocker.patch(
        "jailbee.cli._do_single_push",
        return_value="pushed 'main' -> refs/jailbee/host/main (new)",
    )

    result = CliRunner().invoke(app, ["git", "push"])

    assert result.exit_code == 0, result.output
    pick_source.assert_called_once()
    pick_action.assert_called_once()


def test_push_multi_empty_selection_exits_zero(mocker, tmp_path):
    _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-a"), _info("myrepo-feat-b")],
        picked=[],
    )
    do_push = mocker.patch("jailbee.cli._do_single_push")

    result = CliRunner().invoke(app, ["git", "push"])

    assert result.exit_code == 0, result.output
    assert "Nothing selected" in result.output
    do_push.assert_not_called()


def test_push_multi_user_cancels(mocker, tmp_path):
    _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-a"), _info("myrepo-feat-b")],
        picked=None,
    )
    do_push = mocker.patch("jailbee.cli._do_single_push")

    result = CliRunner().invoke(app, ["git", "push"])

    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    assert "Aborted" in combined
    do_push.assert_not_called()


def test_push_multi_single_container_auto_selects(mocker, tmp_path):
    """One pushable container -> skip the picker, push to it directly.

    Confirmation is disabled here: this test is about the auto-select
    mechanic, not the confirmation prompt added on top of it (see
    test_push_confirms_when_the_single_container_was_auto_selected).
    """
    cfg_mock = _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-a")],
        picked=None,  # picker must NOT be called
    )
    _wire_confirm(cfg_mock, auto_target=False)
    picker = mocker.patch(
        "jailbee.tui.pick_containers_multi",
        return_value=None,
    )
    do_push = mocker.patch(
        "jailbee.cli._do_single_push",
        return_value="pushed 'main' -> refs/jailbee/host/main (new)",
    )

    result = CliRunner().invoke(app, ["git", "push"])

    assert result.exit_code == 0, result.output
    picker.assert_not_called()
    shorts = [c.args[2] for c in do_push.call_args_list]
    assert shorts == ["feat-a"]
    assert "Only one eligible container" in result.output


def test_push_multi_all_succeed_prints_summary_no_hint(mocker, tmp_path):
    _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-a"), _info("myrepo-feat-b")],
        picked=["myrepo-feat-a", "myrepo-feat-b"],
    )
    mocker.patch(
        "jailbee.cli._do_single_push",
        return_value="pushed 'main' -> refs/jailbee/host/main (new)",
    )

    result = CliRunner().invoke(app, ["git", "push"])

    assert result.exit_code == 0, result.output
    combined = result.stdout + (result.stderr or "")
    assert "Summary:" in combined
    assert "✓ feat-a" in combined
    assert "✓ feat-b" in combined
    assert "2 succeeded, 0 failed" in combined
    assert "Fix failed containers" not in combined


def _wire_confirm(cfg_mock, *, auto_target: bool = True, push_from: str = "origin"):
    """Give the MagicMock cfg the attributes the confirmation path reads."""
    cfg_mock.confirm.auto_target = auto_target
    cfg_mock.push.push_from = push_from
    cfg_mock.push.autofetch = False
    return cfg_mock


def _fake_plan():
    from jailbee.sync import BridgePlan, RefSummary

    return BridgePlan(
        direction="push",
        container_short="feat-only",
        container_full="myrepo-feat-only",
        container_state="Running",
        source=RefSummary(label="origin/main", oid="a" * 40, subject="Bump deps"),
        target=RefSummary(label="feat/foo", oid="b" * 40, subject="WIP"),
        action="plain",
        incoming=2,
        notes=(),
    )


def test_push_confirms_when_the_single_container_was_auto_selected(mocker, tmp_path):
    cfg_mock = _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-only")],
        picked=None,
        action="plain",
    )
    _wire_confirm(cfg_mock)
    mocker.patch("jailbee.sync.plan_push", return_value=_fake_plan())
    mocker.patch("jailbee.sync.prefetch_push_source", return_value=(False, None))
    do_push = mocker.patch("jailbee.cli._do_single_push", return_value="pushed")

    result = CliRunner().invoke(app, ["git", "push"], input="y\n")

    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "Push  host ──▶ container" in combined
    assert "origin/main" in combined
    do_push.assert_called_once()
    # The hoisted fetch must not run twice.
    assert do_push.call_args.kwargs["fetch"] is False


def test_push_declined_confirmation_does_not_push(mocker, tmp_path):
    cfg_mock = _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-only")],
        picked=None,
        action="plain",
    )
    _wire_confirm(cfg_mock)
    mocker.patch("jailbee.sync.plan_push", return_value=_fake_plan())
    mocker.patch("jailbee.sync.prefetch_push_source", return_value=(False, None))
    do_push = mocker.patch("jailbee.cli._do_single_push")

    result = CliRunner().invoke(app, ["git", "push"], input="n\n")

    assert result.exit_code != 0
    do_push.assert_not_called()


def test_push_bare_enter_proceeds(mocker, tmp_path):
    cfg_mock = _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-only")],
        picked=None,
        action="plain",
    )
    _wire_confirm(cfg_mock)
    mocker.patch("jailbee.sync.plan_push", return_value=_fake_plan())
    mocker.patch("jailbee.sync.prefetch_push_source", return_value=(False, None))
    do_push = mocker.patch("jailbee.cli._do_single_push", return_value="pushed")

    result = CliRunner().invoke(app, ["git", "push"], input="\n")

    assert result.exit_code == 0
    do_push.assert_called_once()


def test_push_no_confirm_flag_skips_the_prompt(mocker, tmp_path):
    cfg_mock = _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-only")],
        picked=None,
        action="plain",
    )
    _wire_confirm(cfg_mock)
    plan_push = mocker.patch("jailbee.sync.plan_push", return_value=_fake_plan())
    do_push = mocker.patch("jailbee.cli._do_single_push", return_value="pushed")

    result = CliRunner().invoke(app, ["git", "push", "--no-confirm"])

    assert result.exit_code == 0
    plan_push.assert_not_called()
    do_push.assert_called_once()
    assert do_push.call_args.kwargs["fetch"] is None


def test_push_confirm_flag_overrides_a_disabled_config(mocker, tmp_path):
    cfg_mock = _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-only")],
        picked=None,
        action="plain",
    )
    _wire_confirm(cfg_mock, auto_target=False)
    mocker.patch("jailbee.sync.plan_push", return_value=_fake_plan())
    mocker.patch("jailbee.sync.prefetch_push_source", return_value=(False, None))
    do_push = mocker.patch("jailbee.cli._do_single_push", return_value="pushed")

    result = CliRunner().invoke(app, ["git", "push", "--confirm"], input="y\n")

    assert result.exit_code == 0
    assert "Push  host ──▶ container" in (result.stdout + (result.stderr or ""))
    do_push.assert_called_once()


def test_push_config_off_means_no_plan(mocker, tmp_path):
    cfg_mock = _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-only")],
        picked=None,
        action="plain",
    )
    _wire_confirm(cfg_mock, auto_target=False)
    plan_push = mocker.patch("jailbee.sync.plan_push")
    do_push = mocker.patch("jailbee.cli._do_single_push", return_value="pushed")

    result = CliRunner().invoke(app, ["git", "push"])

    assert result.exit_code == 0
    plan_push.assert_not_called()
    do_push.assert_called_once()


def test_push_unbuildable_plan_skips_the_prompt_and_still_pushes(mocker, tmp_path):
    """A plan is a preview: failing to build one must not fail the command."""
    from jailbee.incus import IncusError

    cfg_mock = _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-only")],
        picked=None,
        action="plain",
    )
    _wire_confirm(cfg_mock)
    mocker.patch(
        "jailbee.sync.plan_push",
        side_effect=IncusError("incus list failed"),
    )
    mocker.patch("jailbee.sync.prefetch_push_source", return_value=(False, None))
    do_push = mocker.patch("jailbee.cli._do_single_push", return_value="pushed")

    result = CliRunner().invoke(app, ["git", "push"])

    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "Push  host ──▶ container" not in combined
    do_push.assert_called_once()


def test_push_unbuildable_plan_still_reports_a_failed_hoisted_fetch(mocker, tmp_path):
    """M2: the host fetch is hoisted ahead of the plan so the plan can show
    the tip the push would really send. If building the plan then raises,
    `_confirm_plan_if_buildable` discards the plan — and the fetch-failure
    note it would have carried — but the fetch already ran and failed. That
    error must still reach the user even though no plan was shown.
    """
    from jailbee.incus import IncusError

    cfg_mock = _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-only")],
        picked=None,
        action="plain",
    )
    _wire_confirm(cfg_mock)
    mocker.patch(
        "jailbee.sync.plan_push",
        side_effect=IncusError("incus list failed"),
    )
    mocker.patch(
        "jailbee.sync.prefetch_push_source",
        return_value=(False, "fatal: could not read from remote"),
    )
    # Source resolves to the origin-tracking ref (not the refs/heads/<source>
    # fallback) so the warning's own noise gate — mirroring plan_push's — lets
    # it through: this is the "it DID matter" case, not the stacked-PR one.
    mocker.patch("jailbee.git.local_branch_exists", return_value=False)
    mocker.patch("jailbee.git.remote_ref_exists", return_value=True)
    do_push = mocker.patch("jailbee.cli._do_single_push", return_value="pushed")

    result = CliRunner().invoke(app, ["git", "push"])

    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "could not read from remote" in combined
    do_push.assert_called_once()
    # The hoisted fetch ran once; the push itself must not fetch again.
    assert do_push.call_args.kwargs["fetch"] is False


def test_push_unbuildable_plan_stays_quiet_when_source_is_local_only(mocker, tmp_path):
    """The M2 warning is gated the same way plan_push gates its own note
    (M1): a failed origin fetch is noise when the source resolves to
    refs/heads/<source> (not on origin at all, the normal stacked-PR case),
    since it had no bearing on what the push will send.
    """
    from jailbee.incus import IncusError

    cfg_mock = _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-only")],
        picked=None,
        action="plain",
    )
    _wire_confirm(cfg_mock)
    mocker.patch(
        "jailbee.sync.plan_push",
        side_effect=IncusError("incus list failed"),
    )
    mocker.patch(
        "jailbee.sync.prefetch_push_source",
        return_value=(False, "fatal: could not read from remote"),
    )
    mocker.patch("jailbee.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.git.remote_ref_exists", return_value=False)
    do_push = mocker.patch("jailbee.cli._do_single_push", return_value="pushed")

    result = CliRunner().invoke(app, ["git", "push"])

    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "could not read from remote" not in combined
    do_push.assert_called_once()


def test_push_picker_selection_is_not_confirmed(mocker, tmp_path):
    cfg_mock = _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-a"), _info("myrepo-feat-b")],
        picked=["myrepo-feat-a", "myrepo-feat-b"],
        action="plain",
    )
    _wire_confirm(cfg_mock)
    plan_push = mocker.patch("jailbee.sync.plan_push")
    mocker.patch("jailbee.cli._do_single_push", return_value="pushed")

    result = CliRunner().invoke(app, ["git", "push"])

    assert result.exit_code == 0
    plan_push.assert_not_called()
