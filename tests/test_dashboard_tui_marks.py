"""Marking containers in the terminal dashboard: keys, pruning, persistence."""

from __future__ import annotations

from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import keys as tkeys
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import bare_session, drive


def _group(tmp_path, *names, state="Running"):
    return dmodel.RepoGroup("alpha", str(tmp_path), None, [ci(n, "alpha", state) for n in names])


def test_space_on_a_container_marks_it_and_moves_down(mocker, tmp_path):
    session, _ = bare_session(mocker, [_group(tmp_path, "alpha-a", "alpha-b")])
    session.handle_key("down")

    session.handle_key("space")

    assert session.marked == frozenset({"alpha-a"})
    assert session.selected == dmodel.Row("container", "alpha-b")


def test_space_on_a_marked_container_unmarks_it(mocker, tmp_path):
    session, _ = bare_session(mocker, [_group(tmp_path, "alpha-a", "alpha-b")])
    for key in ("down", "space", "up", "space"):
        session.handle_key(key)

    assert session.marked == frozenset()


def test_space_on_a_repo_header_still_folds(mocker, tmp_path):
    session, _ = bare_session(mocker, [_group(tmp_path, "alpha-a")])

    session.handle_key("space")

    assert session.folded == frozenset({"alpha"})
    assert session.marked == frozenset()


def test_shift_down_marks_every_row_it_passes(mocker, tmp_path):
    session, _ = bare_session(mocker, [_group(tmp_path, "alpha-a", "alpha-b", "alpha-c")])
    session.handle_key("down")

    session.handle_key("extend-down")
    session.handle_key("extend-down")

    assert session.marked == frozenset({"alpha-a", "alpha-b", "alpha-c"})
    assert session.selected == dmodel.Row("container", "alpha-c")


def test_shift_up_onto_a_header_marks_only_containers(mocker, tmp_path):
    session, _ = bare_session(mocker, [_group(tmp_path, "alpha-a")])
    session.handle_key("down")

    session.handle_key("extend-up")

    assert session.marked == frozenset({"alpha-a"})
    assert session.selected == dmodel.Row("repo", "alpha")


def test_escape_clears_the_marks(mocker, tmp_path):
    session, _ = bare_session(mocker, [_group(tmp_path, "alpha-a")])
    session.handle_key("down")
    session.handle_key("space")

    session.handle_key("cancel")

    assert session.marked == frozenset()
    assert session.notice == "Marks cleared"


def test_escape_without_marks_says_nothing(mocker, tmp_path):
    session, _ = bare_session(mocker, [_group(tmp_path, "alpha-a")])

    session.handle_key("cancel")

    assert session.notice is None


def test_marks_of_a_vanished_container_are_dropped_on_tick(mocker, tmp_path):
    group = _group(tmp_path, "alpha-a", "alpha-b")
    session, _ = bare_session(mocker, [group])
    session.marked = frozenset({"alpha-a", "alpha-b"})

    group.containers.pop(0)
    session.tick()

    assert session.marked == frozenset({"alpha-b"})


def test_marks_survive_a_reorder(mocker, tmp_path):
    group = _group(tmp_path, "alpha-a", "alpha-b", "alpha-c")
    session, _ = bare_session(mocker, [group])
    session.marked = frozenset({"alpha-a", "alpha-c"})

    group.containers.reverse()
    session.tick()

    assert session.marked == frozenset({"alpha-a", "alpha-c"})


def test_marks_in_a_folded_repo_are_kept(mocker, tmp_path):
    session, _ = bare_session(mocker, [_group(tmp_path, "alpha-a")])
    session.marked = frozenset({"alpha-a"})

    session.toggle_fold("alpha")
    session.tick()

    assert session.marked == frozenset({"alpha-a"})


def test_shift_arrows_are_bound():
    assert tkeys.parse_key("shift+up") == "extend-up"
    assert tkeys.parse_key("shift+down") == "extend-down"


def test_the_view_carries_the_marks(mocker, tmp_path):
    run = drive(mocker, ["j", "space"], [_group(tmp_path, "alpha-a", "alpha-b")])

    assert run.last.marked == frozenset({"alpha-a"})
