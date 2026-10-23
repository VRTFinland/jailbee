"""Bulk actions in the terminal dashboard."""

from __future__ import annotations

import pytest

from jailbee.dashboard import dispatch as ddispatch
from jailbee.dashboard import model as dmodel
from jailbee.dashboard.bulk import BulkAction, plan_bulk
from jailbee.dashboard.overlays import Picker, PickerEntry
from jailbee.dashboard.tui import session as tsession
from jailbee.dashboard.tui.menu_state import RepoMenuState
from jailbee.remote_ssh.router import RouteError
from jailbee.tui import DIAGNOSTICS_ENV
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import SyncJobs, bare_session, box_text, drive, patch_pause


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


def _children(mocker, *, failing=(), warning=(), stderr="boom"):
    def run(argv, **kw):
        if any(name in argv for name in warning):
            with open(kw["env"][DIAGNOSTICS_ENV], "a") as fh:
                fh.write("warn\n")
        proc = mocker.Mock()
        bad = any(name in argv for name in failing)
        proc.returncode = 1 if bad else 0
        proc.stderr = stderr if bad else ""
        return proc

    return mocker.patch.object(tsession.subprocess, "run", side_effect=run)


def _bulk_key(session, letter):
    """The letter typed in the table, then its entry chosen in the picker it opened."""
    assert session.open_menu_for_letter(letter)
    picker = session.overlay
    session.picker_chosen(next(e for e in picker.entries if e.key == letter))


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

    assert child.call_args.args[0] == ["jailbee", "destroy", "alpha-a", "--force", "--background"]


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

    mocker.patch.object(session.jobs, "start", side_effect=start)
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
        "merge",
        "git push",
        "git pull",
        "restart",
        "stop",
        "net loose",
        "destroy",
    ]


def test_the_bulk_picker_gives_every_entry_its_own_letter(mocker, tmp_path):
    session, _, _ = _session(mocker, tmp_path, "Running", "Running")
    session.marked = frozenset({"alpha-a", "alpha-b"})
    session.handle_key("down")

    session.handle_key("enter")

    assert isinstance(session.overlay, Picker)
    keys = {e.value: e.key for e in session.overlay.entries}
    assert keys["git push"] == "u" and keys["destroy"] == "D" and keys["merge"] == "m"
    assert all(keys.values()) and len(set(keys.values())) == len(keys)


def test_bulk_letters_match_the_container_menu_where_the_verbs_do():
    from jailbee.dashboard.bulk import BULK_KEYS, MOUNTS_KEY
    from jailbee.dashboard.tui.menu_state import _MENU_KEYS

    shared = {v: k for v, k in BULK_KEYS.items() if v in _MENU_KEYS}
    assert shared == {v: _MENU_KEYS[v] for v in shared}
    assert {"merge", "git push", "git pull", "restart", "stop", "destroy", "start"} <= set(shared)
    assert len(set(BULK_KEYS.values())) == len(BULK_KEYS)  # a bulk can offer any of them at once
    assert MOUNTS_KEY not in BULK_KEYS.values()


def test_a_bulk_letter_in_the_table_opens_the_bulk_picker(mocker, tmp_path):
    session, _, _ = _session(mocker, tmp_path, "Running", "Running")
    session.marked = frozenset({"alpha-a", "alpha-b"})
    session.handle_key("down")

    assert session.open_menu_for_letter("D")
    assert isinstance(session.overlay, Picker) and session.overlay.purpose == "bulk-action"


def test_a_letter_no_bulk_entry_owns_opens_nothing(mocker, tmp_path):
    session, _, _ = _session(mocker, tmp_path, "Running", "Running")
    session.marked = frozenset({"alpha-a", "alpha-b"})
    session.handle_key("down")

    assert not session.open_menu_for_letter("z")
    assert session.overlay is None and session.notice is None


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
    assert isinstance(session.overlay, Picker) and session.overlay.purpose == "bulk-confirm"
    child.assert_not_called()
    session.picker_chosen(PickerEntry("Yes, 2", "yes"))

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

    _bulk_key(session, "D")

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
    _bulk_key(session, "D")

    session.picker_chosen(PickerEntry("Yes, destroy 2", "yes"))

    assert [c.args[0] for c in child.call_args_list] == [
        ["jailbee", "destroy", "alpha-a", "--force", "--background"],
        ["jailbee", "destroy", "alpha-b", "--force", "--background"],
    ]


def test_capital_d_without_marks_asks_in_the_dashboard_before_destroying(mocker, tmp_path):
    child = _children(mocker)
    mocker.patch("jailbee.dashboard.dispatch._wait_for_return")
    session, terminal, _ = _session(mocker, tmp_path, "Running")
    session.handle_key("down")

    assert session.open_menu_for_letter("l")  # Lifecycle →, then Destroy inside it
    session.menu_chosen("destroy", "Lifecycle →", 2)

    picker = session.overlay
    assert isinstance(picker, Picker) and picker.purpose == "container-destroy-confirm"
    assert [e.value for e in picker.entries] == ["no", "yes"]
    assert terminal.handed == []
    child.assert_not_called()

    session.picker_chosen(picker.entries[1])

    assert child.call_args.args[0] == ["jailbee", "destroy", "alpha-a", "--force", "--background"]


def test_declining_the_single_destroy_runs_nothing(mocker, tmp_path):
    child = _children(mocker)
    session, _terminal, _ = _session(mocker, tmp_path, "Running")
    session.overlay = session.begin_destroy("alpha-a")
    assert isinstance(session.overlay, Picker)

    session.picker_chosen(session.overlay.entries[0])

    child.assert_not_called()
    assert session.notice == "Cancelled"


@pytest.mark.parametrize(
    ("verb", "index", "argv"),
    [
        ("restart", 0, ["jailbee", "restart", "alpha-a", "--background"]),
        ("stop", 1, ["jailbee", "stop", "alpha-a"]),
    ],
)
def test_a_lifecycle_menu_entry_runs_detached_not_in_the_terminal(
    mocker, tmp_path, verb, index, argv
):
    """A shutdown or a boot can take minutes; the dashboard must stay usable.

    Mutation: send these verbs back to `dispatch` and the terminal is handed off.
    """
    child = _children(mocker)
    mocker.patch("jailbee.dashboard.dispatch._wait_for_return")
    session, terminal, _ = _session(mocker, tmp_path, "Running")
    session.handle_key("down")

    assert session.open_menu_for_letter("l")
    session.menu_chosen(verb, "Lifecycle →", index)

    assert terminal.handed == []
    assert [c.args[0] for c in child.call_args_list] == [argv]
    assert session.view().running == frozenset({"alpha-a"})


def test_start_from_the_menu_runs_detached_and_tracked(mocker, tmp_path):
    child = _children(mocker)
    session, terminal, _ = _session(mocker, tmp_path, "Stopped")
    session.handle_key("down")

    assert session.open_menu_for_letter("s")
    session.menu_chosen("start", None, 0)

    assert terminal.handed == []
    assert [c.args[0] for c in child.call_args_list] == [
        ["jailbee", "start", "alpha-a", "--background"]
    ]


def test_bulk_loose_asks_the_ttl_once_and_passes_it(mocker, tmp_path):
    child = _children(mocker)
    session, _, _ = _session(mocker, tmp_path, "Running", "Running", loose_ttl_default="5m")
    session.marked = frozenset({"alpha-a", "alpha-b"})

    session.overlay = session.begin_bulk("net loose", confirmed=True)
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

    _bulk_key(session, "u")
    session.picker_chosen(PickerEntry("Yes, 2", "yes"))

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
    session.handle_key("down")  # off the header, which would open the repo menu
    run = mocker.patch.object(session, "run_bulk")

    _bulk_key(session, "u")

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
    session, _, _ = _session(mocker, tmp_path, "Running")
    session.marked = frozenset({"alpha-a"})
    session.overlay = session.begin_bulk("destroy")
    session.groups[0].containers.clear()  # it vanished under the open confirm
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
    session, _, _ = _session(mocker, tmp_path, "Running", loose_ttl_default="5m")
    session.marked = frozenset({"alpha-a"})
    session.overlay = session.begin_bulk("net loose")
    session.groups[0].containers.clear()
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

    _bulk_key(session, "u")
    session.picker_chosen(PickerEntry("Yes, 2", "yes"))

    assert len(terminal.handed) == 1
    assert [c.args[0] for c in child.call_args_list] == [
        ["jailbee", "git", "push", "alpha-a", "alpha-b", "--no-confirm"]
    ]
    pause.assert_not_called()  # a clean run returns straight to the table


def test_bulk_merge_picks_its_targets_in_the_table_before_running(mocker, tmp_path):
    child = _children(mocker)
    patch_pause(mocker)
    session, terminal, _ = _session(mocker, tmp_path, "Running", "Running", "Running")
    session.marked = frozenset({"alpha-a", "alpha-b"})

    session.begin_bulk("merge")

    child.assert_not_called()
    assert terminal.handed == []
    assert session.merge_pick is not None
    assert session.merge_pick.sources == ("alpha-a", "alpha-b")


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

    session.begin_bulk("git pull", confirmed=True)

    assert len(terminal.handed) == 1
    calls = [(c.args[0], c.kwargs["cwd"]) for c in child.call_args_list]
    assert calls == [
        (["jailbee", "git", "pull", "alpha-a", "--no-confirm"], tmp_path / "a"),
        (
            [
                "jailbee",
                "git",
                "pull",
                "beta-a",
                "--no-confirm",
                "--config",
                str(tmp_path / "b.yaml"),
            ],
            tmp_path / "b",
        ),
    ]
    pause.assert_not_called()


@pytest.mark.parametrize(("failing", "warning"), [(("beta-a",), ()), ((), ("alpha-a",))])
def test_one_failed_or_warning_run_pauses_once_after_the_last(mocker, tmp_path, failing, warning):
    child = _children(mocker, failing=failing, warning=warning)
    pause = patch_pause(mocker)
    session = _two_repos(mocker, tmp_path)

    session.begin_bulk("git pull", confirmed=True)

    assert child.call_count == 2
    pause.assert_called_once()
    envs = {c.kwargs["env"][DIAGNOSTICS_ENV] for c in child.call_args_list}
    assert len(envs) == 1  # one marker shared by every run


def test_a_clean_single_pull_says_it_is_done(mocker, tmp_path):
    session, _, _ = _session(mocker, tmp_path, "Running")
    mocker.patch.object(tsession, "_dispatch_action", return_value=0)

    session.dispatch("alpha-a", "git pull")

    assert session.notice == "'jailbee git pull alpha-a' done"


def test_a_successful_run_unmarks_its_names_and_a_failed_one_keeps_them(mocker, tmp_path):
    _children(mocker, failing=("beta-a",))
    patch_pause(mocker)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    alpha = dmodel.RepoGroup("alpha", str(tmp_path / "a"), None, [ci("alpha-a", "alpha")])
    beta = dmodel.RepoGroup("beta", str(tmp_path / "b"), None, [ci("beta-a", "beta")])
    session, _ = bare_session(mocker, [alpha, beta])
    session.marked = frozenset({"alpha-a", "beta-a"})

    session.begin_bulk("git push", confirmed=True)

    assert session.marked == frozenset({"beta-a"})
    assert session.notice == "git push: 1 ok, 1 failed (beta: exited 1)"


def _two_repos(mocker, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    alpha = dmodel.RepoGroup("alpha", str(tmp_path / "a"), None, [ci("alpha-a", "alpha")])
    beta = dmodel.RepoGroup("beta", str(tmp_path / "b"), None, [ci("beta-a", "beta")])
    session, _ = bare_session(mocker, [alpha, beta])
    session.marked = frozenset({"alpha-a", "beta-a"})
    return session


def test_a_refused_argv_runs_nothing_for_any_repo(mocker, tmp_path):
    child = _children(mocker)
    pause = patch_pause(mocker)
    session = _two_repos(mocker, tmp_path)
    mocker.patch.object(
        ddispatch, "check_dashboard_command", side_effect=[None, RouteError("not allowed here")]
    )

    session.begin_bulk("git push", confirmed=True)

    child.assert_not_called()
    pause.assert_not_called()
    assert "not allowed here" in session.notice
    assert session.marked == frozenset({"alpha-a", "beta-a"})


def test_a_run_that_cannot_start_still_pauses_once_and_keeps_earlier_results(mocker, tmp_path):
    pause = patch_pause(mocker)
    session = _two_repos(mocker, tmp_path)

    def run(argv, **_kw):
        if "beta-a" in argv:
            raise FileNotFoundError(2, "No such file or directory", "jailbee")
        return mocker.Mock(returncode=0)

    mocker.patch.object(tsession.subprocess, "run", side_effect=run)

    session.begin_bulk("git push", confirmed=True)

    pause.assert_called_once()
    assert session.marked == frozenset({"beta-a"})
    assert "No such file or directory: jailbee" in session.notice
    assert "directory no longer exists" not in session.notice
    assert "1 ok" in session.notice


@pytest.mark.parametrize("size", [(120, 40), (60, 40), (40, 40), (60, 10)])
def test_the_destroy_confirm_keeps_both_entries_whatever_the_size(mocker, tmp_path, size):
    group = dmodel.RepoGroup(
        "alpha",
        str(tmp_path),
        None,
        [ci("bulk-b", "alpha", "Running"), ci("bulk-c", "alpha", "Running")],
    )

    run = drive(mocker, ["down", "space", "down", "space", "D"], [group], size=size, screens=True)

    screen = run.screens[5]  # after the five steps, before the padding Ctrl-C
    assert any(line.strip("│ ").strip() == "[n] No" for line in screen.splitlines())
    assert "Yes, destroy 2" in screen


@pytest.mark.parametrize("height", [12, 10, 8, 6])
def test_a_destroy_entry_is_never_selectable_while_hidden(mocker, tmp_path, height):
    group = dmodel.RepoGroup(
        "alpha",
        str(tmp_path),
        None,
        [ci("bulk-b", "alpha", "Running"), ci("bulk-c", "alpha", "Running")],
    )
    mocker.patch.object(
        tsession, "destroy_risk_lines", return_value=tuple(f"⚠ c{i}: dirty" for i in range(6))
    )
    child = mocker.patch.object(tsession.subprocess, "run")

    run = drive(
        mocker,
        ["down", "space", "down", "space", "D", "down", "enter"],
        [group],
        size=(80, height),
        screens=True,
    )

    shown = run.screens[5]  # the confirm as drawn, before any key reached it
    after_down = run.screens[6]  # the cursor has moved: that entry must now be on screen
    destroyed = any("destroy" in c.args[0] for c in child.call_args_list)
    if destroyed:
        assert "Yes, destroy 2" in after_down  # never chosen while hidden
    if height >= 8:
        assert "Yes, destroy 2" in shown
        assert " No " in shown


def test_a_container_with_a_bulk_child_running_is_not_offered_another_verb(mocker, tmp_path):
    session, _, _ = _session(mocker, tmp_path, "Running", "Running")
    session.marked = frozenset({"alpha-a", "alpha-b"})
    session.bulk_batches.append(tsession.BulkBatch("stop", pending={"alpha-a"}))

    action = session._plan("destroy")

    assert action.eligible == ("alpha-b",)
    assert action.skipped == (("alpha-a", "busy"),)


def test_marks_are_planned_in_listing_order_not_alphabetical(mocker, tmp_path):
    group = dmodel.RepoGroup(
        "alpha",
        str(tmp_path),
        None,
        [ci("alpha-c", "alpha"), ci("alpha-a", "alpha"), ci("alpha-b", "alpha")],
    )
    session, _ = bare_session(mocker, [group])
    session.marked = frozenset({"alpha-a", "alpha-b", "alpha-c"})

    assert session._plan("destroy").eligible == ("alpha-c", "alpha-a", "alpha-b")
    picker = session.begin_bulk("destroy")
    assert isinstance(picker, Picker)
    assert "alpha-c, alpha-a, alpha-b" in picker.title


# -- the several-containers question ------------------------------------------


@pytest.mark.parametrize(
    ("verb", "state"),
    [
        ("start", "Stopped"),
        ("stop", "Running"),
        ("restart", "Running"),
        ("net loose", "Running"),
        ("git push", "Running"),
    ],
)
def test_a_bulk_verb_over_several_containers_asks_first_and_runs_nothing(
    mocker, tmp_path, verb, state
):
    child = _children(mocker)
    session, _, _ = _session(mocker, tmp_path, state, state)
    session.marked = frozenset({"alpha-a", "alpha-b"})
    run = mocker.patch.object(session, "run_bulk")

    overlay = session.begin_bulk(verb)

    assert isinstance(overlay, Picker) and overlay.purpose == "bulk-confirm"
    assert overlay.carry == (verb,)
    assert overlay.entries[0].value == "no"  # No is the first, default entry
    assert "alpha-a, alpha-b" in overlay.title and "2 containers" in overlay.title
    run.assert_not_called()
    child.assert_not_called()


def test_answering_yes_runs_the_verb_over_the_marks(mocker, tmp_path):
    child = _children(mocker)
    session, _, _ = _session(mocker, tmp_path, "Running", "Running")
    session.marked = frozenset({"alpha-a", "alpha-b"})
    session.overlay = session.begin_bulk("stop")

    session.picker_chosen(PickerEntry("Yes, 2", "yes"))

    assert [c.args[0] for c in child.call_args_list] == [
        ["jailbee", "stop", "alpha-a"],
        ["jailbee", "stop", "alpha-b"],
    ]


def test_answering_no_runs_nothing(mocker, tmp_path):
    child = _children(mocker)
    session, _, _ = _session(mocker, tmp_path, "Running", "Running")
    session.marked = frozenset({"alpha-a", "alpha-b"})
    session.overlay = session.begin_bulk("stop")

    session.picker_chosen(PickerEntry("No", "no"))

    child.assert_not_called()
    assert session.notice == "Cancelled"
    assert session.overlay is None


def test_one_marked_container_is_not_asked(mocker, tmp_path):
    child = _children(mocker)
    session, _, _ = _session(mocker, tmp_path, "Running", "Running")
    session.marked = frozenset({"alpha-a"})

    assert session.begin_bulk("stop") is None

    assert child.call_args.args[0] == ["jailbee", "stop", "alpha-a"]


def test_the_question_names_who_is_passed_over(mocker, tmp_path):
    session, _, _ = _session(mocker, tmp_path, "Running", "Running", "Stopped")
    session.marked = frozenset({"alpha-a", "alpha-b", "alpha-c"})

    overlay = session.begin_bulk("stop")

    assert isinstance(overlay, Picker)
    assert overlay.detail == ("skipped alpha-c: already stopped",)


def test_loose_asks_to_confirm_before_it_asks_the_ttl(mocker, tmp_path):
    child = _children(mocker)
    session, _, _ = _session(mocker, tmp_path, "Running", "Running", loose_ttl_default="5m")
    session.marked = frozenset({"alpha-a", "alpha-b"})
    session.overlay = session.begin_bulk("net loose")
    assert isinstance(session.overlay, Picker) and session.overlay.purpose == "bulk-confirm"

    session.picker_chosen(PickerEntry("Yes, 2", "yes"))

    assert isinstance(session.overlay, Picker) and session.overlay.purpose == "bulk-loose-ttl"
    child.assert_not_called()


def test_destroy_and_merge_keep_their_own_question(mocker, tmp_path):
    session, _, _ = _session(mocker, tmp_path, "Running", "Running", "Running")
    session.marked = frozenset({"alpha-a", "alpha-b"})

    destroy = session.begin_bulk("destroy")

    assert isinstance(destroy, Picker) and destroy.purpose == "bulk-destroy-confirm"
    assert session.begin_bulk("merge") is None  # straight to picking the targets
    assert session.merge_pick is not None


def test_the_bulk_picker_shows_each_entrys_letter(tmp_path):
    picker = Picker(
        "bulk-action",
        "2 selected",
        (PickerEntry("Stop (2)", "stop", "t"), PickerEntry("Destroy… (2)", "destroy", "D")),
    )

    text = "\n".join(box_text(picker))

    assert "[t] Stop (2)" in text and "[D] Destroy… (2)" in text


def test_a_letter_chooses_the_bulk_picker_entry_that_owns_it(mocker, tmp_path):
    group = dmodel.RepoGroup(
        "alpha",
        str(tmp_path),
        None,
        [ci("bulk-b", "alpha", "Running"), ci("bulk-c", "alpha", "Running")],
    )

    run = drive(mocker, ["down", "space", "down", "space", "enter", "D"], [group])

    assert run.of_type(Picker)[-1].purpose == "bulk-destroy-confirm"


def test_a_bulk_letter_typed_in_the_table_reaches_the_bulk_picker(mocker, tmp_path):
    group = dmodel.RepoGroup(
        "alpha",
        str(tmp_path),
        None,
        [ci("bulk-b", "alpha", "Running"), ci("bulk-c", "alpha", "Running")],
    )

    run = drive(mocker, ["down", "space", "down", "space", "w"], [group])

    assert run.of_type(Picker)[-1].purpose == "bulk-confirm"


def _mount_session(mocker, tmp_path, *attached, ttl=None):
    """Running containers alpha-a… with ``attached[i]`` the kinds mounted on each."""
    from dataclasses import replace

    names = [f"alpha-{c}" for c in "abc"[: len(attached)]]
    group = dmodel.RepoGroup(
        "alpha",
        str(tmp_path),
        None,
        [replace(ci(n, "alpha"), optional_mounts=k) for n, k in zip(names, attached, strict=True)],
        optional_mounts=("aws", "gcp"),
        mount_ttl_defaults=ttl or {},
    )
    session, _ = bare_session(mocker, [group])
    session.jobs = SyncJobs()
    session.marked = frozenset(names)
    return session


def _choose(session, value):
    picker = session.overlay
    assert isinstance(picker, Picker)
    session.picker_chosen(next(e for e in picker.entries if e.value == value))


def test_the_bulk_picker_offers_mounts_with_its_own_letter(mocker, tmp_path):
    session = _mount_session(mocker, tmp_path, (), ())

    picker = session.bulk_menu()

    assert isinstance(picker, Picker)
    entry = next(e for e in picker.entries if e.value == "mounts")
    assert (entry.label, entry.key) == ("Mounts…", "o")
    assert [e.value for e in picker.entries][-2:] == ["mounts", "destroy"]


def test_a_repo_without_optional_mounts_offers_no_mounts_entry(mocker, tmp_path):
    session, _, _ = _session(mocker, tmp_path, "Running", "Running")
    session.marked = frozenset({"alpha-a", "alpha-b"})

    picker = session.bulk_menu()

    assert isinstance(picker, Picker)
    assert "mounts" not in [e.value for e in picker.entries]


def test_a_stopped_container_is_offered_mounts_before_destroy(mocker, tmp_path):
    from dataclasses import replace

    group = dmodel.RepoGroup(
        "alpha",
        str(tmp_path),
        None,
        [replace(ci("alpha-a", "alpha", "Stopped"), optional_mounts=())],
        optional_mounts=("aws",),
    )
    session, _ = bare_session(mocker, [group])
    session.marked = frozenset({"alpha-a"})

    picker = session.bulk_menu()

    assert isinstance(picker, Picker)
    assert [e.value for e in picker.entries] == ["start", "mounts", "destroy"]


def test_the_o_letter_in_the_table_opens_the_bulk_picker(mocker, tmp_path):
    session = _mount_session(mocker, tmp_path, (), ())
    session.handle_key("down")

    assert session.open_menu_for_letter("o")
    assert isinstance(session.overlay, Picker) and session.overlay.purpose == "bulk-action"


def test_mounts_lists_each_kind_and_direction(mocker, tmp_path):
    session = _mount_session(mocker, tmp_path, ("aws",), ())
    session.overlay = session.bulk_menu()

    _choose(session, "mounts")

    assert isinstance(session.overlay, Picker) and session.overlay.purpose == "bulk-mounts"
    assert [(e.label, e.value) for e in session.overlay.entries] == [
        ("○ aws — mount (1), renew TTL (1)", "mount:aws"),
        ("● aws — unmount (1)", "unmount:aws"),
        ("○ gcp — mount (2)", "mount:gcp"),
    ]


def test_bulk_mount_confirms_then_asks_the_ttl_once_and_passes_it(mocker, tmp_path):
    child = _children(mocker)
    session = _mount_session(mocker, tmp_path, (), (), ttl={"aws": "4h"})
    session.overlay = session.bulk_menu()
    _choose(session, "mounts")

    _choose(session, "mount:aws")
    assert isinstance(session.overlay, Picker) and session.overlay.purpose == "bulk-mount-confirm"
    assert session.overlay.title == "Mount 'aws': 2 containers — alpha-a, alpha-b?"
    assert session.overlay.entries[0].value == "no"
    _choose(session, "yes")
    assert isinstance(session.overlay, Picker) and session.overlay.purpose == "bulk-mount-ttl"
    assert session.overlay.entries[0].value == "4h"
    child.assert_not_called()
    _choose(session, "2h")

    assert [c.args[0] for c in child.call_args_list] == [
        ["jailbee", "mount", "--for", "2h", "--", "aws", "alpha-a"],
        ["jailbee", "mount", "--for", "2h", "--", "aws", "alpha-b"],
    ]
    session.tick()
    assert session.notice == "mount aws: 2 ok"
    assert session.marked == frozenset()


def test_bulk_unmount_on_one_container_runs_without_questions(mocker, tmp_path):
    child = _children(mocker)
    session = _mount_session(mocker, tmp_path, ("aws",), ())
    session.overlay = session.bulk_menu()
    _choose(session, "mounts")

    _choose(session, "unmount:aws")

    assert session.overlay is None
    assert [c.args[0] for c in child.call_args_list] == [
        ["jailbee", "unmount", "--", "aws", "alpha-a"]
    ]
    session.tick()
    assert session.notice == "unmount aws: 1 ok, 1 skipped (alpha-b: not mounted)"


def test_bulk_mount_without_a_ttl_default_passes_no_for(mocker, tmp_path):
    child = _children(mocker)
    session = _mount_session(mocker, tmp_path, ())
    session.overlay = session.bulk_menu()
    _choose(session, "mounts")

    _choose(session, "mount:gcp")

    assert child.call_args.args[0] == ["jailbee", "mount", "--", "gcp", "alpha-a"]


def test_declining_the_bulk_mount_runs_nothing(mocker, tmp_path):
    child = _children(mocker)
    session = _mount_session(mocker, tmp_path, (), ())
    session.overlay = session.bulk_menu()
    _choose(session, "mounts")
    _choose(session, "mount:aws")

    _choose(session, "no")

    child.assert_not_called()
    assert session.notice == "Cancelled"


def test_a_bulk_mount_with_nothing_left_says_so(mocker, tmp_path):
    child = _children(mocker)
    session = _mount_session(mocker, tmp_path, ("aws",))
    session.overlay = session.bulk_menu()
    _choose(session, "mounts")
    session.groups[0].containers[0].optional_mounts = ()  # detached meanwhile

    _choose(session, "unmount:aws")

    child.assert_not_called()
    assert session.notice == "Unmount 'aws': nothing to do (alpha-a: not mounted)"


def _orphan_session(mocker, *states):
    group = dmodel.RepoGroup(
        "ghost",
        None,
        None,
        [ci(f"ghost-{c}", "ghost", s) for c, s in zip("abc", states, strict=False)],
    )
    session, _ = bare_session(mocker, [group])
    session.jobs = SyncJobs()
    return session


def test_marked_orphans_offer_a_bulk_destroy_run_from_home(mocker):
    child = _children(mocker)
    session = _orphan_session(mocker, "Running", "Stopped")
    session.marked = frozenset({"ghost-a", "ghost-b"})

    picker = session.bulk_menu()

    assert picker is not None
    assert [e.value for e in picker.entries] == ["orphan destroy"]
    session.overlay = session.begin_bulk("orphan destroy")
    assert isinstance(session.overlay, Picker)
    assert session.overlay.purpose == "bulk-destroy-confirm"
    assert "orphan containers: ghost-a, ghost-b" in session.overlay.title

    session.picker_chosen(PickerEntry("Yes, destroy 2", "yes"))

    assert [c.args[0] for c in child.call_args_list] == [
        ["jailbee", "orphan", "destroy", "ghost-a", "--force"],
        ["jailbee", "orphan", "destroy", "ghost-b", "--force"],
    ]
    assert all(c.kwargs["cwd"] == dmodel.RepoTarget.orphan().cwd() for c in child.call_args_list)
