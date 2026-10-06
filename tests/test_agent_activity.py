"""The reader that turns live sessions into activity, and how it is worded."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jailbee import agent_activity as aa
from jailbee.accounts.adapters import base
from jailbee.accounts.models import ActivityPaths, AgentActivity, AgentSession
from jailbee.agent_status import AgentSummary, match_sessions
from jailbee.procstat import ProcSample

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
HOME = Path("/shared/claude")
_CANNED = AgentActivity("Bash  ls", "hi", 1)


def _session(sid: str | None = "sid-1", agent: str = "claude") -> AgentSession:
    return AgentSession(agent, 10, 500, "busy", None, None, None, session_id=sid)


class _Adapter:
    """Records what the reader asks, answers with canned activity."""

    name = "claude"

    def __init__(self, activity: AgentActivity | None = _CANNED) -> None:
        self.activity = activity
        self.locates = 0
        self.reads = 0
        self.locatable = True

    def locate_activity(self, config_home: Path, session: AgentSession) -> ActivityPaths | None:
        self.locates += 1
        if not self.locatable:
            return None
        return ActivityPaths(config_home / "t.jsonl", config_home / "sub")

    def read_activity(self, paths: ActivityPaths, *, now: float) -> AgentActivity | None:
        self.reads += 1
        return self.activity


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> _Adapter:
    fake = _Adapter()
    monkeypatch.setattr(base, "get_adapter", lambda name: fake)
    return fake


def _lookup(reader: aa.ActivityReader, procs: dict[int, ProcSample] | None = None) -> Any:
    return reader.lookup_for(
        {("c", "claude"): HOME}, lambda container: procs if procs is not None else {}
    )


def test_the_shell_count_is_the_session_processs_shell_children(adapter: _Adapter) -> None:
    reader = aa.ActivityReader()
    procs = {
        1010: ProcSample("claude", 0, 500, ppid=1),
        1011: ProcSample("bash", 0, 600, ppid=1010),
        1012: ProcSample("bash", 0, 601, ppid=1010),
        1013: ProcSample("bash", 0, 602, ppid=7),
    }

    got = _lookup(reader, procs)("c", _session(), 1010)

    assert got == AgentActivity("Bash  ls", "hi", subagents=1, shells=2)


def test_the_path_is_located_once_per_session_not_once_per_tick(adapter: _Adapter) -> None:
    reader = aa.ActivityReader()
    lookup = _lookup(reader)

    for _ in range(3):
        reader.begin()
        lookup("c", _session(), 1010)
        reader.finish()

    assert adapter.locates == 1
    assert adapter.reads == 3  # the transcript itself is read every tick


def test_a_session_that_stopped_being_live_is_forgotten(adapter: _Adapter) -> None:
    reader = aa.ActivityReader()
    lookup = _lookup(reader)
    reader.begin()
    lookup("c", _session("a"), 1010)
    lookup("c", _session("b"), 1011)
    reader.finish()
    assert reader.cached == 2

    reader.begin()
    lookup("c", _session("a"), 1010)
    reader.finish()

    assert reader.cached == 1


def test_an_unlocatable_transcript_is_retried_next_tick(adapter: _Adapter) -> None:
    reader = aa.ActivityReader()
    lookup = _lookup(reader)
    adapter.locatable = False
    reader.begin()
    assert lookup("c", _session(), 1010) is None
    reader.finish()
    assert reader.cached == 0

    adapter.locatable = True
    reader.begin()
    assert lookup("c", _session(), 1010) is not None
    assert adapter.locates == 2


def test_a_transcript_that_reads_as_nothing_is_located_again_next_tick(
    adapter: _Adapter,
) -> None:
    reader = aa.ActivityReader()
    lookup = _lookup(reader)
    adapter.activity = None
    reader.begin()
    assert lookup("c", _session(), 1010) is None
    reader.finish()
    assert reader.cached == 0

    adapter.activity = _CANNED
    reader.begin()
    assert lookup("c", _session(), 1010) is not None
    assert adapter.locates == 2


def test_a_processes_callable_that_raises_never_ends_the_tick(adapter: _Adapter) -> None:
    def boom(container: str) -> dict[int, ProcSample]:
        raise OSError("proc gone")

    reader = aa.ActivityReader()
    lookup = reader.lookup_for({("c", "claude"): HOME}, boom)

    assert lookup("c", _session(), 1010) is None


def test_two_containers_with_one_session_id_are_cached_apart(adapter: _Adapter) -> None:
    reader = aa.ActivityReader()
    lookup = reader.lookup_for({("c", "claude"): HOME, ("d", "claude"): HOME}, lambda c: {})
    reader.begin()
    lookup("c", _session(), 1)
    lookup("d", _session(), 2)
    reader.finish()

    assert reader.cached == 2


def test_nothing_is_read_without_a_session_id_or_a_config_home(adapter: _Adapter) -> None:
    reader = aa.ActivityReader()
    lookup = _lookup(reader)

    assert lookup("c", _session(sid=None), 1) is None
    assert lookup("unknown-container", _session(), 1) is None
    assert (adapter.locates, adapter.reads) == (0, 0)


def test_an_adapter_that_raises_never_ends_the_tick(
    adapter: _Adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("format changed")

    monkeypatch.setattr(adapter, "read_activity", boom)

    assert _lookup(aa.ActivityReader())("c", _session(), 1) is None


def test_an_agent_with_no_adapter_has_no_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> None:
        raise KeyError(name)

    monkeypatch.setattr(base, "get_adapter", missing)

    assert _lookup(aa.ActivityReader())("c", _session(), 1) is None


def test_the_clock_is_what_the_adapter_is_told(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []

    class Spy(_Adapter):
        def read_activity(self, paths: ActivityPaths, *, now: float) -> AgentActivity | None:
            seen.append(now)
            return self.activity

    monkeypatch.setattr(base, "get_adapter", lambda name: Spy())

    _lookup(aa.ActivityReader(clock=lambda: 123.5))("c", _session(), 1)

    assert seen == [123.5]


def _summary(**kw: Any) -> AgentSummary:
    base_kw: dict[str, Any] = {
        "agent": "claude",
        "state": "busy",
        "since": NOW - timedelta(minutes=2),
        "waiting_for": None,
        "count": 1,
        "activity": AgentActivity("Bash  uv run pytest -x", "done", 2, 1),
    }
    return AgentSummary(**(base_kw | kw))


def test_describe_words_the_three_lines() -> None:
    text = aa.describe(_summary(), NOW)

    assert text is not None
    assert text.lines() == (
        "busy 2m · ~2 subagents · 1 shell",
        "↳ Bash  uv run pytest -x",
        "“done”",
    )


def test_describe_singular_and_omitted_counts() -> None:
    one = aa.describe(_summary(activity=AgentActivity(None, None, 1, 0)), NOW)
    unknown = aa.describe(_summary(activity=AgentActivity(None, None, None, None)), NOW)

    assert one is not None and unknown is not None
    assert one.lines() == ("busy 2m · ~1 subagent",)  # 0 shells is not worth a word
    assert unknown.lines() == ("busy 2m",)


def test_describe_without_activity_is_none() -> None:
    assert aa.describe(_summary(activity=None), NOW) is None


def test_describe_a_state_without_a_date_or_from_the_future_has_no_duration() -> None:
    undated = aa.describe(_summary(since=None), NOW)
    future = aa.describe(_summary(since=NOW + timedelta(hours=1)), NOW)

    assert undated is not None and future is not None
    assert undated.head == "busy · ~2 subagents · 1 shell"
    assert future.head == "busy · ~2 subagents · 1 shell"


def test_describe_head_uses_the_activitys_own_session_state_and_since() -> None:
    mine = AgentActivity(
        "Bash  ls", None, state="idle", since=NOW - timedelta(hours=1), modified=1.0
    )
    text = aa.describe(_summary(state="waiting", activity=mine), NOW)

    assert text is not None
    assert text.head == "idle 1h"


def test_describe_does_not_borrow_the_summary_date_for_an_undated_activity() -> None:
    text = aa.describe(_summary(activity=AgentActivity("t", None, state="idle")), NOW)

    assert text is not None
    assert text.head == "idle"


def test_all_live_sessions_stay_cached_and_missing_transcripts_are_retried(
    adapter: _Adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    def locate(home: Path, session: AgentSession) -> ActivityPaths | None:
        if session.session_id == "missing":
            return None
        return ActivityPaths(home / f"{session.session_id}.jsonl", home / "sub")

    monkeypatch.setattr(adapter, "locate_activity", locate)
    reader = aa.ActivityReader()
    lookup = _lookup(reader)
    live = [
        replace(_session(sid), pid=pid, proc_start=pid)
        for pid, sid in enumerate(("a", "b", "missing"), start=10)
    ]

    def tick(sessions: list[AgentSession]) -> None:
        reader.begin()
        match_sessions(
            {"c": sessions}, {"c": {s.pid: s.proc_start for s in sessions}}, lambda p: p, lookup
        )
        reader.finish()

    tick(live)
    assert reader.cached == 2  # both readable sessions, not just the rank top
    tick(live)
    assert reader.cached == 2
    tick(live[1:])
    assert reader.cached == 1  # only the dead session is evicted
    monkeypatch.setattr(
        adapter, "locate_activity", lambda home, session: ActivityPaths(home / "t", home)
    )
    tick(live[1:])
    assert reader.cached == 2  # the missing transcript is retried without a new session
