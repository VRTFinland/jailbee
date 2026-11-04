import multiprocessing
from datetime import UTC, datetime

import pytest

from jailbee import autostart_outcomes as outcomes


@pytest.fixture(autouse=True)
def state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def test_record_then_read_round_trip():
    now = datetime(2026, 11, 4, tzinfo=UTC)
    outcomes.record("c", "on_start", "s", "a", "fail", now=now)
    assert outcomes.read("c") == {
        "on_start": {"s": {"a": outcomes.StepOutcome("fail", now.isoformat())}}
    }


def test_rerun_ok_clears_fail_and_keeps_others():
    outcomes.record("c", "on_start", "s", "a", "fail")
    outcomes.record("c", "on_start", "s", "b", "fail")
    outcomes.record("c", "on_start", "s", "a", "ok")
    assert outcomes.failed_steps(outcomes.read("c")) == [("on_start", "s", "b")]
    assert outcomes.failed_count("c") == 1


def test_corrupt_file_reads_empty():
    path = outcomes.outcomes_path("c")
    path.parent.mkdir(parents=True)
    path.write_text("{")
    assert outcomes.read("c") == {}


def test_malformed_entries_are_skipped():
    path = outcomes.outcomes_path("c")
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"on_start":{"s":{"good":{"state":"ok","at":"now"},"bad":{"state":"no","at":"now"},"broken":null},"bad":[]},"bad":null}'
    )
    assert outcomes.read("c") == {"on_start": {"s": {"good": outcomes.StepOutcome("ok", "now")}}}


def test_record_on_readonly_dir_does_not_raise(monkeypatch):
    def fail(*args):
        raise OSError("readonly")

    monkeypatch.setattr(outcomes.os, "replace", fail)
    outcomes.record("c", "on_start", "s", "a", "ok")
    assert outcomes.read("c") == {}


def test_start_counts_as_failed():
    outcomes.record("c", "on_start", "s", "a", "start")
    assert outcomes.failed_count("c") == 1


def test_remove_missing_is_silent():
    outcomes.remove("missing")


def _paused_writer(read_done, resume):
    original_read = outcomes.read

    def paused_read(container):
        result = original_read(container)
        read_done.set()
        if not resume.wait(10):
            raise RuntimeError("writer not released")
        return result

    outcomes.read = paused_read
    outcomes.record("c", "on_start", "s", "a", "fail")


def _other_writer(connection):
    import fcntl

    original_read = outcomes.read
    original_flock = fcntl.flock

    def observed_read(container):
        result = original_read(container)
        connection.send("read")
        return result

    def observed_flock(fd, operation):
        if operation == fcntl.LOCK_EX:
            connection.send("locking")
        return original_flock(fd, operation)

    outcomes.read = observed_read
    fcntl.flock = observed_flock
    outcomes.record("c", "on_start", "s", "b", "ok")


def test_concurrent_process_writers_preserve_both_outcomes():
    ctx = multiprocessing.get_context("fork")
    read_done, resume = ctx.Event(), ctx.Event()
    parent, child = ctx.Pipe(duplex=False)
    first = ctx.Process(target=_paused_writer, args=(read_done, resume))
    second = ctx.Process(target=_other_writer, args=(child,))
    first.start()
    try:
        assert read_done.wait(10)
        second.start()
        assert parent.poll(10)
        first_action = parent.recv()
        if first_action == "read":
            second.join(timeout=10)
    finally:
        resume.set()
        first.join(timeout=10)
        if second.pid is not None:
            second.join(timeout=10)
        for process in (first, second):
            if process.is_alive():
                process.terminate()
                process.join()
    assert first.exitcode == 0
    assert second.exitcode == 0
    steps = outcomes.read("c")["on_start"]["s"]
    assert {name: value.state for name, value in steps.items()} == {"a": "fail", "b": "ok"}
