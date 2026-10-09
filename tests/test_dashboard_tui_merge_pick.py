"""Merge-target mode in the terminal dashboard: entry, keys, the run, and refreshes."""

from __future__ import annotations

from dataclasses import replace

from jailbee.dashboard import model as dmodel
from jailbee.dashboard.hit import Hit
from jailbee.dashboard.model import container_of
from jailbee.dashboard.tui.menu_state import MenuState
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import bare_session, drive, patch_pause

Row = dmodel.Row


def _group(tmp_path, *containers, prefix="r"):  # type: ignore[no-untyped-def]
    return dmodel.RepoGroup(prefix, str(tmp_path), None, list(containers))


def _session(mocker, tmp_path, *containers):  # type: ignore[no-untyped-def]
    group = _group(tmp_path, *containers)
    session, _ = bare_session(mocker, [group])
    return session, group


def _two_repo_session(mocker, tmp_path):  # type: ignore[no-untyped-def]
    (tmp_path / "r").mkdir()
    (tmp_path / "q").mkdir()
    r = dmodel.RepoGroup("r", str(tmp_path / "r"), None, [ci("r-a", "r"), ci("r-c", "r")])
    q = dmodel.RepoGroup("q", str(tmp_path / "q"), None, [ci("q-a", "q")])
    session, _ = bare_session(mocker, [r, q])
    return session


# -- entry -------------------------------------------------------------------


def test_merge_menu_enters_target_mode_with_fork_source_under_cursor(mocker, tmp_path):
    src = ci("r-src", "r")
    fork = replace(ci("r-b", "r"), fork_of="r-src")
    other = ci("r-c", "r")
    session, _ = _session(mocker, tmp_path, other, fork, src)

    session.begin_merge_pick(["r-b"])

    assert session.merge_pick is not None
    assert session.merge_pick.sources == ("r-b",)
    assert session.merge_pick.prefix == "r"
    assert container_of(session.selected) == "r-src"


def test_without_a_common_fork_source_the_cursor_takes_the_first_eligible_row(mocker, tmp_path):
    session, _ = _session(mocker, tmp_path, ci("r-b", "r"), ci("r-a", "r"))

    session.begin_merge_pick(["r-b"])

    assert container_of(session.selected) == "r-a"


def test_a_fork_source_that_is_stopped_is_not_where_the_cursor_lands(mocker, tmp_path):
    src = ci("r-src", "r", "Stopped")
    fork = replace(ci("r-b", "r"), fork_of="r-src")
    session, _ = _session(mocker, tmp_path, src, fork, ci("r-c", "r"))

    session.begin_merge_pick(["r-b"])

    assert container_of(session.selected) == "r-c"


def test_merge_mode_refuses_sources_from_two_repos(mocker, tmp_path):
    session = _two_repo_session(mocker, tmp_path)
    session.marked = frozenset({"r-a", "q-a"})

    session.begin_merge_pick(["r-a", "q-a"])

    assert session.merge_pick is None
    assert session.notice == "Merge sources must be in one repo"
    assert session.marked == frozenset({"r-a", "q-a"})


def test_no_eligible_target_refuses_the_mode(mocker, tmp_path):
    session, _ = _session(
        mocker,
        tmp_path,
        ci("r-a", "r"),
        ci("r-s", "r", "Stopped"),
        ci("r-m", "r", mode="mount"),
    )
    session.marked = frozenset({"r-a"})

    session.begin_merge_pick(["r-a"])

    assert session.merge_pick is None
    assert session.notice == "No running clone-mode container to merge into"
    assert session.marked == frozenset({"r-a"})


def test_a_container_of_another_repo_is_never_a_target(mocker, tmp_path):
    session = _two_repo_session(mocker, tmp_path)

    session.begin_merge_pick(["q-a"])

    assert session.merge_pick is None
    assert session.notice == "No running clone-mode container to merge into"


def test_eligibility_matches_the_cli_rule(mocker, tmp_path):
    session, _ = _session(
        mocker,
        tmp_path,
        ci("r-a", "r"),
        ci("r-b", "r"),
        ci("r-s", "r", "Stopped"),
        ci("r-m", "r", mode="mount"),
    )
    session.begin_merge_pick(["r-b"])

    eligible = {
        name for name in ("r-a", "r-b", "r-s", "r-m") if session.merge_target_eligible(name)
    }
    assert eligible == {"r-a"}
    assert not session.merge_target_eligible("nope")


def test_menu_entry_routes_into_the_mode(mocker, tmp_path):
    session, _ = _session(mocker, tmp_path, ci("r-a", "r"), ci("r-b", "r"))
    session.select(Row("container", "r-b"))
    session.handle_key("enter")
    assert isinstance(session.overlay, MenuState)

    session.menu_chosen("merge", "Git →", 0)

    assert session.overlay is None
    assert session.merge_pick is not None
    assert session.merge_pick.sources == ("r-b",)
    assert container_of(session.selected) == "r-a"


def test_bulk_merge_routes_into_the_mode_with_the_marks_as_sources(mocker, tmp_path):
    session, _ = _session(mocker, tmp_path, ci("r-a", "r"), ci("r-b", "r"), ci("r-c", "r"))
    session.marked = frozenset({"r-c", "r-b"})

    assert session.begin_bulk("merge") is None

    assert session.merge_pick is not None
    assert session.merge_pick.sources == ("r-b", "r-c")
    assert session.merge_pick.saved_marks == frozenset({"r-b", "r-c"})
    assert session.marked == frozenset()


def test_the_menu_keys_g_m_reach_the_mode_in_the_app(mocker, tmp_path):
    run = drive(
        mocker,
        ["j", "j", "enter", "g", "m"],
        [_group(tmp_path, ci("r-a", "r"), ci("r-b", "r"))],
    )

    view = run.last
    assert view.sources == frozenset({"r-b"})
    assert view.selected == Row("container", "r-a")
    assert view.notice is not None and view.notice.startswith("Merge b into: —")


# -- keys --------------------------------------------------------------------


def test_merge_mode_skips_source_and_stopped_rows(mocker, tmp_path):
    session, _ = _session(
        mocker, tmp_path, ci("r-a", "r"), ci("r-b", "r"), ci("r-s", "r", "Stopped"), ci("r-c", "r")
    )
    session.begin_merge_pick(["r-b"])
    assert container_of(session.selected) == "r-a"

    session.handle_key("down")
    assert container_of(session.selected) == "r-c"  # skipped r-b (source) and r-s (stopped)

    session.handle_key("down")
    assert container_of(session.selected) == "r-c"  # nothing further: stays put

    session.handle_key("up")
    assert container_of(session.selected) == "r-a"

    session.handle_key("up")
    assert container_of(session.selected) == "r-a"  # the repo header is not a target


def test_space_marks_a_target_and_moves_to_the_next_eligible_row(mocker, tmp_path):
    session, _ = _session(mocker, tmp_path, ci("r-a", "r"), ci("r-b", "r"), ci("r-c", "r"))
    session.begin_merge_pick(["r-b"])

    session.handle_key("space")

    assert session.merge_pick is not None
    assert session.merge_pick.targets == frozenset({"r-a"})
    assert container_of(session.selected) == "r-c"
    assert session.marked == frozenset()  # target marks are not bulk marks
    assert session.view().marked == frozenset({"r-a"})


def test_space_twice_on_one_row_unmarks_it(mocker, tmp_path):
    session, _ = _session(mocker, tmp_path, ci("r-a", "r"), ci("r-b", "r"))
    session.begin_merge_pick(["r-b"])

    session.handle_key("space")  # marks r-a; no eligible row below, cursor stays
    session.handle_key("space")

    assert session.merge_pick is not None
    assert session.merge_pick.targets == frozenset()


def test_merge_mode_space_marks_and_enter_runs_all_targets(mocker, tmp_path):
    session, _ = _session(mocker, tmp_path, ci("r-a", "r"), ci("r-b", "r"), ci("r-c", "r"))
    run = mocker.patch.object(session, "run_dashboard_command", return_value=0)
    session.begin_merge_pick(["r-b"])

    session.handle_key("space")  # r-a, then the cursor moves on to r-c
    session.handle_key("down")  # nothing further: stays on r-c
    session.handle_key("space")  # r-c
    session.handle_key("enter")

    run.assert_called_once_with(
        "r-b", "container", ["merge", "r-b", "--into", "r-a", "--into", "r-c"]
    )
    assert session.merge_pick is None


def test_merge_mode_enter_without_marks_uses_cursor_row(mocker, tmp_path):
    session, _ = _session(mocker, tmp_path, ci("r-a", "r"), ci("r-b", "r"))
    run = mocker.patch.object(session, "run_dashboard_command", return_value=0)
    session.begin_merge_pick(["r-b"])

    session.handle_key("enter")

    run.assert_called_once_with("r-b", "container", ["merge", "r-b", "--into", "r-a"])
    assert session.merge_pick is None


def test_enter_on_an_ineligible_cursor_row_with_no_marks_runs_nothing(mocker, tmp_path):
    group = _group(tmp_path, ci("r-a", "r"), ci("r-b", "r"))
    session, _ = bare_session(mocker, [group])
    run = mocker.patch.object(session, "run_dashboard_command", return_value=0)
    session.begin_merge_pick(["r-b"])
    group.containers[0] = ci("r-a", "r", "Stopped")  # the only target stops
    session.tick()

    session.handle_key("enter")

    run.assert_not_called()
    assert session.merge_pick is not None
    assert session.notice == "No merge target is highlighted"


def test_a_successful_bulk_merge_unmarks_the_sources_only(mocker, tmp_path):
    session, _ = _session(
        mocker, tmp_path, ci("r-a", "r"), ci("r-b", "r"), ci("r-c", "r"), ci("r-d", "r")
    )
    mocker.patch.object(session, "run_dashboard_command", return_value=0)
    session.marked = frozenset({"r-b", "r-c"})
    session.begin_bulk("merge")
    session.handle_key("enter")

    assert session.marked == frozenset()


def test_a_failed_merge_keeps_the_bulk_marks(mocker, tmp_path):
    session, _ = _session(mocker, tmp_path, ci("r-a", "r"), ci("r-b", "r"), ci("r-c", "r"))
    mocker.patch.object(session, "run_dashboard_command", return_value=1)
    session.marked = frozenset({"r-b", "r-c"})
    session.begin_bulk("merge")
    session.handle_key("enter")

    assert session.merge_pick is None
    assert session.marked == frozenset({"r-b", "r-c"})


def test_a_successful_bulk_merge_keeps_a_mark_that_was_not_a_source(mocker, tmp_path):
    # r-s is marked but stopped, so the bulk plan skips it as a source: the
    # merge succeeding says nothing about it, and its mark must survive.
    session, _ = _session(
        mocker, tmp_path, ci("r-a", "r"), ci("r-b", "r"), ci("r-s", "r", "Stopped")
    )
    mocker.patch.object(session, "run_dashboard_command", return_value=0)
    session.marked = frozenset({"r-b", "r-s"})
    session.begin_bulk("merge")
    assert session.merge_pick is not None
    assert session.merge_pick.sources == ("r-b",)

    session.handle_key("enter")

    assert session.marked == frozenset({"r-s"})


def test_merge_targets_marked_out_of_listing_order_run_in_listing_order(mocker, tmp_path):
    session, _ = _session(mocker, tmp_path, ci("r-a", "r"), ci("r-b", "r"), ci("r-c", "r"))
    run = mocker.patch.object(session, "run_dashboard_command", return_value=0)
    session.begin_merge_pick(["r-b"])

    session.click(Hit("row", ("r-c",)), toggle=True)  # c first ...
    session.click(Hit("row", ("r-a",)), toggle=True)  # ... then a
    session.handle_key("enter")

    run.assert_called_once_with(
        "r-b", "container", ["merge", "r-b", "--into", "r-a", "--into", "r-c"]
    )


def test_a_merge_that_never_ran_restores_the_marks_untouched(mocker, tmp_path):
    # `run_dashboard_command` answers None when it did not run the command
    # (e.g. the remote policy refused it): nothing succeeded, so nothing is
    # unmarked, the sources included.
    session, _ = _session(mocker, tmp_path, ci("r-a", "r"), ci("r-b", "r"), ci("r-c", "r"))
    mocker.patch.object(session, "run_dashboard_command", return_value=None)
    session.marked = frozenset({"r-b", "r-c"})
    session.begin_bulk("merge")

    session.handle_key("enter")

    assert session.merge_pick is None
    assert session.marked == frozenset({"r-b", "r-c"})


def test_the_merge_runs_in_the_terminal_with_every_into(mocker, tmp_path):
    child = mocker.patch("jailbee.dashboard.tui.session.subprocess.run")
    child.return_value.returncode = 0
    patch_pause(mocker)
    session, _ = _session(mocker, tmp_path, ci("r-a", "r"), ci("r-b", "r"))
    session.begin_merge_pick(["r-b"])

    session.handle_key("enter")

    assert child.call_args.args[0] == ["jailbee", "merge", "r-b", "--into", "r-a"]


def test_merge_mode_esc_restores_bulk_marks(mocker, tmp_path):
    session, _ = _session(mocker, tmp_path, ci("r-a", "r"), ci("r-b", "r"), ci("r-c", "r"))
    session.marked = frozenset({"r-b", "r-c"})

    session.begin_merge_pick(["r-b", "r-c"])
    assert session.marked == frozenset()
    session.handle_key("cancel")

    assert session.merge_pick is None
    assert session.marked == frozenset({"r-b", "r-c"})
    assert session.notice == "Merge cancelled"


def test_other_keys_are_ignored_while_picking(mocker, tmp_path):
    session, _ = _session(mocker, tmp_path, ci("r-a", "r"), ci("r-b", "r"))
    session.begin_merge_pick(["r-b"])
    pick = session.merge_pick

    for key in ("help", "command", "settings", "action:destroy", "new", "extend-down", "mouse"):
        assert session.handle_key(key) is None

    assert session.overlay is None
    assert session.merge_pick == pick
    assert session.marked == frozenset()


def test_quit_still_quits_while_picking(mocker, tmp_path):
    session, _ = _session(mocker, tmp_path, ci("r-a", "r"), ci("r-b", "r"))
    session.begin_merge_pick(["r-b"])

    assert session.handle_key("quit") == "quit"


def test_clicks_while_picking_never_open_a_menu_or_touch_bulk_marks(mocker, tmp_path):
    session, _ = _session(mocker, tmp_path, ci("r-a", "r"), ci("r-b", "r"), ci("r-c", "r"))
    session.begin_merge_pick(["r-b"])

    session.click(Hit("row", ("r-c",)), double=True)
    assert session.overlay is None
    assert container_of(session.selected) == "r-c"

    session.click(Hit("row", ("r-a",)), toggle=True)
    assert session.marked == frozenset()
    assert session.merge_pick is not None
    assert session.merge_pick.targets == frozenset({"r-a"})

    session.click(Hit("row", ("r-b",)))  # a source: not a target, cursor stays
    assert container_of(session.selected) == "r-a"


# -- view ------------------------------------------------------------------


def test_the_view_shows_the_mode(mocker, tmp_path):
    session, _ = _session(
        mocker, tmp_path, ci("r-a", "r"), ci("r-b", "r"), ci("r-s", "r", "Stopped")
    )
    session.set_notice("something else")
    session.begin_merge_pick(["r-b"])
    view = session.view()
    assert view.notice == ("Merge b into: —   space mark · enter merge · esc cancel")
    assert view.sources == frozenset({"r-b"})
    assert view.ineligible == frozenset({"r-s"})

    session.handle_key("space")
    view = session.view()
    assert view.notice is not None and view.notice.startswith("Merge b into: a ")
    assert view.marked == frozenset({"r-a"})


def test_the_view_outside_the_mode_has_no_roles(mocker, tmp_path):
    session, _ = _session(mocker, tmp_path, ci("r-a", "r"))
    view = session.view()
    assert view.sources == frozenset()
    assert view.ineligible == frozenset()


# -- refresh -----------------------------------------------------------------


def test_merge_mode_cancels_when_source_vanishes(mocker, tmp_path):
    group = _group(tmp_path, ci("r-a", "r"), ci("r-b", "r"), ci("r-c", "r"))
    session, _ = bare_session(mocker, [group])
    session.marked = frozenset({"r-b", "r-c"})
    session.begin_merge_pick(["r-b"])

    group.containers.pop(1)  # r-b is destroyed
    session.tick()

    assert session.merge_pick is None
    assert session.notice == "Merge cancelled: source is gone"
    assert session.marked == frozenset({"r-c"})


def test_one_of_two_sources_vanishing_keeps_the_mode(mocker, tmp_path):
    group = _group(tmp_path, ci("r-a", "r"), ci("r-b", "r"), ci("r-c", "r"))
    session, _ = bare_session(mocker, [group])
    session.marked = frozenset({"r-b", "r-c"})
    session.begin_merge_pick(["r-b", "r-c"])

    group.containers.pop(2)
    session.tick()

    assert session.merge_pick is not None
    assert session.merge_pick.sources == ("r-b",)
    assert session.merge_pick.saved_marks == frozenset({"r-b"})


def test_a_target_that_vanishes_or_stops_is_dropped(mocker, tmp_path):
    group = _group(tmp_path, ci("r-a", "r"), ci("r-b", "r"), ci("r-c", "r"), ci("r-d", "r"))
    session, _ = bare_session(mocker, [group])
    session.begin_merge_pick(["r-b"])
    for _ in range(3):
        session.handle_key("space")  # r-a, r-c, r-d
    assert session.merge_pick is not None
    assert session.merge_pick.targets == frozenset({"r-a", "r-c", "r-d"})

    group.containers[2] = ci("r-c", "r", "Stopped")
    group.containers.pop(3)
    session.tick()

    assert session.merge_pick is not None
    assert session.merge_pick.targets == frozenset({"r-a"})


def test_a_cursor_left_on_a_stopped_row_moves_to_an_eligible_one(mocker, tmp_path):
    group = _group(tmp_path, ci("r-a", "r"), ci("r-b", "r"), ci("r-c", "r"))
    session, _ = bare_session(mocker, [group])
    session.begin_merge_pick(["r-b"])
    session.handle_key("down")
    assert container_of(session.selected) == "r-c"

    group.containers[2] = ci("r-c", "r", "Stopped")
    session.tick()

    assert container_of(session.selected) == "r-a"


def test_a_bulk_batch_ending_while_picking_unmarks_its_successes_on_restore(mocker, tmp_path):
    from jailbee.dashboard.bulk import BulkBatch

    session, _ = _session(mocker, tmp_path, ci("r-a", "r"), ci("r-b", "r"), ci("r-c", "r"))
    session.marked = frozenset({"r-b", "r-c"})
    batch = BulkBatch("stop", pending={"r-c"})
    session.bulk_batches.append(batch)
    session.begin_merge_pick(["r-b"])

    batch.pending.clear()
    batch.ok.append("r-c")
    session._settle(batch)
    assert session.marked == frozenset()  # still picking: the target marks are separate
    session.handle_key("cancel")

    assert session.marked == frozenset({"r-b"})
