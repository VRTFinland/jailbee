"""CLI tests for multi-select `gie git push`."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.lifecycle import ContainerInfo
from tests.conftest import panel_text


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
    cfg_mock.push.ff = "auto"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=containers,
    )
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch(
        "jailbee.tui.pick_containers_multi",
        return_value=picked,
    )
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")

    def _short(_cfg, full: str) -> str:
        return full.removeprefix("myrepo-")

    mocker.patch("jailbee.lifecycle.short_name", side_effect=_short)
    return cfg_mock


def test_push_pr_without_name_picks_only_running_clone_pr_containers(mocker, tmp_path):
    from jailbee.pr import FetchResult, PrInfo
    from jailbee.sync import PushResult

    _wire(
        mocker,
        tmp_path,
        containers=[
            _info("myrepo-ordinary"),
            _info("myrepo-pr-one"),
            _info("myrepo-pr-stopped", state="Stopped"),
            _info("myrepo-pr-mounted", mode="mount"),
            _info("myrepo-pr-bad-label"),
            _info("myrepo-pr-two"),
        ],
        picked=None,
    )
    incus = mocker.patch("jailbee.incus.Incus").return_value

    def label(name, key):
        labels = {
            "myrepo-pr-one": ("21", "feat/one"),
            "myrepo-pr-stopped": ("22", "feat/stopped"),
            "myrepo-pr-mounted": ("23", "feat/mounted"),
            "myrepo-pr-bad-label": ("invalid", "feat/bad"),
            "myrepo-pr-two": ("42", "feat/two"),
        }
        pr, branch = labels.get(name, (None, None))
        return {"user.jailbee.pr": pr, "user.jailbee.branch": branch}.get(key)

    incus.config_get.side_effect = label
    picked = mocker.patch("jailbee.tui.pick_container", return_value="myrepo-pr-two")
    mocker.patch(
        "jailbee.pr.resolve_pr",
        return_value=PrInfo(
            number=42, head_ref="feat/two", head_sha="sha", state="OPEN", base_ref="main"
        ),
    )
    mocker.patch(
        "jailbee.pr.fetch_pr_head",
        return_value=FetchResult(
            updated=True, prev_sha=None, new_sha="sha", ref="refs/jailbee/pr/42/head"
        ),
    )
    pushed = mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=PushResult(
            source="feat/two",
            source_ref="refs/jailbee/pr/42/head",
            container_ref="refs/jailbee/host/feat/two",
            old_oid=None,
            new_oid="sha",
        ),
    )

    result = CliRunner().invoke(app, ["push", "--pr"])

    assert result.exit_code == 0, result.output
    assert [c.name for c in picked.call_args.args[0]] == ["myrepo-pr-one", "myrepo-pr-two"]
    assert pushed.call_args.args[2] == "pr-two"
    assert pushed.call_args.kwargs["source_ref"] == "refs/jailbee/pr/42/head"


def test_push_pr_without_name_uses_only_eligible_container_without_picker(mocker, tmp_path):
    _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-ordinary"), _info("myrepo-pr-one")],
        picked=None,
    )
    incus = mocker.patch("jailbee.incus.Incus").return_value
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.pr": "21" if name == "myrepo-pr-one" else None,
        "user.jailbee.branch": "feat/one" if name == "myrepo-pr-one" else None,
    }.get(key)
    pick = mocker.patch("jailbee.tui.pick_container")
    refresh = mocker.patch(
        "jailbee.cli._refresh_pr_source", return_value=("feat/one", "refs/jailbee/pr/21/head")
    )
    pushed = mocker.patch("jailbee.cli._do_single_push")

    result = CliRunner().invoke(app, ["git", "push", "--pr"])

    assert result.exit_code == 0, result.output
    assert "Only one eligible PR container" in result.output
    pick.assert_not_called()
    assert refresh.call_args.args[2] == "myrepo-pr-one"
    assert pushed.call_args.args[2] == "pr-one"
    assert pushed.call_args.kwargs["source_ref"] == "refs/jailbee/pr/21/head"


def test_push_pr_without_name_reports_when_no_eligible_pr_containers(mocker, tmp_path):
    _wire(mocker, tmp_path, containers=[_info("myrepo-ordinary")], picked=None)
    incus = mocker.patch("jailbee.incus.Incus").return_value
    incus.config_get.return_value = None
    refresh = mocker.patch("jailbee.cli._refresh_pr_source")

    result = CliRunner().invoke(app, ["git", "push", "--pr"])

    assert result.exit_code == 2
    assert "No running clone-mode PR containers" in panel_text(result.output)
    refresh.assert_not_called()


@pytest.mark.parametrize("pr_label", ["0", "-12"])
def test_push_pr_without_name_excludes_nonpositive_pr_numbers(mocker, tmp_path, pr_label):
    _wire(mocker, tmp_path, containers=[_info("myrepo-invalid")], picked=None)
    incus = mocker.patch("jailbee.incus.Incus").return_value
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.pr": pr_label,
        "user.jailbee.branch": "feat/invalid",
    }.get(key)
    refresh = mocker.patch("jailbee.cli._refresh_pr_source")

    result = CliRunner().invoke(app, ["git", "push", "--pr"])

    assert result.exit_code == 2
    assert "No running clone-mode PR containers" in panel_text(result.output)
    refresh.assert_not_called()


def test_push_pr_without_name_cancel_does_not_fetch_or_push(mocker, tmp_path):
    _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-pr-one"), _info("myrepo-pr-two")],
        picked=None,
    )
    incus = mocker.patch("jailbee.incus.Incus").return_value
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.pr": "21" if name == "myrepo-pr-one" else "42",
        "user.jailbee.branch": "feat/one" if name == "myrepo-pr-one" else "feat/two",
    }.get(key)
    mocker.patch("jailbee.tui.pick_container", return_value=None)
    refresh = mocker.patch("jailbee.cli._refresh_pr_source")

    result = CliRunner().invoke(app, ["git", "push", "--pr"])

    assert result.exit_code == 1
    assert "cancelled" in result.output
    refresh.assert_not_called()


@pytest.mark.parametrize("several", [False, True])
def test_push_pr_without_name_off_a_tty_exits_2_naming_candidates(mocker, tmp_path, several):
    names = ["myrepo-pr-one", "myrepo-pr-two"][: 2 if several else 1]
    _wire(mocker, tmp_path, containers=[_info(n) for n in names], picked=None)
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    incus = mocker.patch("jailbee.incus.Incus").return_value
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.pr": "21",
        "user.jailbee.branch": "feat/x",
    }.get(key)
    pick = mocker.patch("jailbee.tui.pick_container")
    refresh = mocker.patch("jailbee.cli._refresh_pr_source")

    result = CliRunner().invoke(app, ["push", "--pr"])

    assert result.exit_code == 2
    text = panel_text(result.output)
    assert "PR container" in text
    assert "pr-one" in text
    pick.assert_not_called()
    refresh.assert_not_called()


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

    assert result.exit_code == 1
    assert "cancelled" in panel_text(result.stdout + (result.stderr or ""))
    do_push.assert_not_called()


def test_push_without_name_off_a_tty_lists_the_candidates(mocker, tmp_path):
    _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-a"), _info("myrepo-feat-b"), _info("myrepo-mount", "mount")],
        picked=None,
    )
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    picker = mocker.patch("jailbee.tui.pick_containers_multi")
    do_push = mocker.patch("jailbee.cli._do_single_push")

    result = CliRunner().invoke(app, ["git", "push"])

    assert result.exit_code == 2
    combined = panel_text(result.stdout + (result.stderr or ""))
    assert "Candidates: feat-a, feat-b" in combined
    assert "mount" not in combined.split("Candidates:")[1]
    picker.assert_not_called()
    do_push.assert_not_called()


def test_push_without_name_off_a_tty_never_auto_takes_a_single_container(mocker, tmp_path):
    _wire(mocker, tmp_path, containers=[_info("myrepo-feat-a")], picked=None)
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    do_push = mocker.patch("jailbee.cli._do_single_push")

    result = CliRunner().invoke(app, ["git", "push"])

    assert result.exit_code == 2
    assert "Candidates: feat-a" in panel_text(result.stdout + (result.stderr or ""))
    do_push.assert_not_called()


def test_push_without_name_and_no_pushable_container_is_a_missing_value(mocker, tmp_path):
    _wire(mocker, tmp_path, containers=[_info("myrepo-mount", "mount")], picked=None)
    do_push = mocker.patch("jailbee.cli._do_single_push")

    result = CliRunner().invoke(app, ["git", "push"])

    assert result.exit_code == 2
    assert "No pushable containers" in panel_text(result.stdout + (result.stderr or ""))
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


# --- push.ff resolution -----------------------------------------------------


@pytest.mark.parametrize(
    ("cfg_ff", "flag", "expected_no_ff"),
    [
        ("auto", None, None),
        ("never", None, True),
        ("always", None, False),
        ("auto", True, False),  # --ff
        ("auto", False, True),  # --no-ff
    ],
)
def test_push_resolves_ff_config_into_the_no_ff_tristate(
    mocker, tmp_path, cfg_ff, flag, expected_no_ff
):
    """`push.ff` decides `no_ff` only when neither flag is given; `--ff`/
    `--no-ff` always win over it (the last two rows).

    Single auto-selected container (the same setup as
    `test_push_config_off_means_no_plan`), so this reuses `_wire` /
    `_wire_confirm` rather than adding a new mocking surface for the
    named-container path.
    """
    cfg_mock = _wire(
        mocker,
        tmp_path,
        containers=[_info("myrepo-feat-only")],
        picked=None,
        action="merge",
    )
    _wire_confirm(cfg_mock, auto_target=False)
    cfg_mock.push.ff = cfg_ff
    do_push = mocker.patch("jailbee.cli._do_single_push", return_value="pushed")

    argv = ["git", "push"]
    if flag is True:
        argv.append("--ff")
    elif flag is False:
        argv.append("--no-ff")

    result = CliRunner().invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert do_push.call_args.kwargs["no_ff"] is expected_no_ff
