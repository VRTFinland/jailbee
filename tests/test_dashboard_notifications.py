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
    assert session.notice == "'jailbee start alpha-a' done · Q: queue"
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
    # `git pull` is critical: the first run's modal opened; nothing else popped up.
    assert [t for t, _, _ in terminal.shown] == ["jailbee git pull alpha-a"]


@pytest.mark.parametrize(
    "result",
    [drunner.RunResult(refused="not allowed here"), drunner.RunResult(error="spawn failed")],
)
def test_a_refused_or_unstarted_action_is_a_notice_only(mocker, tmp_path, result):
    session, terminal = _one_repo(mocker, tmp_path)
    terminal.run_actions = lambda runs, done, progress=None: done(
        drunner.ActionOutcome([result], "", [""])
    )

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


def test_a_running_entry_with_no_output_yet_says_so(mocker, tmp_path):
    _children(mocker)
    session, terminal = _one_repo(mocker, tmp_path, defer=True)
    _start(session)
    _open_queue(session)

    _choose(session, "jailbee start alpha-a")
    _choose(session, "Show output")

    ((_title, text, failed),) = terminal.shown
    assert text == "(no output yet)" and failed is False


def test_a_dispatched_action_names_its_container_as_the_target(mocker, tmp_path):
    _children(mocker)
    session, _ = _one_repo(mocker, tmp_path)

    session.run_dashboard_command("alpha-a", "container", ["service", "ls", "alpha-a"])

    (entry,) = _entries(session)
    assert entry.target == "alpha-a"


# --- review rework (#146 ticket 18) -------------------------------------------------


def _outcome_terminal(terminal, *, status=0, output="line\n", error=None):  # type: ignore[no-untyped-def]
    result = drunner.RunResult(status=None if error else status, error=error)
    terminal.run_actions = lambda runs, done, progress=None: done(
        drunner.ActionOutcome([result], output, [output])
    )


def test_a_printing_verb_opens_its_output_when_it_finishes(mocker, tmp_path):
    session, terminal = _one_repo(mocker, tmp_path)
    _outcome_terminal(terminal, output="+added\n")

    session.run_dashboard_command("alpha-a", "container", ["net", "egress", "ls"], style="output")

    assert terminal.shown == [("jailbee net egress ls", "+added\n", False)]
    assert session.notice is not None and "Q" in session.notice


def test_a_failed_printing_verb_opens_its_output_as_failed(mocker, tmp_path):
    session, terminal = _one_repo(mocker, tmp_path)
    _outcome_terminal(terminal, status=2, output="boom\n")

    session.run_dashboard_command("alpha-a", "container", ["doctor"], style="paged")

    assert terminal.shown == [("jailbee doctor", "boom\n", True)]


def test_a_plain_action_stays_quiet_but_its_notice_names_the_queue(mocker, tmp_path):
    _children(mocker)
    session, terminal = _one_repo(mocker, tmp_path)

    _start(session)

    assert getattr(terminal, "shown", []) == []
    assert session.notice is not None and session.notice.startswith("'jailbee start alpha-a' done")
    assert "Q" in session.notice


def test_the_command_line_opens_its_output(mocker, tmp_path):
    session, terminal = _one_repo(mocker, tmp_path)
    _outcome_terminal(terminal, output="listing\n")
    repo = session.repo_for("alpha-a", "container")
    assert repo is not None

    session.run_action(repo, ["ls"], style="output", label="ls")

    assert [text for _t, text, _f in terminal.shown] == ["listing\n"]


def test_the_entry_tail_has_no_ansi_escapes(mocker, tmp_path):
    session, terminal = _one_repo(mocker, tmp_path)
    _outcome_terminal(terminal, output="\x1b[32m+added line\x1b[0m\n\x1b[31m-gone\x1b[0m\n")
    _start(session)
    _open_queue(session)

    _choose(session, "jailbee start alpha-a")

    picker = session.overlay
    assert isinstance(picker, Picker)
    assert "+added line" in picker.detail and "-gone" in picker.detail
    assert all("\x1b" not in line for line in picker.detail)


def test_a_failure_notice_carries_the_last_output_line_for_any_style(mocker, tmp_path):
    session, terminal = _one_repo(mocker, tmp_path)
    _outcome_terminal(terminal, status=1, output="merging\nCONFLICT in a.py\n")

    session.run_dashboard_command("alpha-a", "container", ["merge", "x"], style="report")

    assert session.notice == "'jailbee merge x' exited 1: CONFLICT in a.py · Q: queue"


def test_a_serve_error_after_output_finishes_the_entry_as_failed(mocker, tmp_path):
    session, terminal = _one_repo(mocker, tmp_path)
    _outcome_terminal(terminal, output="partial\n", error="prompt channel broke")

    _start(session)

    (entry,) = _entries(session)
    assert entry.state == "failed" and entry.output == "partial\n"


def test_a_start_error_without_output_still_leaves_no_entry(mocker, tmp_path):
    session, terminal = _one_repo(mocker, tmp_path)
    _outcome_terminal(terminal, output="", error="spawn failed")

    _start(session)

    assert _entries(session) == []


# --- live output and questions (#146 ticket 13) -----------------------------


def test_queue_append_accumulates_chunks_in_order_and_finish_keeps_the_output():
    from jailbee.dashboard.notifications import NotificationQueue

    queue = NotificationQueue()
    entry_id = queue.start("jailbee merge a", "a")
    queue.append(entry_id, "one\n")
    queue.append(entry_id, "two\n")

    entry = queue.get(entry_id)
    assert entry is not None and entry.output == "one\ntwo\n" and entry.state == "running"

    queue.finish(entry_id, 0, "one\ntwo\nthree\n")
    final = queue.get(entry_id)
    assert final is not None and final.output == "one\ntwo\nthree\n" and final.state == "done"


def test_queue_append_respects_the_output_cap_and_marks_the_drop():
    from jailbee.dashboard.notifications import NotificationQueue

    queue = NotificationQueue()
    entry_id = queue.start("jailbee merge a", "a")
    for _ in range(5):
        queue.append(entry_id, "x" * drunner.OUTPUT_KEEP_CHARS)
    queue.append(entry_id, "END")

    entry = queue.get(entry_id)
    assert entry is not None
    assert entry.output.startswith(drunner.OUTPUT_DROPPED_MARKER)
    assert entry.output.endswith("xxEND")
    assert len(entry.output) <= drunner.OUTPUT_KEEP_CHARS + len(drunner.OUTPUT_DROPPED_MARKER)
    assert entry.output.count(drunner.OUTPUT_DROPPED_MARKER) == 1


def test_queue_append_to_a_gone_or_finished_entry_is_ignored():
    from jailbee.dashboard.notifications import NotificationQueue

    queue = NotificationQueue()
    entry_id = queue.start("jailbee merge a", "a")
    queue.finish(entry_id, 0, "done\n")
    queue.append(entry_id, "late\n")
    queue.append(99, "nobody\n")

    entry = queue.get(entry_id)
    assert entry is not None and entry.output == "done\n"


def test_queue_question_marks_awaiting_and_answering_returns_to_running():
    from jailbee.dashboard.notifications import NotificationQueue, QueueCounts

    queue = NotificationQueue()
    entry_id = queue.start("jailbee merge a", "a")

    queue.set_awaiting(entry_id, True)
    entry = queue.get(entry_id)
    assert entry is not None and entry.state == "awaiting" and not entry.finished
    assert entry.needs_attention
    assert queue.counts() == QueueCounts(running=0, attention=1)
    assert not queue.dismiss(entry_id)

    queue.set_awaiting(entry_id, False)
    entry = queue.get(entry_id)
    assert entry is not None and entry.state == "running"
    assert queue.counts() == QueueCounts(running=1, attention=0)


def _progress(terminal):  # type: ignore[no-untyped-def]
    return terminal.progress


def test_session_chunks_accumulate_into_the_running_entry_in_order(mocker, tmp_path):
    session, terminal = _one_repo(mocker, tmp_path, defer=True)
    _start(session)

    progress = _progress(terminal)
    progress.output(0, "first\n")
    progress.output(0, "second\n")

    (entry,) = _entries(session)
    assert entry.state == "running" and entry.output == "first\nsecond\n"


def test_session_question_goes_awaiting_answer_then_running_again(mocker, tmp_path):
    session, terminal = _one_repo(mocker, tmp_path, defer=True)
    _start(session)
    progress = _progress(terminal)

    progress.question(0, True)
    (entry,) = _entries(session)
    assert entry.state == "awaiting"
    assert session.view().queue == QueueCounts(running=0, attention=1)

    progress.question(0, False)
    (entry,) = _entries(session)
    assert entry.state == "running"
    assert session.view().queue == QueueCounts(running=1, attention=0)


def test_session_failure_and_pending_question_both_raise_the_attention_count(mocker, tmp_path):
    _children(mocker, failing=("alpha-a",))
    session, terminal = _one_repo(mocker, tmp_path, defer=True)
    _start(session)
    _progress(terminal).question(0, True)
    assert session.view().queue == QueueCounts(running=0, attention=1)

    terminal.finish_pending()  # the child ended (exit 3) with the question still open

    (entry,) = _entries(session)
    assert entry.state == "failed"
    assert session.view().queue == QueueCounts(running=0, attention=1)


def test_the_list_row_of_a_running_entry_shows_its_last_line(mocker, tmp_path):
    session, terminal = _one_repo(mocker, tmp_path, defer=True)
    _start(session)
    _progress(terminal).output(0, "fetching\nmerging 3 commits\n")

    picker = _open_queue(session)

    assert "merging 3 commits" in picker.entries[0].label
    assert "fetching" not in picker.entries[0].label


def test_an_open_list_redraws_as_chunks_arrive(mocker, tmp_path):
    session, terminal = _one_repo(mocker, tmp_path, defer=True)
    _start(session)
    _open_queue(session)

    _progress(terminal).output(0, "step 2\n")

    picker = session.overlay
    assert isinstance(picker, Picker) and "step 2" in picker.entries[0].label


def test_an_expanded_running_entry_follows_its_output_live(mocker, tmp_path):
    session, terminal = _one_repo(mocker, tmp_path, defer=True)
    _start(session)
    _progress(terminal).output(0, "one\n")
    _open_queue(session)
    _choose(session, "alpha-a")
    _choose(session, "Show output")
    assert terminal.shown[-1][1] == "one\n"  # what has arrived so far, not a placeholder

    _progress(terminal).output(0, "two\n")

    assert terminal.updated[-1] == "one\ntwo\n"


def test_returning_from_a_hand_off_shows_the_queue_when_something_needs_attention(mocker, tmp_path):
    session, terminal = _one_repo(mocker, tmp_path, defer=True)
    _start(session)
    _progress(terminal).question(0, True)

    session.after_hand_off()

    picker = session.overlay
    assert isinstance(picker, Picker) and picker.purpose == "queue"


def test_returning_from_a_hand_off_leaves_the_screen_alone_when_nothing_needs_attention(
    mocker, tmp_path
):
    session, _ = _one_repo(mocker, tmp_path, defer=True)
    _start(session)  # running, no question

    session.after_hand_off()

    assert session.overlay is None


class _GatedChild:
    """A child that prints only when the test says so, then exits 0 once released."""

    def __init__(self, sock) -> None:  # type: ignore[no-untyped-def]
        import os
        import threading

        read_fd, self._write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb")
        self._sock = sock
        self._released = threading.Event()
        self._thread = threading.Thread(target=self._hold)
        self._thread.start()

    def _hold(self) -> None:
        import os

        self._released.wait(10)
        os.close(self._write_fd)
        self._sock.close()

    def print(self, text: str) -> None:
        import os

        os.write(self._write_fd, text.encode())

    def release(self) -> None:
        self._released.set()

    def wait(self) -> int:
        self._thread.join(10)
        return 0

    def kill(self) -> None:
        self._released.set()


def test_pilot_an_expanded_running_entry_updates_live(mocker, tmp_path):
    import asyncio
    import time

    from jailbee.dashboard.tui import prompt_modals as pm
    from tests.dashboard_pilot import keys, make_app

    children: list[_GatedChild] = []

    def spawn(argv, *, cwd, env, prompt_sock):  # type: ignore[no-untyped-def]
        child = _GatedChild(prompt_sock)
        children.append(child)
        return child

    app = make_app(mocker, [_pilot_group(tmp_path)])
    app.threaded_actions = True
    app.action_runner = drunner.run_action
    app.action_spawn = spawn

    def shown(app) -> str:  # type: ignore[no-untyped-def]
        screen = app.screen
        assert isinstance(screen, pm.OutputScreen)
        return str(screen.query_one("#captured").render())

    async def until(pilot, condition, what):  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + 5
        while not condition():
            assert time.monotonic() < deadline, what
            await pilot.pause(0.02)

    async def main() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            for key in ["j", "!", *keys("start alpha-a"), "enter"]:
                await pilot.press(key)
            await until(pilot, lambda: bool(children), "the child")
            children[0].print("first line\n")
            await until(pilot, lambda: "first line" in _row(app), "the row's last line")
            await pilot.press("Q", "enter", "enter")  # list → entry → Show output
            await until(pilot, lambda: isinstance(app.screen, pm.OutputScreen), "the screen")
            assert "first line" in shown(app)
            children[0].print("second line\n")
            await until(pilot, lambda: "second line" in shown(app), "the live update")
            assert "first line" in shown(app)
            children[0].release()
            await until(pilot, lambda: all(e.finished for e in app.session.queue), "the finish")

    def _row(app) -> str:  # type: ignore[no-untyped-def]
        return " ".join(e.output for e in app.session.queue)

    asyncio.run(asyncio.wait_for(main(), 20))


def test_pilot_returning_from_a_hand_off_surfaces_attention(mocker, tmp_path):
    from tests.dashboard_pilot import drive

    _children(mocker, failing=("alpha-a",))
    seen: list[object] = []

    def hand_off(app) -> None:  # type: ignore[no-untyped-def]
        def run() -> int:
            # Attention that arrives while the terminal is handed over.
            _pilot_start(app)
            return 0

        app.hand_off(run)
        seen.append(app.session.overlay)

    drive(mocker, [hand_off], [_pilot_group(tmp_path)])

    (overlay,) = seen
    assert _is_queue(overlay)


def test_run_action_returns_the_id_of_its_queue_entry(mocker, tmp_path):
    session, _ = _one_repo(mocker, tmp_path, defer=True)
    repo = session.repo_for("alpha-a", "container")
    assert repo is not None

    entry_id = session.run_action(repo, ["start", "alpha-a"], style="report", label="start alpha-a")

    (entry,) = _entries(session)
    assert entry.id == entry_id


# --- lifecycle jobs from the snapshot (#146 ticket 15) -------------------------------


def _job_container(name, phase, *, kind="create", pid=None):  # type: ignore[no-untyped-def]
    import dataclasses
    import os

    return dataclasses.replace(
        ci(name, "alpha", job_phase=phase, job_pid=pid if pid is not None else os.getpid()),
        job_kind=kind,
    )


def _job_session(mocker, tmp_path, containers):  # type: ignore[no-untyped-def]
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, containers)
    session, _ = bare_session(mocker, [group])
    return session, group


def _retick(session, group, containers):  # type: ignore[no-untyped-def]
    group.containers[:] = containers
    session.tick()


def test_snapshot_jobs_are_read_only_queue_rows_counted_as_running(mocker, tmp_path):
    session, _ = _job_session(
        mocker,
        tmp_path,
        [
            _job_container("alpha-a", "creating"),
            _job_container("alpha-b", "starting", kind="boot"),
            _job_container("alpha-c", "deps", kind="autostart"),
            _job_container("alpha-d", "stopping", kind="stop"),  # not a lifecycle job
            ci("alpha-e", "alpha"),
        ],
    )

    assert [(j.target, j.failed) for j in session.queue.jobs] == [
        ("alpha-a", False),
        ("alpha-b", False),
        ("alpha-c", False),
    ]
    assert len(list(session.queue)) == 0  # not actions of this session
    assert session.view().queue == QueueCounts(running=3, attention=0)
    picker = _open_queue(session)
    assert [e.label.split(":")[0].lstrip("⟳✗ ") for e in picker.entries] == [
        "alpha-a",
        "alpha-b",
        "alpha-c",
    ]
    assert not any("Dismiss" in e.label for e in picker.entries)


def test_a_job_row_goes_when_the_job_does(mocker, tmp_path):
    session, group = _job_session(mocker, tmp_path, [_job_container("alpha-a", "creating")])
    assert session.view().queue == QueueCounts(running=1, attention=0)

    _retick(session, group, [ci("alpha-a", "alpha")])

    assert session.queue.jobs == ()
    assert session.view().queue is None


def test_an_open_queue_follows_the_jobs(mocker, tmp_path):
    session, group = _job_session(mocker, tmp_path, [_job_container("alpha-a", "creating")])
    _open_queue(session)

    _retick(session, group, [ci("alpha-a", "alpha")])

    picker = session.overlay
    assert isinstance(picker, Picker) and picker.purpose == "queue"
    assert [e.value for e in picker.entries] == ["close"]


def test_a_failed_job_needs_attention_while_the_snapshot_shows_it(mocker, tmp_path):
    session, group = _job_session(mocker, tmp_path, [_job_container("alpha-a", "failed")])

    assert [(j.target, j.failed) for j in session.queue.jobs] == [("alpha-a", True)]
    assert session.view().queue == QueueCounts(running=0, attention=1)

    _retick(session, group, [ci("alpha-a", "alpha")])
    assert session.view().queue is None


def test_a_dead_worker_is_a_failed_job(mocker, tmp_path):
    session, _ = _job_session(mocker, tmp_path, [_job_container("alpha-a", "creating", pid=2**22)])
    assert session.view().queue == QueueCounts(running=0, attention=1)


def test_a_job_row_cannot_be_dismissed(mocker, tmp_path):
    _children(mocker)
    session, _ = _job_session(mocker, tmp_path, [_job_container("alpha-a", "failed")])
    _open_queue(session)
    _choose(session, "alpha-a")

    picker = session.overlay
    assert isinstance(picker, Picker) and picker.purpose == "queue-job"
    assert [e.value for e in picker.entries] == ["log"]
    assert session.view().queue == QueueCounts(running=0, attention=1)


def test_a_job_row_opens_the_job_log(mocker, tmp_path):
    run = _children(mocker)
    session, _ = _job_session(mocker, tmp_path, [_job_container("alpha-a", "failed")])
    _open_queue(session)
    _choose(session, "alpha-a")
    _choose(session, "Show job log")

    argvs = [call.args[0] for call in run.call_args_list]
    assert any(a[:3] == ["jailbee", "job", "log"] and "alpha-a" in a for a in argvs)


def test_a_live_job_log_follows_through_the_terminal_hand_off(mocker, tmp_path):
    run = _children(mocker)
    session, _ = _job_session(mocker, tmp_path, [_job_container("alpha-a", "creating")])
    session.terminal.handed.clear()
    _open_queue(session)
    _choose(session, "alpha-a")
    _choose(session, "Show job log")

    assert len(session.terminal.handed) == 1
    argvs = [call.args[0] for call in run.call_args_list]
    assert any(a[:3] == ["jailbee", "job", "log"] and "--follow" in a for a in argvs)


def test_a_job_log_of_a_hidden_repo_is_not_offered_as_a_row(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [_job_container("alpha-a", "failed")])
    session, _ = bare_session(mocker, [group])
    assert [j.target for j in session.queue.jobs] == ["alpha-a"]

    session.hidden_repos = frozenset({"alpha"})
    session.tick()

    assert session.queue.jobs == ()
    assert session.view().queue is None


def test_returning_from_a_hand_off_ignores_attention_that_was_already_there(mocker, tmp_path):
    session, _ = _job_session(mocker, tmp_path, [_job_container("alpha-a", "failed")])
    before = session.queue.counts().attention
    assert before == 1

    session.after_hand_off(before)

    assert session.overlay is None


def test_returning_from_a_hand_off_opens_for_attention_that_is_new(mocker, tmp_path):
    session, _ = _job_session(mocker, tmp_path, [_job_container("alpha-a", "failed")])
    before = session.queue.counts().attention

    session.after_hand_off(before - 1)

    assert isinstance(session.overlay, Picker)


def test_the_badge_adds_job_rows_to_queue_entries(mocker, tmp_path):
    _children(mocker, failing=("alpha-b",))
    group = dmodel.RepoGroup(
        "alpha",
        str(tmp_path),
        None,
        [
            _job_container("alpha-a", "creating"),
            _job_container("alpha-c", "failed"),
            ci("alpha-b", "alpha"),
        ],
    )
    session, terminal = bare_session(mocker, [group])
    terminal.defer = True
    session.run_dashboard_command("alpha-b", "container", ["start", "alpha-b"])
    # one live job + one running entry; one failed job
    assert session.queue.counts() == QueueCounts(running=2, attention=1)
    terminal.finish_pending()
    # the entry failed: attention = failed entry + failed job
    assert session.queue.counts() == QueueCounts(running=1, attention=2)


def _pilot_two_entries(mocker, tmp_path):  # type: ignore[no-untyped-def]
    children: list[_GatedChild] = []

    def spawn(argv, *, cwd, env, prompt_sock):  # type: ignore[no-untyped-def]
        child = _GatedChild(prompt_sock)
        children.append(child)
        return child

    from tests.dashboard_pilot import make_app

    app = make_app(mocker, [_pilot_group(tmp_path)])
    app.threaded_actions = True
    app.action_runner = drunner.run_action
    app.action_spawn = spawn
    return app, children


def test_pilot_the_queue_cursor_stays_while_output_streams(mocker, tmp_path):
    import asyncio
    import time

    from jailbee.dashboard.tui.native import OverlayList, PickerBox
    from tests.dashboard_pilot import keys

    app, children = _pilot_two_entries(mocker, tmp_path)

    async def until(pilot, condition, what):  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + 5
        while not condition():
            assert time.monotonic() < deadline, what
            await pilot.pause(0.02)

    def box():  # type: ignore[no-untyped-def]
        found = app.frame.native_box
        assert isinstance(found, PickerBox)
        return found

    async def main() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("j")
            for _ in range(2):
                for key in ["!", *keys("start alpha-a"), "enter"]:
                    await pilot.press(key)
                await until(pilot, lambda: len(app.session.queue) >= len(children), "the entry")
            await until(pilot, lambda: len(children) == 2, "both children")
            await pilot.press("Q")
            await until(pilot, lambda: app.frame.native_box is not None, "the list")
            first = box()
            await pilot.press("j")
            assert first.query_one(OverlayList).highlighted == 1
            for n in range(5):
                children[0].print(f"chunk {n}\n")  # the older entry: the list's second row
                await until(
                    pilot,
                    lambda n=n: f"chunk {n}" in app.session.queue_picker().entries[1].label,
                    "the row's last line",
                )
                await pilot.pause(0.05)
                assert box() is first  # the box stays mounted
                assert first.query_one(OverlayList).highlighted == 1
            await pilot.press("enter")  # opens the cursor's entry, not a dropped key
            await pilot.pause(0.1)
            assert app.session.overlay.purpose == "queue-entry"
            for child in children:
                child.release()
            await until(pilot, lambda: all(e.finished for e in app.session.queue), "the finish")

    asyncio.run(asyncio.wait_for(main(), 30))


def test_pilot_the_queue_cursor_follows_its_entry_as_job_rows_come_and_go(mocker, tmp_path):
    import asyncio
    import time

    from jailbee.dashboard.tui.native import OverlayList, PickerBox
    from tests.dashboard_pilot import keys

    app, children = _pilot_two_entries(mocker, tmp_path)
    (group,) = app.session.groups

    async def until(pilot, condition, what):  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + 5
        while not condition():
            assert time.monotonic() < deadline, what
            await pilot.pause(0.02)

    def highlighted_value() -> str:
        found = app.frame.native_box
        assert isinstance(found, PickerBox)
        row = found.query_one(OverlayList).highlighted
        assert row is not None
        return app.session.overlay.entries[row].value

    async def main() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("j")
            for _ in range(2):
                for key in ["!", *keys("start alpha-a"), "enter"]:
                    await pilot.press(key)
                await until(pilot, lambda: len(app.session.queue) >= len(children), "the entry")
            await until(pilot, lambda: len(children) == 2, "both children")
            await pilot.press("Q")
            await until(pilot, lambda: app.frame.native_box is not None, "the list")
            await pilot.press("j")
            selected = highlighted_value()
            original = list(group.containers)

            for containers in (
                [_job_container("alpha-a", "creating")],  # a job row appears above
                [_job_container("alpha-a", "failed")],  # it changes phase
                original,  # it goes
            ):
                group.containers[:] = containers
                app.session.tick()
                await pilot.pause(0.1)
                assert highlighted_value() == selected

            for child in children:
                child.release()
            await until(pilot, lambda: all(e.finished for e in app.session.queue), "the finish")

    asyncio.run(asyncio.wait_for(main(), 30))


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def output(self, index: int, chunk: str) -> None:
        self.calls.append(("output", index, chunk))

    def question(self, index: int, asking: bool) -> None:
        self.calls.append(("question", index, asking))


def test_output_never_waits_for_the_app_thread_and_is_coalesced():
    from jailbee.dashboard.tui.app import _OnAppThread

    inner = _Recorder()
    posted: list = []

    def blocked_app_thread(fn):  # type: ignore[no-untyped-def]
        raise AssertionError("output must not block on the app thread")

    relay = _OnAppThread(inner, blocked_app_thread, posted.append)
    for n in range(100):
        relay.output(0, f"{n}\n")  # returns at once although the app thread is busy
    relay.output(1, "other\n")

    assert len(posted) == 1  # one flush is scheduled, however many chunks arrive
    assert inner.calls == []
    posted[0]()
    assert [c[:2] for c in inner.calls] == [("output", 0), ("output", 1)]
    assert inner.calls[0][2] == "".join(f"{n}\n" for n in range(100))
    relay.output(0, "later\n")
    assert len(posted) == 2  # a new flush after the first ran


def test_a_question_drains_the_output_before_it():
    from jailbee.dashboard.tui.app import _OnAppThread

    inner = _Recorder()
    relay = _OnAppThread(inner, lambda fn: fn(), lambda fn: None)
    relay.output(0, "before\n")
    relay.question(0, True)

    assert inner.calls == [("output", 0, "before\n"), ("question", 0, True)]


def test_a_crashed_action_worker_still_settles_its_entries(mocker, tmp_path):
    from tests.dashboard_pilot import drive

    mocker.patch.object(drunner, "run_actions", side_effect=RuntimeError("boom"))
    seen: list[list[str]] = []

    def start_and_look(app) -> None:  # type: ignore[no-untyped-def]
        app.threaded_actions = False  # the worker body runs inline
        _pilot_start(app)
        seen.append([e.state for e in app.session.queue])

    drive(mocker, [start_and_look], [_pilot_group(tmp_path)])

    (states,) = seen
    assert "running" not in states  # settled, not left running and undismissable


# --- critical actions (#146 ticket 14) ----------------------------------------------

CRITICAL_ARGVS = [
    ["merge", "alpha-a"],
    ["git", "push", "alpha-a", "--merge"],
    ["git", "pull", "alpha-a"],
    ["pr", "push", "alpha-a"],
]


def _hand_off_attempt(session):  # type: ignore[no-untyped-def]
    """A foreground hand-off through the session; returns what it answered."""
    return session.run_dashboard_command(
        "alpha-a", "container", ["shell", "alpha-a"], foreground=True
    )


@pytest.mark.parametrize("argv", CRITICAL_ARGVS)
def test_a_critical_action_opens_its_live_modal_and_refuses_hand_offs(mocker, tmp_path, argv):
    mocker.patch.object(tsession, "_run_cli_foreground", return_value=0)
    session, terminal = _one_repo(mocker, tmp_path, defer=True)

    session.run_dashboard_command("alpha-a", "container", argv)

    (entry,) = _entries(session)
    assert entry.critical and entry.foreground
    ((title, text, failed),) = terminal.shown  # opened by itself, expanded and live
    assert title == entry.command and text == "(no output yet)" and not failed
    assert _hand_off_attempt(session) is None
    assert terminal.handed == []
    assert session.notice is not None
    assert "continue in the background" in session.notice


def test_continue_in_background_allows_hand_offs_and_keeps_the_entry(mocker, tmp_path):
    mocker.patch.object(tsession, "_run_cli_foreground", return_value=0)
    session, terminal = _one_repo(mocker, tmp_path, defer=True)
    session.run_dashboard_command("alpha-a", "container", ["merge", "alpha-a"])
    on_background = terminal.modal

    on_background()

    (entry,) = _entries(session)
    assert entry.state == "running" and not entry.foreground
    assert _hand_off_attempt(session) == 0
    assert len(terminal.handed) == 1


def test_closing_the_modal_of_a_running_critical_action_continues_it_in_background(
    mocker, tmp_path
):
    session, terminal = _one_repo(mocker, tmp_path, defer=True)
    session.run_dashboard_command("alpha-a", "container", ["git", "pull", "alpha-a"])

    terminal.modal()  # Esc / Enter

    (entry,) = _entries(session)
    assert not entry.foreground and entry.state == "running"


def test_a_critical_action_that_ends_in_the_modal_keeps_its_result_there(mocker, tmp_path):
    _children(mocker)
    session, terminal = _one_repo(mocker, tmp_path, defer=True)
    session.run_dashboard_command("alpha-a", "container", ["merge", "alpha-a"])

    terminal.finish_pending()

    (entry,) = _entries(session)
    assert entry.state == "done"
    assert len(terminal.shown) == 1  # no second output screen on top of the modal
    assert terminal.updated == ["output of jailbee merge alpha-a\n"]  # the final result, live


def test_a_critical_action_sent_to_background_pops_no_output_when_it_ends(mocker, tmp_path):
    _children(mocker, failing=("alpha-a",))
    session, terminal = _one_repo(mocker, tmp_path, defer=True)
    session.run_dashboard_command("alpha-a", "container", ["merge", "alpha-a"])
    terminal.modal()

    terminal.finish_pending()

    (entry,) = _entries(session)
    assert entry.state == "failed" and entry.needs_attention
    assert len(terminal.shown) == 1  # only the modal it opened with


def test_non_critical_verbs_open_no_modal_and_never_block(mocker, tmp_path):
    mocker.patch.object(tsession, "_run_cli_foreground", return_value=0)
    session, terminal = _one_repo(mocker, tmp_path, defer=True)

    for argv in (["start", "alpha-a"], ["git", "push", "alpha-a"], ["pr", "open", "alpha-a"]):
        session.run_dashboard_command("alpha-a", "container", argv)

    assert [e.critical for e in _entries(session)] == [False, False, False]
    assert getattr(terminal, "shown", []) == []
    assert _hand_off_attempt(session) == 0


def test_two_critical_actions_each_get_an_entry_and_the_block_holds_while_any_is_foreground(
    mocker, tmp_path
):
    mocker.patch.object(tsession, "_run_cli_foreground", return_value=0)
    session, terminal = _one_repo(mocker, tmp_path, defer=True)
    session.run_dashboard_command("alpha-a", "container", ["merge", "alpha-a"])
    session.run_dashboard_command("alpha-a", "container", ["git", "pull", "alpha-a"])

    assert len(_entries(session)) == 2
    assert len(terminal.shown) == 1  # one modal at a time
    terminal.modal()  # the first goes to the background; the second's modal opens
    assert [t for t, _, _ in terminal.shown] == [
        "jailbee merge alpha-a",
        "jailbee git pull alpha-a",
    ]
    assert _hand_off_attempt(session) is None  # the second is still in the foreground

    terminal.modal()

    assert _hand_off_attempt(session) == 0


def _pilot_merge(app):  # type: ignore[no-untyped-def]
    app.session.run_dashboard_command("alpha-a", "container", ["merge", "alpha-a"])


def test_pilot_a_critical_action_opens_its_modal_and_the_result_stays_until_closed(
    mocker, tmp_path
):
    from jailbee.dashboard.tui import prompt_modals as pm
    from tests.dashboard_pilot import drive

    _children(mocker)
    seen: list[object] = []
    run = drive(
        mocker,
        [
            _pilot_merge,
            lambda app: seen.append((app.screen, app.screen.text)),  # still there, finished
            "enter",
            lambda app: seen.append(app.screen),
        ],
        [_pilot_group(tmp_path)],
        screens=True,
    )

    screen, text = seen[0]
    assert isinstance(screen, pm.OutputScreen)
    assert text == "output of jailbee merge alpha-a\n"
    assert "b, Enter or Esc continues in the background" in run.screens[1]
    assert not isinstance(seen[1], pm.OutputScreen)  # closed by the user
    (entry,) = list(run.app.session.queue)
    assert entry.state == "done" and not entry.foreground


def test_pilot_b_continues_a_critical_action_in_the_background(mocker, tmp_path):
    from jailbee.dashboard.tui import prompt_modals as pm
    from tests.dashboard_pilot import drive

    _children(mocker)
    seen: list[object] = []
    run = drive(
        mocker,
        [_pilot_merge, "b", lambda app: seen.append(app.screen)],
        [_pilot_group(tmp_path)],
    )

    assert not isinstance(seen[0], pm.OutputScreen)
    (entry,) = list(run.app.session.queue)
    assert not entry.foreground
