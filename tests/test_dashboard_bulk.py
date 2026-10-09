"""The bulk core: which marked containers take a verb, and how it runs."""

from __future__ import annotations

from pathlib import Path

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.dashboard import bulk
from jailbee.dashboard import model as dmodel
from jailbee.git_status import GitStatus
from jailbee.remote_ssh.router import RouteError
from tests.dashboard_fixtures import ci


def _group(tmp_path, *containers, prefix="alpha", config=None):
    return dmodel.RepoGroup(prefix, str(tmp_path), config, list(containers))


def test_stop_takes_the_running_and_skips_the_stopped(tmp_path):
    groups = [_group(tmp_path, ci("alpha-a", "alpha"), ci("alpha-b", "alpha", "Stopped"))]

    action = bulk.plan_bulk(groups, ["alpha-a", "alpha-b"], "stop")

    assert action.eligible == ("alpha-a",)
    assert action.skipped == (("alpha-b", "already stopped"),)
    assert action.mode == "parallel"
    assert action.label == "Stop (1)"


def test_start_skips_a_running_container(tmp_path):
    groups = [_group(tmp_path, ci("alpha-a", "alpha"), ci("alpha-b", "alpha", "Stopped"))]

    action = bulk.plan_bulk(groups, ["alpha-a", "alpha-b"], "start")

    assert action.eligible == ("alpha-b",)
    assert action.skipped == (("alpha-a", "already running"),)


def test_net_loose_skips_a_container_already_loose(tmp_path):
    loose = ci("alpha-b", "alpha")
    loose.network = "loose"
    groups = [_group(tmp_path, ci("alpha-a", "alpha"), loose)]

    action = bulk.plan_bulk(groups, ["alpha-a", "alpha-b"], "net loose")

    assert action.eligible == ("alpha-a",)
    assert action.skipped == (("alpha-b", "already loose"),)


def test_git_verbs_skip_mount_mode_and_run_in_the_foreground(tmp_path):
    groups = [_group(tmp_path, ci("alpha-a", "alpha"), ci("alpha-b", "alpha", mode="mount"))]

    action = bulk.plan_bulk(groups, ["alpha-a", "alpha-b"], "git push")

    assert action.eligible == ("alpha-a",)
    assert action.skipped == (("alpha-b", "mount mode"),)
    assert action.mode == "foreground"


def test_git_pull_skips_a_container_with_no_commits_for_the_host(tmp_path):
    idle = ci(
        "alpha-b",
        "alpha",
        git_status=GitStatus(wt="clean", ahead_diff="clean", ahead_count="0", conflict="ok"),
    )
    groups = [_group(tmp_path, ci("alpha-a", "alpha"), idle)]

    action = bulk.plan_bulk(groups, ["alpha-a", "alpha-b"], "git pull")

    assert action.eligible == ("alpha-a",)
    assert action.skipped == (("alpha-b", "no commits for the host"),)


def test_an_orphan_container_is_view_only(tmp_path):
    orphan = dmodel.RepoGroup("ghost", None, None, [ci("ghost-a", "ghost")])

    action = bulk.plan_bulk([orphan], ["ghost-a"], "stop")

    assert action.eligible == ()
    assert action.skipped == (("ghost-a", "view-only"),)


def test_a_vanished_name_is_skipped_as_gone(tmp_path):
    action = bulk.plan_bulk([_group(tmp_path, ci("alpha-a", "alpha"))], ["alpha-z"], "stop")

    assert action.skipped == (("alpha-z", "gone"),)


def test_a_policy_refusal_is_its_own_skip_reason(tmp_path, mocker):
    groups = [_group(tmp_path, ci("alpha-a", "alpha"))]
    policy = RemoteSSHConfig()
    mocker.patch(
        "jailbee.dashboard.menus.check_dashboard_command",
        side_effect=RouteError("not allowed"),
    )

    action = bulk.plan_bulk(groups, ["alpha-a"], "stop", ssh_policy=policy, over_ssh=True)

    assert action.skipped == (("alpha-a", "not permitted by the SSH policy"),)


def test_bulk_actions_lists_only_what_some_mark_can_take_in_menu_order(tmp_path):
    groups = [_group(tmp_path, ci("alpha-a", "alpha"), ci("alpha-b", "alpha", "Stopped"))]

    verbs = [a.verb for a in bulk.bulk_actions(groups, ["alpha-a", "alpha-b"])]

    assert verbs == [
        "start",
        "stop",
        "restart",
        "net loose",
        "git push",
        "git pull",
        "merge",
        "destroy",
    ]


def test_bulk_argv_forces_destroy_and_appends_answers():
    assert bulk.bulk_argv("stop", "alpha-a") == ["stop", "alpha-a"]
    assert bulk.bulk_argv("destroy", "alpha-a") == ["destroy", "alpha-a", "--force"]
    assert bulk.bulk_argv("net loose", "alpha-a", ("--for", "2h")) == [
        "net",
        "loose",
        "alpha-a",
        "--for",
        "2h",
    ]


def test_foreground_runs_are_one_per_repo_in_listing_order(tmp_path):
    alpha = _group(tmp_path / "a", ci("alpha-a", "alpha"), ci("alpha-b", "alpha"))
    beta = _group(tmp_path / "b", ci("beta-a", "beta"), prefix="beta", config=Path("/b/c.yaml"))
    action = bulk.BulkAction("merge", ("beta-a", "alpha-b", "alpha-a"))

    runs = bulk.foreground_runs([alpha, beta], action)

    assert [(r.prefix, r.argv, r.names) for r in runs] == [
        ("alpha", ("merge", "alpha-b", "alpha-a"), ("alpha-b", "alpha-a")),
        ("beta", ("merge", "beta-a"), ("beta-a",)),
    ]
    assert runs[1].target.flags() == ["--config", "/b/c.yaml"]


def test_nothing_to_do_names_every_reason(tmp_path):
    action = bulk.BulkAction("stop", (), (("alpha-a", "already stopped"), ("alpha-b", "gone")))

    assert (
        bulk.nothing_to_do(action)
        == "Stop: nothing to do (alpha-a: already stopped; alpha-b: gone)"
    )
