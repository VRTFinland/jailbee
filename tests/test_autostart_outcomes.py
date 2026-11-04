from datetime import datetime, timezone

import pytest

from jailbee import autostart_outcomes as outcomes


@pytest.fixture(autouse=True)
def state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def test_record_then_read_round_trip():
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    outcomes.record("c", "on_start", "s", "a", "fail", now=now)
    assert outcomes.read("c") == {"on_start": {"s": {"a": outcomes.StepOutcome("fail", now.isoformat())}}}


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
    path.write_text('{"on_start":{"s":{"good":{"state":"ok","at":"now"},"bad":{"state":"no","at":"now"},"broken":null},"bad":[]},"bad":null}')
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
