"""The live-session match behind the AGENT column. Pure: no /proc, no files
except for `read_sessions`, which only needs a fake adapter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jailbee import agent_status
from jailbee.accounts.adapters import base
from jailbee.accounts.models import AgentActivity, AgentSession

T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _s(
    pid: int,
    start: int,
    state: str = "busy",
    *,
    agent: str = "claude",
    since: datetime | None = T0,
    waiting_for: str | None = None,
    updated_at: int | None = None,
) -> AgentSession:
    return AgentSession(
        agent=agent,
        pid=pid,
        proc_start=start,
        state=state,
        waiting_for=waiting_for,
        since=since,
        updated_at=updated_at,
    )


def _match(sessions, processes, nspids):
    return agent_status.match_sessions(sessions, processes, nspids.get)


def test_each_container_is_matched_against_its_own_sessions():
    sessions = {"a": [_s(10, 500, "waiting")], "b": [_s(20, 600, "busy")]}
    out = _match(sessions, {"a": {1010: 500}, "b": {2020: 600}}, {1010: 10, 2020: 20})

    assert [s.state for s in out["a"]] == ["waiting"]
    assert [s.state for s in out["b"]] == ["busy"]


def test_another_containers_identical_session_is_never_matched():
    """Same namespace pid, same start time, two containers. `a` recorded
    nothing, so `b`'s file must not light up `a`'s process — the collision a
    shared registry allowed, and the forgery it made possible."""
    out = _match(
        {"b": [_s(10, 500, "busy")]},
        {"a": {1010: 500}, "b": {2020: 500}},
        {1010: 10, 2020: 10},
    )

    assert out["a"] == ()
    assert [s.state for s in out["b"]] == ["busy"]


def test_identical_sessions_in_two_containers_keep_their_own_state():
    out = _match(
        {"a": [_s(10, 500, "waiting")], "b": [_s(10, 500, "busy")]},
        {"a": {1010: 500}, "b": {2020: 500}},
        {1010: 10, 2020: 10},
    )

    assert [(s.state, s.count) for s in out["a"]] == [("waiting", 1)]
    assert [(s.state, s.count) for s in out["b"]] == [("busy", 1)]


def test_a_stale_file_is_not_live():
    """No process has its start time: the session's process is gone."""
    out = _match({"a": [_s(10, 500)]}, {"a": {1010: 999}}, {1010: 10})

    assert out == {"a": ()}


def test_a_recycled_pid_is_not_live():
    """Same namespace pid, different start time: a different process."""
    out = _match({"a": [_s(10, 500)]}, {"a": {1010: 501}}, {1010: 10})

    assert out["a"] == ()


def test_same_start_time_but_another_namespace_pid_is_not_live():
    out = _match({"a": [_s(10, 500)]}, {"a": {1010: 500}}, {1010: 11})

    assert out["a"] == ()


def test_an_unreadable_nspid_is_not_live():
    out = _match({"a": [_s(10, 500)]}, {"a": {1010: 500}}, {})

    assert out["a"] == ()


def test_nspid_is_read_only_for_start_time_candidates():
    """One status read per live session, not one per process."""
    asked: list[int] = []

    def nspid(pid: int) -> int | None:
        asked.append(pid)
        return 10

    agent_status.match_sessions({"a": [_s(10, 500)]}, {"a": {1: 500, 2: 999, 3: 777}}, nspid)

    assert asked == [1]


def test_no_sessions_reads_no_nspid_at_all():
    asked: list[int] = []
    out = agent_status.match_sessions({}, {"a": {1: 500}}, lambda p: asked.append(p) or None)

    assert out == {"a": ()}
    assert asked == []


def test_the_most_urgent_session_speaks_for_the_agent_and_all_are_counted():
    sessions = [_s(1, 11, "idle"), _s(2, 12, "waiting", waiting_for="input needed"), _s(3, 13)]
    out = _match({"a": sessions}, {"a": {101: 11, 102: 12, 103: 13}}, {101: 1, 102: 2, 103: 3})

    assert out["a"] == (
        agent_status.AgentSummary(
            agent="claude", state="waiting", since=T0, waiting_for="input needed", count=3
        ),
    )


def test_among_equals_the_longest_wait_wins():
    early, late = T0, T0 + timedelta(minutes=5)
    sessions = [_s(1, 11, "waiting", since=late), _s(2, 12, "waiting", since=early)]
    out = _match({"a": sessions}, {"a": {101: 11, 102: 12}}, {101: 1, 102: 2})

    assert out["a"][0].since == early


def test_an_undated_session_ranks_after_a_dated_one_of_the_same_state():
    sessions = [_s(1, 11, "busy", since=None), _s(2, 12, "busy", since=T0)]
    out = _match({"a": sessions}, {"a": {101: 11, 102: 12}}, {101: 1, 102: 2})

    assert out["a"][0].since == T0


def test_shell_ranks_between_busy_and_idle():
    sessions = [_s(1, 11, "idle"), _s(2, 12, "shell")]
    out = _match({"a": sessions}, {"a": {101: 11, 102: 12}}, {101: 1, 102: 2})
    assert out["a"][0].state == "shell"

    sessions = [_s(1, 11, "shell"), _s(2, 12, "busy")]
    out = _match({"a": sessions}, {"a": {101: 11, 102: 12}}, {101: 1, 102: 2})
    assert out["a"][0].state == "busy"


def test_an_unknown_state_ranks_after_idle():
    sessions = [_s(1, 11, "compacting"), _s(2, 12, "idle")]
    out = _match({"a": sessions}, {"a": {101: 11, 102: 12}}, {101: 1, 102: 2})

    assert out["a"][0].state == "idle"


def test_an_unknown_state_alone_is_shown_raw():
    out = _match({"a": [_s(1, 11, "compacting")]}, {"a": {101: 11}}, {101: 1})

    assert out["a"][0].state == "compacting"


def test_two_files_claiming_one_process_count_once_and_the_newest_wins():
    sessions = [
        _s(1, 11, "busy", updated_at=100),
        _s(1, 11, "waiting", updated_at=200),
        _s(1, 11, "idle", updated_at=None),
    ]
    out = _match({"a": sessions}, {"a": {101: 11}}, {101: 1})

    assert [(s.state, s.count) for s in out["a"]] == [("waiting", 1)]


def test_several_agents_are_ordered_most_urgent_first():
    sessions = [
        _s(1, 11, "busy", agent="codex"),
        _s(2, 12, "waiting"),
        _s(3, 13, "busy", agent="aider"),
    ]
    out = _match({"a": sessions}, {"a": {101: 11, 102: 12, 103: 13}}, {101: 1, 102: 2, 103: 3})

    assert [s.agent for s in out["a"]] == ["claude", "aider", "codex"]


class _SessionsAdapter:
    def __init__(self, by_home: dict[Path, list[AgentSession]], *, boom: bool = False) -> None:
        self.by_home = by_home
        self.boom = boom
        self.asked: list[Path] = []

    def read_sessions(self, home: Path) -> list[AgentSession]:
        self.asked.append(home)
        if self.boom:
            raise RuntimeError("parser bug")
        return self.by_home.get(home, [])


def test_read_sessions_groups_by_container_and_reads_each_home_once(monkeypatch, tmp_path):
    a_home, b_home = tmp_path / "a", tmp_path / "b"
    adapter = _SessionsAdapter({a_home: [_s(1, 11)], b_home: [_s(2, 22)]})
    monkeypatch.setitem(base.ADAPTERS, "fakeagent", adapter)

    got = agent_status.read_sessions(
        [("a", "fakeagent", a_home), ("a", "fakeagent", a_home), ("b", "fakeagent", b_home)]
    )

    assert got == {"a": [_s(1, 11)], "b": [_s(2, 22)]}
    assert adapter.asked == [a_home, b_home]


def test_read_sessions_leaves_out_a_container_with_nothing_recorded(monkeypatch, tmp_path):
    monkeypatch.setitem(base.ADAPTERS, "fakeagent", _SessionsAdapter({}))

    assert agent_status.read_sessions([("a", "fakeagent", tmp_path)]) == {}


def test_read_sessions_skips_an_agent_with_no_adapter(tmp_path):
    assert agent_status.read_sessions([("a", "no-such-agent", tmp_path)]) == {}


def test_read_sessions_survives_an_adapter_that_raises(monkeypatch, tmp_path):
    """The format is undocumented; a bug in one parser must not end the tick
    or hide another agent's sessions."""
    monkeypatch.setitem(base.ADAPTERS, "broken", _SessionsAdapter({}, boom=True))
    monkeypatch.setitem(base.ADAPTERS, "fine", _SessionsAdapter({tmp_path: [_s(1, 11)]}))

    got = agent_status.read_sessions([("a", "broken", tmp_path), ("a", "fine", tmp_path)])

    assert got == {"a": [_s(1, 11)]}


def test_the_same_namespace_pid_in_two_containers_keeps_both_sessions():
    """Inner pids are namespace-local, so two containers routinely share one.
    Only the start time tells the sessions apart."""
    sessions = {"a": [_s(10, 500, "waiting")], "b": [_s(10, 600, "busy")]}
    out = _match(sessions, {"a": {1010: 500}, "b": {2020: 600}}, {1010: 10, 2020: 10})

    assert [(s.state, s.count) for s in out["a"]] == [("waiting", 1)]
    assert [(s.state, s.count) for s in out["b"]] == [("busy", 1)]


def test_two_sessions_with_one_start_time_are_told_apart_by_pid():
    sessions = [_s(10, 500, "waiting"), _s(11, 500, "busy")]
    out = _match({"a": sessions}, {"a": {1010: 500}}, {1010: 11})

    assert [(s.state, s.count) for s in out["a"]] == [("busy", 1)]


def test_no_containers_is_an_empty_result():
    assert _match({"a": [_s(10, 500)]}, {}, {}) == {}


def test_live_sessions_are_keyed_by_pid_and_start_together():
    """Within one container a pid is unique in practice, but the key must not
    rely on it: distinct (pid, start) pairs are distinct sessions."""
    sessions = [_s(10, 500, "waiting"), _s(10, 600, "busy")]
    out = _match({"a": sessions}, {"a": {1: 500, 2: 600}}, {1: 10, 2: 10})

    assert [s.count for s in out["a"]] == [2]


def test_summary_has_no_activity_without_a_lookup():
    out = _match({"a": [_s(10, 500)]}, {"a": {1010: 500}}, {1010: 10})

    assert out["a"][0].activity is None


def test_the_lookup_is_asked_about_every_live_session_in_rank_order_with_its_host_pid():
    calls: list[tuple[str, int, int]] = []

    def lookup(container, session, host_pid):
        calls.append((container, session.pid, host_pid))
        return None

    agent_status.match_sessions(
        {"a": [_s(10, 500, "idle"), _s(11, 600, "waiting")]},
        {"a": {1010: 500, 1111: 600}},
        {1010: 10, 1111: 11}.get,
        lookup,
    )

    assert calls == [("a", 11, 1111), ("a", 10, 1010)]  # the waiting one first


def _two_sessions(lookup):
    """`claude` and `claude-jb`: same state, the first has the longer-standing one."""
    return agent_status.match_sessions(
        {
            "a": [
                _s(10, 500, "idle", since=T0 - timedelta(hours=1)),
                _s(11, 600, "idle", since=T0),
            ]
        },
        {"a": {1010: 500, 1111: 600}},
        {1010: 10, 1111: 11}.get,
        lookup,
    )


def test_a_session_without_a_transcript_does_not_hide_the_one_with():
    """The bug: the long-idle session wins `_rank` and has nothing to show."""
    found = AgentActivity("Bash  ls", "hi", modified=100.0)

    def lookup(container, session, host_pid):
        return found if session.pid == 11 else None

    (summary,) = _two_sessions(lookup)["a"]

    assert summary.activity is not None
    assert (summary.activity.last_tool, summary.activity.last_message) == ("Bash  ls", "hi")


def test_the_most_recently_written_transcript_wins_regardless_of_state_or_rank():
    def lookup(container, session, host_pid):
        return AgentActivity(f"pid{session.pid}", None, modified={10: 100.0, 11: 200.0}[session.pid])

    out = agent_status.match_sessions(
        {"a": [_s(10, 500, "waiting"), _s(11, 600, "idle")]},
        {"a": {1010: 500, 1111: 600}},
        {1010: 10, 1111: 11}.get,
        lookup,
    )

    (summary,) = out["a"]
    assert summary.activity is not None
    assert summary.activity.last_tool == "pid11"  # idle, but written more recently
    assert summary.state == "waiting"  # the AGENT column is still the rank top


def test_the_activity_carries_its_own_sessions_state_and_since():
    def lookup(container, session, host_pid):
        return AgentActivity("t", None, modified=1.0) if session.pid == 11 else None

    (summary,) = agent_status.match_sessions(
        {"a": [_s(10, 500, "waiting", since=T0), _s(11, 600, "idle", since=T0 - timedelta(hours=2))]},
        {"a": {1010: 500, 1111: 600}},
        {1010: 10, 1111: 11}.get,
        lookup,
    )["a"]

    assert summary.activity is not None
    assert (summary.activity.state, summary.activity.since) == ("idle", T0 - timedelta(hours=2))
    assert (summary.state, summary.since) == ("waiting", T0)


@pytest.mark.parametrize("modified", [None, 100.0])
def test_equal_or_unknown_mtimes_fall_back_to_rank_order(modified):
    def lookup(container, session, host_pid):
        return AgentActivity(f"pid{session.pid}", None, modified=modified)

    (summary,) = _two_sessions(lookup)["a"]

    assert summary.activity is not None
    assert summary.activity.last_tool == "pid10"  # the longer-standing idle one


def test_the_agent_column_is_unchanged_by_the_activity_choice():
    def lookup(container, session, host_pid):
        return AgentActivity("t", None, modified=1.0) if session.pid == 11 else None

    (summary,) = _two_sessions(lookup)["a"]

    assert (summary.state, summary.since, summary.waiting_for, summary.count) == (
        "idle",
        T0 - timedelta(hours=1),
        None,
        2,
    )


def test_no_readable_session_means_no_activity():
    (summary,) = _two_sessions(lambda c, s, p: None)["a"]

    assert summary.activity is None


def test_the_lookup_is_not_asked_for_a_container_with_no_live_session():
    def lookup(container, session, host_pid):
        raise AssertionError("no live session, nothing to look up")

    out = agent_status.match_sessions(
        {"a": [_s(10, 500)]}, {"a": {1010: 999}, "b": {}}, {1010: 10}.get, lookup
    )

    assert out == {"a": (), "b": ()}


def test_each_agent_of_a_container_is_looked_up_separately():
    seen: list[str] = []

    def lookup(container, session, host_pid):
        seen.append(session.agent)
        return None

    agent_status.match_sessions(
        {"a": [_s(10, 500, agent="claude"), _s(11, 600, agent="codex")]},
        {"a": {1010: 500, 1111: 600}},
        {1010: 10, 1111: 11}.get,
        lookup,
    )

    assert sorted(seen) == ["claude", "codex"]
