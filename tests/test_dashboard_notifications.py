"""The notification queue, seen through the dashboard session (fake terminal and runner)."""

from __future__ import annotations

import pytest

from jailbee.dashboard import model as dmodel
from jailbee.dashboard import runner as drunner
from jailbee.dashboard.notifications import QueueCounts
from jailbee.dashboard.overlays import Picker
from jailbee.dashboard.tui import session as tsession
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import bare_session


def _children(mocker, *, failing=()):  # type: ignore[no-untyped-def]
    def run(argv, **_kw):  # type: ignore[no-untyped-def]
        proc = mocker.Mock()
        proc.returncode = 3 if any(name in argv for name in failing) else 0
        return proc

    return mocker.patch.object(tsession.subprocess, "run", side_effect=run)


def _one_repo(mocker, tmp_path, *, defer=False):  # type: ignore[no-untyped-def]
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])
    session, terminal = bare_session(mocker, [group])
    terminal.defer = defer
    return session, terminal


def _start(session):  # type: ignore[no-untyped-def]
    session.run_dashboard_command("alpha-a", "container", ["start", "alpha-a"])


def _entries(session):  # type: ignore[no-untyped-def]
    return list(session.queue)


def _open_queue(session) -> Picker:  # type: ignore[no-untyped-def]
    session.handle_key("queue")
    picker = session.overlay
    assert isinstance(picker, Picker) and picker.purpose == "queue"
    return picker


def _choose(session, label_part: str):  # type: ignore[no-untyped-def]
    picker = session.overlay
    assert isinstance(picker, Picker)
    session.picker_chosen(next(e for e in picker.entries if label_part in e.label))


def test_a_started_action_is_a_running_entry_and_counts_on_the_badge(mocker, tmp_path):
    _children(mocker)
    session, _ = _one_repo(mocker, tmp_path, defer=True)

    _start(session)

    (entry,) = _entries(session)
    assert (entry.command, entry.target, entry.state) == (
        "jailbee start alpha-a",
        "alpha-a",
        "running",
    )
    assert session.view().queue == QueueCounts(running=1, attention=0)


def test_no_badge_while_nothing_has_run(mocker, tmp_path):
    session, _ = _one_repo(mocker, tmp_path)
    assert session.view().queue is None


def test_a_clean_finish_records_status_and_output_without_an_output_screen(mocker, tmp_path):
    _children(mocker)
    session, terminal = _one_repo(mocker, tmp_path, defer=True)
    _start(session)

    terminal.finish_pending()

    (entry,) = _entries(session)
    assert (entry.state, entry.status) == ("done", 0)
    assert entry.output == "output of jailbee start alpha-a\n"
    assert getattr(terminal, "shown", []) == []
    assert session.notice == "'jailbee start alpha-a' done"
    assert session.view().queue == QueueCounts(running=0, attention=0)


def test_a_failed_finish_needs_attention_without_an_output_screen(mocker, tmp_path):
    _children(mocker, failing=("alpha-a",))
    session, terminal = _one_repo(mocker, tmp_path)

    _start(session)

    (entry,) = _entries(session)
    assert (entry.state, entry.status) == ("failed", 3)
    assert getattr(terminal, "shown", []) == []
    assert session.notice is not None and "exited 3" in session.notice
    assert session.view().queue == QueueCounts(running=0, attention=1)


def test_a_bulk_dispatch_is_one_entry_per_action(mocker, tmp_path):
    _children(mocker, failing=("beta-a",))
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    alpha = dmodel.RepoGroup("alpha", str(tmp_path / "a"), None, [ci("alpha-a", "alpha")])
    beta = dmodel.RepoGroup("beta", str(tmp_path / "b"), None, [ci("beta-a", "beta")])
    session, terminal = bare_session(mocker, [alpha, beta])
    session.marked = frozenset({"alpha-a", "beta-a"})

    session.begin_bulk("git pull", confirmed=True)

    first, second = _entries(session)
    assert (first.target, first.state) == ("alpha", "done")
    assert (second.target, second.state) == ("beta", "failed")
    assert "alpha-a" in first.output and "beta-a" not in first.output
    assert "beta-a" in second.output and "──" not in second.output
    assert getattr(terminal, "shown", []) == []


@pytest.mark.parametrize(
    "result",
    [drunner.RunResult(refused="not allowed here"), drunner.RunResult(error="spawn failed")],
)
def test_a_refused_or_unstarted_action_is_a_notice_only(mocker, tmp_path, result):
    session, terminal = _one_repo(mocker, tmp_path)
    terminal.run_actions = lambda runs, done: done(drunner.ActionOutcome([result], "", [""]))

    _start(session)

    assert _entries(session) == []
    assert session.notice is not None
    assert ("not allowed here" in session.notice) or ("spawn failed" in session.notice)


def test_an_action_refused_before_it_starts_is_a_notice_only(mocker, tmp_path):
    session, _ = _one_repo(mocker, tmp_path)
    mocker.patch.object(
        tsession, "check_dashboard_command", side_effect=tsession.RouteError("not allowed here")
    )

    _start(session)

    assert _entries(session) == []
    assert session.notice == "not allowed here"


def test_the_queue_key_opens_a_list_of_entries(mocker, tmp_path):
    _children(mocker, failing=("alpha-a",))
    session, _ = _one_repo(mocker, tmp_path)
    _start(session)

    picker = _open_queue(session)

    assert any("jailbee start alpha-a" in e.label and "failed" in e.label for e in picker.entries)


def test_an_empty_queue_still_opens_and_says_so(mocker, tmp_path):
    session, _ = _one_repo(mocker, tmp_path)
    picker = _open_queue(session)
    assert picker.detail and "No" in picker.detail[0]


def test_an_entry_expands_to_its_full_output(mocker, tmp_path):
    _children(mocker)
    session, terminal = _one_repo(mocker, tmp_path)
    _start(session)
    _open_queue(session)

    _choose(session, "jailbee start alpha-a")
    _choose(session, "Show output")

    assert terminal.shown == [("jailbee start alpha-a", "output of jailbee start alpha-a\n", False)]


def test_dismiss_one_finished_entry(mocker, tmp_path):
    _children(mocker)
    session, _ = _one_repo(mocker, tmp_path)
    _start(session)
    session.run_dashboard_command("alpha-a", "container", ["stop", "alpha-a"])
    _open_queue(session)

    _choose(session, "jailbee stop alpha-a")
    _choose(session, "Dismiss")

    assert [e.command for e in _entries(session)] == ["jailbee start alpha-a"]
    picker = session.overlay
    assert isinstance(picker, Picker) and picker.purpose == "queue"  # back on the list


def test_dismiss_finished_keeps_the_running_entries(mocker, tmp_path):
    _children(mocker)
    session, terminal = _one_repo(mocker, tmp_path, defer=True)
    _start(session)
    terminal.finish_pending()
    _start(session)  # still running
    _open_queue(session)

    _choose(session, "Dismiss finished")

    (entry,) = _entries(session)
    assert entry.state == "running"


def test_a_running_entry_cannot_be_dismissed(mocker, tmp_path):
    _children(mocker)
    session, _ = _one_repo(mocker, tmp_path, defer=True)
    _start(session)
    picker = _open_queue(session)
    assert not any("Dismiss finished" in e.label for e in picker.entries)

    _choose(session, "jailbee start alpha-a")

    entry_picker = session.overlay
    assert isinstance(entry_picker, Picker)
    assert not any(e.label.startswith("Dismiss") for e in entry_picker.entries)
    (entry,) = _entries(session)
    assert session.queue.dismiss(entry.id) is False
    assert len(_entries(session)) == 1


def test_an_open_queue_list_follows_an_entry_finishing(mocker, tmp_path):
    _children(mocker)
    session, terminal = _one_repo(mocker, tmp_path, defer=True)
    _start(session)
    _open_queue(session)

    terminal.finish_pending()

    picker = session.overlay
    assert isinstance(picker, Picker) and picker.purpose == "queue"
    assert any("done" in e.label for e in picker.entries)


# --- the terminal dashboard (Pilot) -------------------------------------------------


def _pilot_start(app):  # type: ignore[no-untyped-def]
    app.session.run_dashboard_command("alpha-a", "container", ["start", "alpha-a"])


def _pilot_group(tmp_path):  # type: ignore[no-untyped-def]
    return dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])


def _is_queue(overlay) -> bool:  # type: ignore[no-untyped-def]
    return isinstance(overlay, Picker) and overlay.purpose == "queue"


def test_pilot_the_badge_shows_and_a_click_on_it_opens_the_queue(mocker, tmp_path):
    from jailbee.dashboard.hit import Hit
    from tests.dashboard_pilot import Click, drive

    _children(mocker, failing=("alpha-a",))
    run = drive(mocker, [_pilot_start, Click(Hit("queue"))], [_pilot_group(tmp_path)], screens=True)

    assert "⟳ 0 · ⚠ 1" in run.screens[1]
    assert _is_queue(run.trace[2].overlay)
    assert run.app.outputs == []  # nothing popped up on its own


def test_pilot_the_key_opens_the_queue_and_an_outside_click_closes_it(mocker, tmp_path):
    from jailbee.dashboard.hit import Hit
    from tests.dashboard_pilot import Click, drive

    _children(mocker)
    run = drive(
        mocker,
        [_pilot_start, "Q", Click(Hit("row", ("alpha-a",)))],
        [_pilot_group(tmp_path)],
    )

    assert _is_queue(run.trace[2].overlay)
    assert run.trace[3].overlay is None
    assert run.trace[3].selected == dmodel.Row("container", "alpha-a")


def test_pilot_an_entry_expands_to_its_full_output(mocker, tmp_path):
    from jailbee.dashboard.tui import prompt_modals as pm
    from tests.dashboard_pilot import drive

    _children(mocker)
    seen: list[object] = []
    run = drive(
        mocker,
        [_pilot_start, "Q", "enter", "o", lambda app: seen.append(app.screen)],
        [_pilot_group(tmp_path)],
    )

    assert run.app.outputs == [
        ("jailbee start alpha-a", "output of jailbee start alpha-a\n", False)
    ]
    assert isinstance(seen[0], pm.OutputScreen)


def test_an_entry_that_finishes_while_its_view_is_open_offers_dismiss(mocker, tmp_path):
    _children(mocker)
    session, terminal = _one_repo(mocker, tmp_path, defer=True)
    _start(session)
    _open_queue(session)
    _choose(session, "jailbee start alpha-a")

    terminal.finish_pending()

    entry_picker = session.overlay
    assert isinstance(entry_picker, Picker) and entry_picker.purpose == "queue-entry"
    assert any(e.label == "Dismiss" for e in entry_picker.entries)
    assert "output of jailbee start alpha-a" in entry_picker.detail  # the output's tail


def test_a_running_entry_says_its_output_comes_when_it_ends(mocker, tmp_path):
    _children(mocker)
    session, terminal = _one_repo(mocker, tmp_path, defer=True)
    _start(session)
    _open_queue(session)

    _choose(session, "jailbee start alpha-a")
    _choose(session, "Show output")

    ((_title, text, failed),) = terminal.shown
    assert "still running" in text and failed is False


def test_a_dispatched_action_names_its_container_as_the_target(mocker, tmp_path):
    _children(mocker)
    session, _ = _one_repo(mocker, tmp_path)

    session.run_dashboard_command("alpha-a", "container", ["service", "ls", "alpha-a"])

    (entry,) = _entries(session)
    assert entry.target == "alpha-a"
