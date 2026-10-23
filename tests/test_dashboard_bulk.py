"""The bulk core: which marked containers take a verb, and how it runs."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.dashboard import bulk
from jailbee.dashboard import model as dmodel
from jailbee.dashboard.jobs import JobResult
from jailbee.dashboard.overlays import PickerEntry
from jailbee.destroy_guard import RiskSummary
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


def test_net_loose_extends_a_container_already_loose(tmp_path):
    loose = ci("alpha-b", "alpha")
    loose.network = "loose"
    groups = [_group(tmp_path, ci("alpha-a", "alpha"), loose)]

    action = bulk.plan_bulk(groups, ["alpha-a", "alpha-b"], "net loose")

    assert action.eligible == ("alpha-a", "alpha-b")
    assert action.skipped == ()


def test_net_strict_skips_a_container_already_strict(tmp_path):
    loose = ci("alpha-b", "alpha")
    loose.network = "loose"
    groups = [_group(tmp_path, ci("alpha-a", "alpha"), loose)]

    action = bulk.plan_bulk(groups, ["alpha-a", "alpha-b"], "net strict")

    assert action.eligible == ("alpha-b",)
    assert action.skipped == (("alpha-a", "already strict"),)


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


def test_orphans_take_orphan_destroy_when_the_frontend_allows_it(tmp_path):
    orphan = dmodel.RepoGroup("ghost", None, None, [ci("ghost-a", "ghost"), ci("ghost-b", "ghost")])
    repo = _group(tmp_path, ci("alpha-a", "alpha"))

    plan = bulk.plan_bulk(
        [orphan, repo], ["ghost-a", "ghost-b", "alpha-a"], "orphan destroy", orphans=True
    )
    offered = [a.verb for a in bulk.bulk_actions([orphan], ["ghost-a"], orphans=True)]

    assert plan.eligible == ("ghost-a", "ghost-b")
    assert plan.skipped == (("alpha-a", "not an orphan"),)
    assert offered == ["orphan destroy"]
    # The Qt window never passes `orphans`: orphans stay view-only there.
    assert bulk.bulk_actions([orphan], ["ghost-a"]) == []


def test_orphan_destroy_is_parallel_forced_and_asks_for_itself():
    assert bulk.BulkAction("orphan destroy", ("ghost-a",)).mode == "parallel"
    assert bulk.bulk_argv("orphan destroy", "ghost-a") == [
        "orphan",
        "destroy",
        "ghost-a",
        "--force",
    ]
    assert not bulk.needs_confirm(bulk.BulkAction("orphan destroy", ("a", "b")))


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
        "merge",
        "git push",
        "git pull",
        "start",
        "restart",
        "stop",
        "net loose",
        "destroy",
    ]


def test_clear_name_is_parallel_and_skips_unaliased_containers(tmp_path):
    groups = [
        _group(
            tmp_path,
            replace(ci("alpha-a", "alpha"), alias="first"),
            replace(ci("alpha-b", "alpha", "Stopped"), alias="second"),
            ci("alpha-c", "alpha", "Stopped"),
        )
    ]
    names = ["alpha-a", "alpha-b", "alpha-c"]

    action = bulk.plan_bulk(groups, names, "rename --clear")

    assert action.eligible == ("alpha-a", "alpha-b")
    assert action.skipped == (("alpha-c", "no alias"),)
    assert action.mode == "parallel"
    assert action.label == "Clear name (2)"
    verbs = [a.verb for a in bulk.bulk_actions(groups, names)]
    assert verbs[0] == "merge"
    assert verbs[-2:] == ["rename --clear", "destroy"]
    assert bulk.bulk_argv(action.verb, "alpha-a") == ["rename", "--clear", "alpha-a"]


def test_bulk_argv_forces_destroy_and_appends_answers():
    assert bulk.bulk_argv("stop", "alpha-a") == ["stop", "alpha-a"]
    assert bulk.bulk_argv("start", "alpha-a") == ["start", "alpha-a", "--background"]
    assert bulk.bulk_argv("restart", "alpha-a") == ["restart", "alpha-a", "--background"]
    assert bulk.bulk_argv("destroy", "alpha-a") == ["destroy", "alpha-a", "--force", "--background"]
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
        ("alpha", ("merge", "alpha-b", "alpha-a", "--confirm"), ("alpha-b", "alpha-a")),
        ("beta", ("merge", "beta-a", "--confirm"), ("beta-a",)),
    ]
    assert runs[1].target.flags() == ["--config", "/b/c.yaml"]


def test_nothing_to_do_names_every_reason(tmp_path):
    action = bulk.BulkAction("stop", (), (("alpha-a", "already stopped"), ("alpha-b", "gone")))

    assert (
        bulk.nothing_to_do(action)
        == "Stop: nothing to do (alpha-a: already stopped; alpha-b: gone)"
    )


def test_a_batch_summarises_ok_failed_and_skipped():
    batch = bulk.BulkBatch("stop", pending={"a", "b"}, skipped={"c": "already stopped"})
    batch.finish("a", JobResult(0, ""))
    assert not batch.done
    batch.finish("b", JobResult(1, "warn\nboom\n"))

    assert batch.done
    assert batch.ok == ["a"]
    assert batch.summary() == "stop: 1 ok, 1 failed (b: boom), 1 skipped (c: already stopped)"


def test_a_silent_failure_reports_its_exit_code():
    batch = bulk.BulkBatch("start", pending={"a"})
    batch.finish("a", JobResult(3, ""))

    assert batch.summary() == "start: 0 ok, 1 failed (a: exited 3)"


def test_a_child_that_wanted_a_terminal_says_so():
    batch = bulk.BulkBatch("stop", pending={"a"})
    batch.finish("a", JobResult(1, "no terminal to ask on"))

    assert batch.failed == {"a": "needs a terminal; run it from its own menu"}


def test_destroy_risk_lines_assess_each_probed_container_with_its_repo_config(tmp_path, mocker):
    dirty = ci(
        "alpha-a",
        "alpha",
        git_status=GitStatus(wt="+1 -0", ahead_diff="clean", ahead_count="0", conflict="ok"),
    )
    unprobed = ci("alpha-b", "alpha")
    groups = [_group(tmp_path, dirty, unprobed)]
    load = mocker.patch("jailbee.config.load_repo_config", return_value=mocker.Mock())
    mocker.patch(
        "jailbee.destroy_guard.assess",
        side_effect=lambda _cfg, c: RiskSummary(c.name, ("uncommitted changes",)),
    )

    lines = bulk.destroy_risk_lines(groups, ["alpha-a", "alpha-b"])

    load.assert_called_once_with(tmp_path)
    assert lines[0] == "⚠ alpha-a: uncommitted changes"
    assert "git status unknown for: " in lines[1] and "alpha-b" in lines[1]


def test_destroy_risk_lines_survive_an_unreadable_config(tmp_path, mocker):
    dirty = ci(
        "alpha-a",
        "alpha",
        git_status=GitStatus(wt="+1 -0", ahead_diff="clean", ahead_count="0", conflict="ok"),
    )
    mocker.patch("jailbee.config.load_repo_config", side_effect=ValueError("broken"))

    assert bulk.destroy_risk_lines([_group(tmp_path, dirty)], ["alpha-a"]) == ()


def test_the_loose_default_is_the_first_repo_with_a_revert_policy(tmp_path):
    off = dmodel.RepoGroup(
        "alpha", str(tmp_path), None, [ci("alpha-a", "alpha")], loose_ttl_default=None
    )
    on = dmodel.RepoGroup(
        "beta", str(tmp_path), None, [ci("beta-a", "beta")], loose_ttl_default="30m"
    )

    assert bulk.bulk_loose_default([off, on], ["alpha-a", "beta-a"]) == "30m"
    assert bulk.bulk_loose_default([off], ["alpha-a"]) is None


def test_loose_ttl_entries_lead_with_the_default_and_end_with_never():
    entries = bulk.ttl_entries("45m")

    assert entries[0] == PickerEntry("45m", "45m")
    assert entries[-1] == PickerEntry("never (no auto-revert)", "never")
    assert [e.value for e in bulk.ttl_entries("1h")].count("1h") == 1


def test_a_busy_container_is_skipped_by_every_verb(tmp_path):
    groups = [_group(tmp_path, ci("alpha-a", "alpha"), ci("alpha-b", "alpha"))]
    busy = frozenset({"alpha-a"})

    action = bulk.plan_bulk(groups, ["alpha-a", "alpha-b"], "destroy", busy=busy)
    offered = bulk.bulk_actions(groups, ["alpha-a"], busy=busy)

    assert action.eligible == ("alpha-b",)
    assert action.skipped == (("alpha-a", "busy"),)
    assert offered == []


def test_foreground_git_verbs_tell_the_cli_the_dashboard_already_asked(tmp_path):
    alpha = _group(tmp_path / "a", ci("alpha-a", "alpha"), ci("alpha-b", "alpha"))

    for verb in ("git push", "git pull"):
        (run,) = bulk.foreground_runs([alpha], bulk.BulkAction(verb, ("alpha-a", "alpha-b")))
        assert run.argv == (*verb.split(), "alpha-a", "alpha-b", "--no-confirm")


def test_merge_tells_the_cli_to_ask_once_the_targets_are_picked(tmp_path):
    alpha = _group(tmp_path / "a", ci("alpha-a", "alpha"), ci("alpha-b", "alpha"))

    (run,) = bulk.foreground_runs([alpha], bulk.BulkAction("merge", ("alpha-a", "alpha-b")))

    assert run.argv == ("merge", "alpha-a", "alpha-b", "--confirm")


def test_needs_confirm_is_several_containers_and_no_question_of_its_own():
    two = ("alpha-a", "alpha-b")

    assert bulk.needs_confirm(bulk.BulkAction("stop", two))
    assert not bulk.needs_confirm(bulk.BulkAction("stop", ("alpha-a",)))
    assert not bulk.needs_confirm(bulk.BulkAction("destroy", two))
    assert not bulk.needs_confirm(bulk.BulkAction("merge", two))


def test_a_pull_question_says_it_writes_to_the_host_repo():
    action = bulk.BulkAction("git pull", ("alpha-a", "alpha-b"), (("alpha-c", "mount mode"),))

    assert bulk.confirm_detail(action) == (
        "Merges each container's commits into the host repo.",
        "skipped alpha-c: mount mode",
    )
    assert bulk.confirm_question(action) == (
        "Send commits to host (git pull): 2 containers — alpha-a, alpha-b?"
    )


def _mount_group(tmp_path, *containers, kinds=("aws", "gcp"), ttl=None):
    return dmodel.RepoGroup(
        "alpha",
        str(tmp_path),
        None,
        list(containers),
        optional_mounts=kinds,
        mount_ttl_defaults=ttl or {},
    )


def _attached(name, *kinds, state="Running"):
    return replace(ci(name, "alpha", state), optional_mounts=kinds)


def test_bulk_mounts_offer_each_kind_in_both_directions_with_counts(tmp_path):
    groups = [
        _mount_group(
            tmp_path, _attached("alpha-a"), _attached("alpha-b", "aws"), _attached("alpha-c")
        )
    ]

    plans = bulk.bulk_mounts(groups, ["alpha-a", "alpha-b", "alpha-c"])

    assert [(p.kind, p.remove, p.eligible) for p in plans] == [
        ("aws", False, ("alpha-a", "alpha-b", "alpha-c")),
        ("aws", True, ("alpha-b",)),
        ("gcp", False, ("alpha-a", "alpha-b", "alpha-c")),
    ]
    assert [p.label for p in plans] == [
        "○ aws — mount (2), renew TTL (1)",
        "● aws — unmount (1)",
        "○ gcp — mount (3)",
    ]


def test_bulk_mount_over_only_attached_containers_is_a_renewal(tmp_path):
    groups = [_mount_group(tmp_path, _attached("alpha-a", "aws"), kinds=("aws",))]

    (mount, _unmount) = bulk.bulk_mounts(groups, ["alpha-a"])

    assert mount.label == "● aws — renew TTL (1)"
    assert mount.argv("alpha-a", "2h") == ["mount", "--for", "2h", "--", "aws", "alpha-a"]


def test_bulk_mount_skips_what_cannot_take_it_with_reasons(tmp_path):
    other = dmodel.RepoGroup(
        "beta", str(tmp_path), None, [ci("beta-a", "beta")], optional_mounts=("gcp",)
    )
    groups = [
        _mount_group(
            tmp_path,
            _attached("alpha-a"),
            _attached("alpha-b", state="Starting"),
            _attached("alpha-c"),
        ),
        other,
    ]

    plan = bulk.plan_bulk_mount(
        groups,
        ["alpha-a", "alpha-b", "alpha-c", "beta-a", "gone"],
        "aws",
        remove=False,
        busy=frozenset({"alpha-c"}),
    )

    assert plan.eligible == ("alpha-a",)
    assert plan.skipped == (
        ("alpha-b", "starting"),
        ("alpha-c", "busy"),
        ("beta-a", "no 'aws' mount configured"),
        ("gone", "gone"),
    )


def test_bulk_unmount_skips_a_container_without_the_kind_attached(tmp_path):
    groups = [_mount_group(tmp_path, _attached("alpha-a", "aws"), _attached("alpha-b"))]

    plan = bulk.plan_bulk_mount(groups, ["alpha-a", "alpha-b"], "aws", remove=True)

    assert plan.eligible == ("alpha-a",)
    assert plan.skipped == (("alpha-b", "not mounted"),)
    assert plan.argv("alpha-a", None) == ["unmount", "--", "aws", "alpha-a"]


def test_bulk_mount_follows_the_ssh_policy(tmp_path):
    groups = [_mount_group(tmp_path, _attached("alpha-a", "aws"), kinds=("aws",))]
    policy = RemoteSSHConfig.model_validate({})

    plans = bulk.bulk_mounts(groups, ["alpha-a"], ssh_policy=policy, over_ssh=True)

    # `mount` is a host command the default policy refuses; `unmount` stays
    assert [(p.kind, p.remove) for p in plans] == [("aws", True)]


def test_bulk_mount_ttl_default_is_the_first_takers_repo_default(tmp_path):
    groups = [
        dmodel.RepoGroup(
            "beta", str(tmp_path), None, [ci("beta-a", "beta")], optional_mounts=("aws",)
        ),
        _mount_group(tmp_path, _attached("alpha-a"), ttl={"aws": "4h"}),
    ]

    plan = bulk.plan_bulk_mount(groups, ["beta-a", "alpha-a"], "aws", remove=False)

    assert plan.ttl_default(groups) == "4h"
    assert bulk.plan_bulk_mount(groups, ["beta-a"], "aws", remove=False).ttl_default(groups) is None


def test_a_renew_only_mount_asks_about_renewing(tmp_path):
    groups = [
        _mount_group(
            tmp_path, _attached("alpha-a", "aws"), _attached("alpha-b", "aws"), kinds=("aws",)
        )
    ]

    plan = bulk.plan_bulk_mount(groups, ["alpha-a", "alpha-b"], "aws", remove=False)

    assert bulk.mount_confirm_question(plan) == (
        "Renew TTL of 'aws': 2 containers — alpha-a, alpha-b?"
    )


def test_a_kind_not_remote_mountable_says_so_in_a_restricted_session(tmp_path, monkeypatch):
    monkeypatch.setenv("JAILBEE_REMOTE_SSH", "1")
    monkeypatch.setenv("JAILBEE_SSH_EXCLUDED_REPOS", "[]")
    group = replace(_mount_group(tmp_path, _attached("alpha-a")), remote_mountable=("gcp",))
    policy = RemoteSSHConfig.model_validate({"mounts": True})

    plan = bulk.plan_bulk_mount(
        [group], ["alpha-a"], "aws", remove=False, ssh_policy=policy, over_ssh=True
    )

    assert plan.skipped == (("alpha-a", "not mountable over SSH"),)
    assert bulk.plan_bulk_mount(
        [group], ["alpha-a"], "gcp", remove=False, ssh_policy=policy, over_ssh=True
    ).eligible == ("alpha-a",)
