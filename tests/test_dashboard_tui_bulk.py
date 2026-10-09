"""Bulk actions in the terminal dashboard."""

from __future__ import annotations

from jailbee.dashboard import model as dmodel
from jailbee.dashboard.bulk import BulkAction, plan_bulk
from jailbee.dashboard.overlays import Picker, PickerEntry
from jailbee.dashboard.tui import session as tsession
from jailbee.dashboard.tui.menu_state import RepoMenuState
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import SyncJobs, bare_session, box_text, patch_pause


def _session(mocker, tmp_path, *states, config=None, **group_kw):
    names = [f"alpha-{c}" for c in "abc"[: len(states)]]
    group = dmodel.RepoGroup(
        "alpha",
        str(tmp_path),
        config,
        [ci(n, "alpha", s) for n, s in zip(names, states, strict=True)],
        **group_kw,
    )
    session, terminal = bare_session(mocker, [group])
    session.jobs = SyncJobs()
    return session, terminal, group


def _children(mocker, *, failing=(), stderr="boom"):
    def run(argv, **_kw):
        proc = mocker.Mock()
        bad = any(name in argv for name in failing)
        proc.returncode = 1 if bad else 0
        proc.stderr = stderr if bad else ""
        return proc

    return mocker.patch.object(tsession.subprocess, "run", side_effect=run)


def _stop(session, *names):
    session.run_bulk(plan_bulk(session.groups, list(names), "stop"))


def test_a_parallel_bulk_starts_one_detached_child_per_container(mocker, tmp_path):
    child = _children(mocker)
    session, terminal, _ = _session(mocker, tmp_path, "Running", "Running")

    _stop(session, "alpha-a", "alpha-b")

    assert [c.args[0] for c in child.call_args_list] == [
        ["jailbee", "stop", "alpha-a"],
        ["jailbee", "stop", "alpha-b"],
    ]
    assert terminal.handed == []  # nothing suspended the dashboard


def test_a_configured_repo_is_addressed_with_its_config(mocker, tmp_path):
    child = _children(mocker)
    config = tmp_path / "c.yaml"
    session, _, _ = _session(mocker, tmp_path, "Running", config=config)

    _stop(session, "alpha-a")

    assert child.call_args.args[0] == ["jailbee", "stop", "alpha-a", "--config", str(config)]
    assert child.call_args.kwargs["cwd"] == tmp_path


def test_rows_show_running_until_their_child_is_delivered(mocker, tmp_path):
    _children(mocker)
    session, _, _ = _session(mocker, tmp_path, "Running", "Running")

    _stop(session, "alpha-a", "alpha-b")

    assert session.view().running == frozenset({"alpha-a", "alpha-b"})
    session.tick()
    assert session.view().running == frozenset()


def test_a_finished_batch_unmarks_the_successes_and_keeps_the_failures(mocker, tmp_path):
    _children(mocker, failing=("alpha-b",))
    session, _, _ = _session(mocker, tmp_path, "Running", "Running")
    session.marked = frozenset({"alpha-a", "alpha-b"})

    _stop(session, "alpha-a", "alpha-b")
    session.tick()

    assert session.marked == frozenset({"alpha-b"})
    assert session.notice == "stop: 1 ok, 1 failed (alpha-b: boom)"


def test_skipped_containers_are_reported_and_stay_marked(mocker, tmp_path):
    child = _children(mocker)
    session, _, _ = _session(mocker, tmp_path, "Running", "Stopped")
    session.marked = frozenset({"alpha-a", "alpha-b"})

    _stop(session, "alpha-a", "alpha-b")
    session.tick()

    assert [c.args[0] for c in child.call_args_list] == [["jailbee", "stop", "alpha-a"]]
    assert session.marked == frozenset({"alpha-b"})
    assert session.notice == "stop: 1 ok, 1 skipped (alpha-b: already stopped)"


def test_destroy_children_carry_force(mocker, tmp_path):
    child = _children(mocker)
    session, _, _ = _session(mocker, tmp_path, "Running")

    session.run_bulk(plan_bulk(session.groups, ["alpha-a"], "destroy"))

    assert child.call_args.args[0] == ["jailbee", "destroy", "alpha-a", "--force"]


def test_a_batch_still_reports_after_its_container_vanished(mocker, tmp_path):
    _children(mocker)
    session, _, group = _session(mocker, tmp_path, "Running", "Running")
    session.marked = frozenset({"alpha-a", "alpha-b"})

    _stop(session, "alpha-a", "alpha-b")
    group.containers.pop(0)  # destroyed elsewhere before the result is read
    session.tick()

    assert session.notice == "stop: 2 ok"
    assert session.marked == frozenset()


def test_a_child_that_cannot_start_fails_only_itself(mocker, tmp_path):
    _children(mocker)
    session, _, _ = _session(mocker, tmp_path, "Running", "Running")
    real_start = session.jobs.start

    def start(key, label, argv, cwd, on_done):
        if "alpha-a" in argv:
            raise OSError("gone")
        real_start(key, label, argv, cwd, on_done)

    session.jobs.start = start  # type: ignore[method-assign]
    _stop(session, "alpha-a", "alpha-b")
    session.tick()

    assert session.notice.startswith("stop: 1 ok, 1 failed (alpha-a: ")


def test_enter_with_marks_opens_the_bulk_picker(mocker, tmp_path):
    session, _, _ = _session(mocker, tmp_path, "Running", "Running")
    session.marked = frozenset({"alpha-a", "alpha-b"})
    session.handle_key("down")

    session.handle_key("enter")

    assert isinstance(session.overlay, Picker)
    assert session.overlay.purpose == "bulk-action"
    assert session.overlay.title == "2 selected"
    assert [e.value for e in session.overlay.entries] == [
        "stop",
        "restart",
        "net loose",
        "git push",
        "git pull",
        "merge",
        "destroy",
    ]


def test_enter_on_a_repo_header_with_marks_still_opens_the_repo_menu(mocker, tmp_path):
    session, _, _ = _session(mocker, tmp_path, "Running")
    session.marked = frozenset({"alpha-a"})

    session.handle_key("enter")  # the cursor starts on the header

    assert isinstance(session.overlay, RepoMenuState)


def test_choosing_stop_in_the_bulk_picker_runs_the_batch(mocker, tmp_path):
    child = _children(mocker)
    session, _, _ = _session(mocker, tmp_path, "Running", "Running")
    session.marked = frozenset({"alpha-a", "alpha-b"})
    session.handle_key("down")
    session.handle_key("enter")

    session.picker_chosen(PickerEntry("Stop (2)", "stop"))

    assert len(child.call_args_list) == 2
    assert session.overlay is None


def test_no_eligible_container_explains_itself(mocker, tmp_path):
    child = _children(mocker)
    session, _, _ = _session(mocker, tmp_path, "Stopped", "Stopped")
    session.marked = frozenset({"alpha-a", "alpha-b"})

    assert session.begin_bulk("stop") is None

    child.assert_not_called()
    assert (
        session.notice == "Stop: nothing to do (alpha-a: already stopped; alpha-b: already stopped)"
    )


def test_capital_d_with_marks_asks_once_for_all(mocker, tmp_path):
    session, _, _ = _session(mocker, tmp_path, "Running", "Running")
    session.marked = frozenset({"alpha-a", "alpha-b"})
    session.handle_key("down")

    session.handle_key("action:destroy")

    overlay = session.overlay
    assert isinstance(overlay, Picker) and overlay.purpose == "bulk-destroy-confirm"
    assert overlay.entries[0].value == "no"
    assert any("git status unknown" in line for line in overlay.detail)


def test_declining_the_bulk_destroy_runs_nothing(mocker, tmp_path):
    child = _children(mocker)
    session, _, _ = _session(mocker, tmp_path, "Running")
    session.marked = frozenset({"alpha-a"})
    session.overlay = session.begin_bulk("destroy")

    session.picker_chosen(PickerEntry("No", "no"))

    child.assert_not_called()
    assert session.notice == "Cancelled"


def test_capital_d_with_marks_acts_on_the_marks_not_the_cursor(mocker, tmp_path):
    child = _children(mocker)
    session, _, _ = _session(mocker, tmp_path, "Running", "Running", "Running")
    session.marked = frozenset({"alpha-a", "alpha-b"})
    for _ in range(3):
        session.handle_key("down")  # the cursor on alpha-c, unmarked
    session.handle_key("action:destroy")

    session.picker_chosen(PickerEntry("Yes, destroy 2", "yes"))

    assert [c.args[0] for c in child.call_args_list] == [
        ["jailbee", "destroy", "alpha-a", "--force"],
        ["jailbee", "destroy", "alpha-b", "--force"],
    ]


def test_capital_d_without_marks_keeps_the_single_destroy(mocker, tmp_path):
    child = _children(mocker)
    mocker.patch("jailbee.dashboard.dispatch._wait_for_return")
    session, terminal, _ = _session(mocker, tmp_path, "Running")
    session.handle_key("down")

    session.handle_key("action:destroy")

    assert len(terminal.handed) == 1
    assert child.call_args.args[0] == ["jailbee", "destroy", "alpha-a"]


def test_bulk_loose_asks_the_ttl_once_and_passes_it(mocker, tmp_path):
    child = _children(mocker)
    session, _, _ = _session(mocker, tmp_path, "Running", "Running", loose_ttl_default="5m")
    session.marked = frozenset({"alpha-a", "alpha-b"})

    session.overlay = session.begin_bulk("net loose")
    assert isinstance(session.overlay, Picker) and session.overlay.purpose == "bulk-loose-ttl"
    assert session.overlay.entries[0].value == "5m"
    session.picker_chosen(PickerEntry("2h", "2h"))

    assert [c.args[0][-2:] for c in child.call_args_list] == [["--for", "2h"], ["--for", "2h"]]


def test_bulk_loose_without_a_revert_policy_asks_nothing(mocker, tmp_path):
    child = _children(mocker)
    session, _, _ = _session(mocker, tmp_path, "Running", loose_ttl_default=None)
    session.marked = frozenset({"alpha-a"})

    assert session.begin_bulk("net loose") is None

    assert child.call_args.args[0] == ["jailbee", "net", "loose", "alpha-a"]


def test_every_marked_container_ineligible_for_the_picker_says_so(mocker, tmp_path):
    session, _, _ = _session(mocker, tmp_path, "Running")
    session.marked = frozenset({"gone"})
    session.handle_key("down")

    session.handle_key("enter")

    assert session.overlay is None
    assert session.notice == "No action applies to the 1 marked container"


def test_u_with_marks_plans_git_push_over_the_marks_not_the_cursor(mocker, tmp_path):
    session, _, _ = _session(mocker, tmp_path, "Running", "Running", "Running")
    session.marked = frozenset({"alpha-a", "alpha-b"})
    for _ in range(3):
        session.handle_key("down")  # the cursor on alpha-c, unmarked
    run = mocker.patch.object(session, "run_bulk")

    session.handle_key("action:push")

    run.assert_called_once()
    action = run.call_args.args[0]
    assert action.verb == "git push"
    assert action.eligible == ("alpha-a", "alpha-b")


def test_u_with_mixed_marks_plans_only_the_eligible(mocker, tmp_path):
    group = dmodel.RepoGroup(
        "alpha",
        str(tmp_path),
        None,
        [ci("alpha-a", "alpha", "Running"), ci("alpha-m", "alpha", "Running", mode="mount")],
    )
    session, _ = bare_session(mocker, [group])
    session.marked = frozenset({"alpha-a", "alpha-m"})
    run = mocker.patch.object(session, "run_bulk")

    session.handle_key("action:push")

    action = run.call_args.args[0]
    assert action.eligible == ("alpha-a",)
    assert [name for name, _ in action.skipped] == ["alpha-m"]


def test_a_long_risk_list_is_capped_with_a_remainder_line():
    lines = tuple(f"⚠ c{i}: dirty" for i in range(10))

    capped = tsession._cap_detail(lines)

    assert capped[:4] == lines[:4]
    assert capped[-1] == "…and 6 more"
    assert len(capped) == 5


def test_the_destroy_confirm_with_nothing_left_runs_nothing(mocker, tmp_path):
    session, _, group = _session(mocker, tmp_path, "Running")
    session.marked = frozenset({"alpha-a"})
    session.overlay = session.begin_bulk("destroy")
    group.containers.clear()  # it vanished under the open confirm
    run = mocker.patch.object(session, "run_bulk")

    session.picker_chosen(PickerEntry("Yes, destroy 1", "yes"))

    run.assert_not_called()
    assert session.notice


def test_many_risky_marks_cap_the_destroy_confirm_and_keep_its_entries(mocker, tmp_path):
    session, _, _ = _session(mocker, tmp_path, "Running", "Running", "Running")
    names = [f"alpha-{i}" for i in range(9)]
    mocker.patch.object(
        tsession,
        "destroy_risk_lines",
        return_value=tuple(f"⚠ {n}: dirty" for n in names),
    )
    mocker.patch.object(session, "_plan", return_value=BulkAction("destroy", tuple(names)))

    picker = session.begin_bulk("destroy")

    assert isinstance(picker, Picker) and picker.purpose == "bulk-destroy-confirm"
    assert len(picker.detail) == 5
    assert picker.detail[:4] == tuple(f"⚠ {n}: dirty" for n in names[:4])
    assert picker.detail[4] == "…and 5 more"
    text = "\n".join(box_text(picker, size=(100, 12)))
    assert "No" in text
    assert "Yes, destroy 9" in text


def test_cap_detail_boundaries():
    four = tuple(str(i) for i in range(4))
    five = tuple(str(i) for i in range(5))

    assert tsession._cap_detail(four) == four
    assert tsession._cap_detail(five) == (*four, "…and 1 more")


def test_the_loose_ttl_submit_with_nothing_left_says_so(mocker, tmp_path):
    session, _, group = _session(mocker, tmp_path, "Running", loose_ttl_default="5m")
    session.marked = frozenset({"alpha-a"})
    session.overlay = session.begin_bulk("net loose")
    group.containers.clear()
    run = mocker.patch.object(session, "run_bulk")

    session.picker_chosen(PickerEntry("2h", "2h"))

    run.assert_not_called()
    assert session.notice


def test_bulk_push_runs_once_in_the_terminal_over_all_names(mocker, tmp_path):
    child = _children(mocker)
    pause = patch_pause(mocker)
    session, terminal, _ = _session(mocker, tmp_path, "Running", "Running")
    session.marked = frozenset({"alpha-a", "alpha-b"})
    session.handle_key("down")

    session.handle_key("action:push")

    assert len(terminal.handed) == 1
    assert [c.args[0] for c in child.call_args_list] == [
        ["jailbee", "git", "push", "alpha-a", "alpha-b"]
    ]
    pause.assert_called_once()


def test_bulk_merge_passes_no_into(mocker, tmp_path):
    child = _children(mocker)
    patch_pause(mocker)
    session, _, _ = _session(mocker, tmp_path, "Running", "Running")
    session.marked = frozenset({"alpha-a", "alpha-b"})

    session.begin_bulk("merge")

    assert child.call_args.args[0] == ["jailbee", "merge", "alpha-a", "alpha-b"]


def test_two_repos_get_one_run_each(mocker, tmp_path):
    child = _children(mocker)
    pause = patch_pause(mocker)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    alpha = dmodel.RepoGroup("alpha", str(tmp_path / "a"), None, [ci("alpha-a", "alpha")])
    beta = dmodel.RepoGroup(
        "beta", str(tmp_path / "b"), tmp_path / "b.yaml", [ci("beta-a", "beta")]
    )
    session, terminal = bare_session(mocker, [alpha, beta])
    session.marked = frozenset({"alpha-a", "beta-a"})

    session.begin_bulk("git pull")

    assert len(terminal.handed) == 1
    calls = [(c.args[0], c.kwargs["cwd"]) for c in child.call_args_list]
    assert calls == [
        (["jailbee", "git", "pull", "alpha-a"], tmp_path / "a"),
        (
            ["jailbee", "git", "pull", "beta-a", "--config", str(tmp_path / "b.yaml")],
            tmp_path / "b",
        ),
    ]
    pause.assert_called_once()


def test_a_successful_run_unmarks_its_names_and_a_failed_one_keeps_them(mocker, tmp_path):
    _children(mocker, failing=("beta-a",))
    patch_pause(mocker)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    alpha = dmodel.RepoGroup("alpha", str(tmp_path / "a"), None, [ci("alpha-a", "alpha")])
    beta = dmodel.RepoGroup("beta", str(tmp_path / "b"), None, [ci("beta-a", "beta")])
    session, _ = bare_session(mocker, [alpha, beta])
    session.marked = frozenset({"alpha-a", "beta-a"})

    session.begin_bulk("git push")

    assert session.marked == frozenset({"beta-a"})
    assert session.notice == "git push: 1 ok, 1 failed (beta: exited 1)"
