"""Bulk actions in the terminal dashboard."""

from __future__ import annotations

from jailbee.dashboard import model as dmodel
from jailbee.dashboard.bulk import plan_bulk
from jailbee.dashboard.tui import session as tsession
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import SyncJobs, bare_session


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
