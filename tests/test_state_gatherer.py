from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jailbee.dashboard.model import RepoGroup
from jailbee.db import get_engine
from jailbee.git_status import GitStatus
from jailbee.global_config import DashboardRefresh
from jailbee.lifecycle import ContainerInfo
from jailbee.outbox.sent import SentCounts
from jailbee.state_service.gatherer import UPDATE_INTERVAL, Cadence, Gatherer, refresh_due
from jailbee.state_service.protocol import GatherError, Snapshot
from jailbee.update_check import UpdateNotice

CAD = Cadence(interval=3.0, git_interval=10.0, git=True)


def due(**kw):
    args = {
        "now": 100.0,
        "last_base": 99.0,
        "last_full": 95.0,
        "cadence": CAD,
        "active": True,
        "refresh": False,
        "fast": False,
    }
    return refresh_due(**(args | kw))


def test_cadence_floors_interval_and_git_interval():
    assert Cadence.from_config(DashboardRefresh(interval=0.1, git_interval=0.2, git=False)) == (
        Cadence(0.5, 0.5, False)
    )


def test_nothing_is_gathered_for_nobody():
    assert due(active=False, last_base=None) == (False, False)
    assert due(active=False, now=1000.0) == (False, False)


def test_a_refresh_gets_through_without_an_active_client():
    assert due(active=False, refresh=True) == (True, True)


def test_the_first_gather_is_base_only():
    assert due(last_base=None, last_full=None) == (True, False)


def test_git_follows_immediately_after_the_first_base_gather():
    assert due(last_full=None) == (True, True)


def test_base_and_git_on_their_own_intervals():
    assert due() == (False, False)
    assert due(now=102.0) == (True, False)
    assert due(now=105.0, last_base=104.0) == (True, True)


def test_git_disabled_never_probes():
    no_git = Cadence(3.0, 10.0, False)
    assert due(cadence=no_git, last_full=None) == (False, False)
    assert due(cadence=no_git, refresh=True) == (True, False)


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _ci(name, git=None):
    return ContainerInfo(name, "Running", "strict", None, None, repo="alpha", git_status=git)


def _gatherer(mocker, gather, clock, sent_counts=lambda: SentCounts()):
    mocker.patch("jailbee.state_service.gatherer.sample_activity")
    return Gatherer(
        mocker.Mock(),
        CAD,
        sent_counts=sent_counts,
        gather=gather,
        clock=clock,
        sleep=lambda _s: None,
        wall_clock=lambda: datetime(2026, 10, 4, tzinfo=UTC),
    )


def test_tick_returns_numbered_snapshots_with_the_git_setting(mocker):
    clock = _Clock()
    gather = mocker.Mock(side_effect=lambda *a, **k: [RepoGroup("alpha", "/a", None, [_ci("x")])])
    g = _gatherer(mocker, gather, clock)

    first = g.tick(active=True, refresh=False, roots=[Path("/a")])
    second = g.tick(active=True, refresh=False, roots=[Path("/a")])

    assert isinstance(first, Snapshot) and isinstance(second, Snapshot)
    assert (first.seq, second.seq) == (1, 2)
    assert first.git_enabled is True
    assert gather.call_args_list[0].kwargs == {"with_git": False, "parent_heads": {}}
    assert gather.call_args_list[1].kwargs == {"with_git": True, "parent_heads": {}}
    assert gather.call_args_list[0].args[1] == [Path("/a")]
    assert g.tick(active=True, refresh=False, roots=[]) is None  # nothing due yet


def test_a_base_gather_carries_the_last_git_status_forward(mocker):
    clock = _Clock()
    status = GitStatus("+1 -0", "clean", "0", "ok")
    rounds = iter([[_ci("x")], [_ci("x", status)], [_ci("x")]])
    g = _gatherer(mocker, lambda *a, **k: [RepoGroup("alpha", "/a", None, next(rounds))], clock)

    g.tick(active=True, refresh=False, roots=[])  # base
    g.tick(active=True, refresh=False, roots=[])  # git
    clock.t = 3.0
    third = g.tick(active=True, refresh=False, roots=[])  # base again

    assert third.groups[0].containers[0].git_status == status


def test_the_next_gather_gets_the_heads_the_last_git_pass_saw(mocker):
    """Children compare with their parent's HEAD from the previous pass; a
    stopped container's head is not offered, and base passes keep the heads."""
    from jailbee.host_target import ParentHead

    clock = _Clock()
    seen = GitStatus("", "clean", "0", "ok", head_sha="abc")
    stopped = dataclasses.replace(_ci("y", GitStatus("", "clean", "0", "ok", head_sha="old")))
    stopped.state = "Stopped"
    rounds = iter([[_ci("x")], [_ci("x", seen), stopped], [_ci("x")], [_ci("x")]])
    gather = mocker.Mock(
        side_effect=lambda *a, **k: [RepoGroup("alpha", "/a", None, next(rounds))]
    )
    g = _gatherer(mocker, gather, clock)

    g.tick(active=True, refresh=False, roots=[])  # base
    g.tick(active=True, refresh=False, roots=[])  # git
    clock.t = 3.0
    g.tick(active=True, refresh=False, roots=[])  # base
    clock.t = 11.0
    g.tick(active=True, refresh=False, roots=[])  # git

    expected = {"x": ParentHead("abc", datetime(2026, 10, 4, tzinfo=UTC).timestamp())}
    assert [c.kwargs["parent_heads"] for c in gather.call_args_list] == [
        {},
        {},
        expected,
        expected,
    ]


def test_the_sampler_is_primed_once(mocker):
    clock = _Clock()
    sample = mocker.patch("jailbee.state_service.gatherer.sample_activity")
    sleeps: list[float] = []
    g = Gatherer(mocker.Mock(), CAD, gather=lambda *a, **k: [], clock=clock, sleep=sleeps.append)

    g.tick(active=True, refresh=False, roots=[])
    g.tick(active=True, refresh=False, roots=[])

    assert sample.call_count == 3  # prime + reading, then one reading
    assert len(sleeps) == 1


def test_a_failed_gather_is_reported_and_retried_on_the_cadence(mocker):
    clock = _Clock()
    gather = mocker.Mock(side_effect=OSError("incus is down"))
    g = _gatherer(mocker, gather, clock)

    result = g.tick(active=True, refresh=False, roots=[])
    assert result == GatherError("incus is down")
    assert g.tick(active=True, refresh=False, roots=[]) is None  # not a hot loop
    clock.t = 3.0
    assert isinstance(g.tick(active=True, refresh=False, roots=[]), GatherError)


def _failures(caplog):
    return [r for r in caplog.records if r.getMessage() == "gather failed"]


def test_a_repeated_failure_is_logged_once_and_a_new_one_again(mocker, caplog):
    clock = _Clock()
    errors = [OSError("down"), OSError("down"), OSError("down"), OSError("other")]
    g = _gatherer(mocker, mocker.Mock(side_effect=errors), clock)
    with caplog.at_level("WARNING", logger="jailbee.state_service.gatherer"):
        for _ in errors:
            g.tick(active=True, refresh=False, roots=[])
            clock.t += 3.0
    messages = [r.exc_info[1].args[0] for r in _failures(caplog)]
    assert messages == ["down", "other"]


def test_a_failure_after_a_recovery_is_logged_again(mocker, caplog):
    clock = _Clock()
    outcomes = [OSError("down"), [], OSError("down")]
    g = _gatherer(mocker, mocker.Mock(side_effect=outcomes), clock)
    with caplog.at_level("WARNING", logger="jailbee.state_service.gatherer"):
        for _ in outcomes:
            g.tick(active=True, refresh=False, roots=[])
            clock.t += 3.0
    assert len(_failures(caplog)) == 2
    assert sum("gather recovered" in r.getMessage() for r in caplog.records) == 1


def test_cadence_carries_the_activity_setting():
    assert Cadence.from_config(DashboardRefresh(agent_activity=False)).activity is False
    assert Cadence.from_config(DashboardRefresh()).activity is True


def test_the_gatherer_reads_activity_only_when_the_setting_is_on(mocker):
    sample = mocker.patch("jailbee.state_service.gatherer.sample_activity")
    on = Gatherer(mocker.Mock(), CAD, gather=lambda *a, **k: [], sleep=lambda _s: None)
    off = Gatherer(
        mocker.Mock(),
        Cadence(3.0, 10.0, True, activity=False),
        gather=lambda *a, **k: [],
        sleep=lambda _s: None,
    )

    on.tick(active=True, refresh=False, roots=[])
    off.tick(active=True, refresh=False, roots=[])

    readers = [c.args[2] if len(c.args) > 2 else None for c in sample.call_args_list]
    assert readers[0] is not None  # `on`
    assert readers[-1] is None  # `off`


# --- the update notice -------------------------------------------------------

NOTICE = UpdateNotice(current="1.4.0", latest="1.5.0", command="uv tool upgrade jailbee")


def _with_update(mocker, update, clock):
    mocker.patch("jailbee.state_service.gatherer.sample_activity")
    return Gatherer(
        mocker.Mock(),
        CAD,
        gather=lambda *a, **k: [],
        clock=clock,
        sleep=lambda _s: None,
        update=update,
    )


def test_a_snapshot_carries_the_update_notice(mocker):
    g = _with_update(mocker, lambda: NOTICE, _Clock())

    snapshot = g.tick(active=True, refresh=False, roots=[])

    assert snapshot.update == NOTICE


def test_the_notice_is_read_once_a_minute_not_every_gather(mocker):
    clock = _Clock()
    update = mocker.Mock(return_value=NOTICE)
    g = _with_update(mocker, update, clock)

    for _ in range(4):  # 0 s, 3 s, 6 s, 9 s
        g.tick(active=True, refresh=False, roots=[])
        clock.t += 3.0
    assert update.call_count == 1

    clock.t = UPDATE_INTERVAL
    g.tick(active=True, refresh=False, roots=[])
    assert update.call_count == 2


def test_the_notice_between_reads_is_the_last_one_read(mocker):
    clock = _Clock()
    update = mocker.Mock(side_effect=[NOTICE, None])
    g = _with_update(mocker, update, clock)

    first = g.tick(active=True, refresh=False, roots=[])
    clock.t = 3.0
    second = g.tick(active=True, refresh=False, roots=[])

    assert first.update == second.update == NOTICE


def test_a_refresh_reads_the_notice_again(mocker):
    """The dashboard asks for one right after a dismissal; the row must be
    gone from the very next snapshot, not a minute later."""
    clock = _Clock()
    update = mocker.Mock(side_effect=[NOTICE, None])
    g = _with_update(mocker, update, clock)

    g.tick(active=True, refresh=False, roots=[])
    clock.t = 1.0
    after = g.tick(active=True, refresh=True, roots=[])

    assert after.update is None


def test_a_failing_notice_read_cannot_fail_the_gather(mocker):
    g = _with_update(mocker, mocker.Mock(side_effect=RuntimeError("locked")), _Clock())

    snapshot = g.tick(active=True, refresh=False, roots=[])

    assert isinstance(snapshot, Snapshot)
    assert snapshot.update is None


def test_snapshot_with_advisories_round_trips():
    from jailbee.dashboard.model import RepoAdvisory
    from jailbee.state_service.protocol import decode, encode

    advisory = RepoAdvisory("apply", "alpha", ("a", "b"), "1.7.3", "`jb apply` owed", "apply")
    notice = RepoAdvisory("legacy-chrome-block", "/r/c.yaml", ("x",), "", "x")
    group = RepoGroup("alpha", "/r", None, [], advisories=(advisory, notice))
    snap = Snapshot(1, datetime(2026, 1, 1, tzinfo=UTC), True, [group])

    assert decode(encode(snap)) == snap


def _with_system(mocker, reader, containers=None):
    mocker.patch("jailbee.state_service.gatherer.sample_activity")
    groups = [RepoGroup("alpha", "/a", None, containers or [_ci("x")])]
    gather = mocker.Mock(side_effect=lambda *a, **k: groups)
    return Gatherer(
        mocker.Mock(),
        CAD,
        gather=gather,
        clock=_Clock(),
        sleep=lambda _s: None,
        wall_clock=lambda: datetime(2026, 10, 4, tzinfo=UTC),
        system=reader,
    )


def test_snapshot_carries_system_status(mocker):
    from jailbee.dashboard.system import SystemStatus

    status = SystemStatus(memory=None, pools=(), root=None)
    reader = mocker.Mock()
    reader.read.return_value = status
    containers = [dataclasses.replace(_ci("x"), root_pool="fast"), _ci("y")]
    g = _with_system(mocker, reader, containers)

    snap = g.tick(active=True, refresh=False, roots=[])

    assert isinstance(snap, Snapshot)
    assert snap.system == status
    assert reader.read.call_args.args[1] == ("fast",)


def test_system_reader_failure_does_not_fail_gather(mocker):
    reader = mocker.Mock()
    reader.read.side_effect = OSError("boom")
    g = _with_system(mocker, reader)

    snap = g.tick(active=True, refresh=False, roots=[])

    assert isinstance(snap, Snapshot)
    assert snap.system is None


def test_fast_window_caps_both_tier_intervals():
    # Slow cadence: base 3 s and git 10 s are not due 1.5 s / 2.5 s after the last gathers.
    assert due(now=100.0, last_base=98.5, last_full=97.5) == (False, False)
    assert due(now=100.0, last_base=98.5, last_full=97.5, fast=True) == (True, False)
    assert due(now=100.0, last_base=98.5, last_full=96.5, fast=True) == (True, True)


def test_fast_window_never_slows_a_faster_cadence():
    quick = Cadence(interval=0.5, git_interval=0.5, git=True)
    assert due(cadence=quick, now=100.0, last_base=99.4, last_full=99.4, fast=True) == (True, True)


def _sent_groups():
    old = _ci("alpha-x")
    old.uuid = "uuid-x"
    fresh = _ci("alpha-y")
    fresh.uuid = "uuid-y-now"
    return [RepoGroup("alpha", "/a", None, [old, fresh])]


def test_sent_counts_are_set_per_current_incarnation_and_per_repo(mocker):
    counts = SentCounts(
        by_uuid={
            "uuid-x": 3,
            # An earlier incarnation of alpha-y (another uuid): not this container's.
            "uuid-y-before": 5,
        },
        by_prefix={"alpha": 8},
    )
    g = _gatherer(mocker, lambda *a, **k: _sent_groups(), _Clock(), lambda: counts)

    group = g.tick(active=True, refresh=False, roots=[]).groups[0]

    assert group.sent_count == 8
    assert [c.sent_count for c in group.containers] == [3, 0]


def test_a_failing_sent_counts_read_leaves_counts_empty_and_the_gather_alive(mocker):
    def boom():
        raise RuntimeError("database is locked")

    g = _gatherer(mocker, lambda *a, **k: _sent_groups(), _Clock(), boom)

    snapshot = g.tick(active=True, refresh=False, roots=[])

    assert isinstance(snapshot, Snapshot)
    assert snapshot.groups[0].sent_count == 0
    assert [c.sent_count for c in snapshot.groups[0].containers] == [0, 0]


def test_sent_counts_are_read_once_per_round(mocker):
    read = mocker.Mock(return_value=SentCounts())
    g = _gatherer(mocker, lambda *a, **k: _sent_groups(), _Clock(), read)

    g.tick(active=True, refresh=False, roots=[])

    assert read.call_count == 1


# --- recording which container ran which agent session ----------------------


def _agent(*session_ids, agent="claude"):
    from jailbee.agent_status import AgentSummary

    return (AgentSummary(agent, "busy", None, None, len(session_ids), session_ids=session_ids),)


def _ci_live(name, uuid, *session_ids):
    c = _ci(name)
    c.uuid = uuid
    c.agent_status = _agent(*session_ids)
    return c


def _recording_gatherer(mocker, containers, recorder):
    mocker.patch("jailbee.state_service.gatherer.sample_activity")
    return Gatherer(
        mocker.Mock(),
        CAD,
        gather=lambda *a, **k: [RepoGroup("alpha", "/a", None, list(containers))],
        clock=_Clock(),
        sleep=lambda _s: None,
        recorder=recorder,
    )


def test_each_gather_records_the_live_sessions_of_their_containers(mocker):
    from jailbee.state_service.session_log import SessionRecorder, recorded_sessions

    engine = get_engine()
    now = [datetime(2026, 11, 1, 10, tzinfo=UTC)]
    recorder = SessionRecorder(engine, clock=lambda: now[0])
    g = _recording_gatherer(
        mocker, [_ci_live("alpha-1", "u-1", "s-1"), _ci("alpha-idle")], recorder
    )

    g.tick(active=True, refresh=False, roots=[])

    rows = recorded_sessions(engine, "alpha-1", "u-1")
    assert [(r.agent, r.session_id, r.first_seen, r.last_seen) for r in rows] == [
        ("claude", "s-1", now[0], now[0])
    ]
    assert recorded_sessions(engine, "alpha-idle", None) == []


def test_a_later_gather_moves_last_seen_and_keeps_first_seen(mocker):
    from jailbee.state_service.session_log import SessionRecorder, recorded_sessions

    engine = get_engine()
    now = [datetime(2026, 11, 1, 10, tzinfo=UTC)]
    recorder = SessionRecorder(engine, clock=lambda: now[0], write_interval=60.0)
    first = now[0]
    c = _ci_live("alpha-1", "u-1", "s-1")

    recorder.record([RepoGroup("alpha", "/a", None, [c])])
    now[0] = first + timedelta(seconds=10)
    recorder.record([RepoGroup("alpha", "/a", None, [c])])  # too soon: no write
    assert recorded_sessions(engine, "alpha-1", "u-1")[0].last_seen == first
    now[0] = first + timedelta(minutes=5)
    recorder.record([RepoGroup("alpha", "/a", None, [c])])

    (row,) = recorded_sessions(engine, "alpha-1", "u-1")
    assert (row.first_seen, row.last_seen) == (first, now[0])


def test_a_session_that_comes_back_is_written_again_at_once(mocker):
    # The throttle forgets sessions a gather no longer reports, so its memory
    # stays bounded by the live sessions, not by every session ever seen.
    from jailbee.state_service.session_log import SessionRecorder, recorded_sessions

    engine = get_engine()
    now = [datetime(2026, 11, 1, 10, tzinfo=UTC)]
    recorder = SessionRecorder(engine, clock=lambda: now[0], write_interval=60.0)
    first = now[0]
    live = RepoGroup("alpha", "/a", None, [_ci_live("alpha-1", "u-1", "s-1")])

    recorder.record([live])
    now[0] = first + timedelta(seconds=10)
    recorder.record([RepoGroup("alpha", "/a", None, [_ci("alpha-1")])])
    now[0] = first + timedelta(seconds=20)
    recorder.record([live])

    (row,) = recorded_sessions(engine, "alpha-1", "u-1")
    assert row.last_seen == now[0]


def test_a_recreated_container_has_its_own_recorded_sessions(mocker):
    from jailbee.state_service.session_log import SessionRecorder, recorded_sessions

    engine = get_engine()
    recorder = SessionRecorder(engine)
    recorder.record([RepoGroup("alpha", "/a", None, [_ci_live("alpha-1", "u-old", "s-old")])])
    recorder.record([RepoGroup("alpha", "/a", None, [_ci_live("alpha-1", "u-new", "s-new")])])

    assert [r.session_id for r in recorded_sessions(engine, "alpha-1", "u-new")] == ["s-new"]
    assert [r.session_id for r in recorded_sessions(engine, "alpha-1", "u-old")] == ["s-old"]


def test_a_failing_recorder_cannot_fail_the_gather(mocker):
    def boom(groups):
        raise RuntimeError("database is locked")

    g = _recording_gatherer(mocker, [_ci("x")], boom)

    assert isinstance(g.tick(active=True, refresh=False, roots=[]), Snapshot)


def test_a_base_tick_updates_the_rows_outbox_counts_and_not_the_carried_probe_copy(mocker):
    clock = _Clock()
    probed = GitStatus("+1 -0", "clean", "0", "ok", pending_pr_actions=0, pending_issue_actions=0)

    def row(git, pr, issue):
        c = _ci("x", git)
        c.outbox_pr, c.outbox_issue = pr, issue
        return c

    rounds = iter([[row(None, 0, 0)], [row(probed, 0, 0)], [row(None, 2, 1)]])
    g = _gatherer(mocker, lambda *a, **k: [RepoGroup("alpha", "/a", None, next(rounds))], clock)

    g.tick(active=True, refresh=False, roots=[])  # base
    g.tick(active=True, refresh=False, roots=[])  # git
    clock.t = 3.0
    third = g.tick(active=True, refresh=False, roots=[])  # base, staged since the git tick

    c = third.groups[0].containers[0]
    assert (c.outbox_pr, c.outbox_issue) == (2, 1)
    assert c.git_status == probed  # carried forward, stale by design


def test_a_snapshot_carries_pr_head_fetch_facts_even_on_a_base_gather(mocker):
    from jailbee.host_target import SyncTarget
    from jailbee.pr_autofetch import AutofetchPolicy, PrHeadAutofetcher

    clock = _Clock()
    status = GitStatus(
        "clean", "clean", "0", "ok", sync_target=SyncTarget("pr", "PR #7", "a" * 40, "r")
    )
    rounds = iter([[_ci("x")], [_ci("x", status)], [_ci("x")]])

    def groups(*a, **k):
        batch = next(rounds)
        for c in batch:
            c.pr_number = 7
        return [RepoGroup("alpha", "/a", None, batch)]

    fetches: list[int] = []
    pr_heads = PrHeadAutofetcher(
        policy=lambda root: AutofetchPolicy(root, "origin", True, 300.0),
        fetch=lambda root, n, remote: fetches.append(n),
        wall=lambda: 5.0,
        submit=lambda job: job(),
    )
    mocker.patch("jailbee.state_service.gatherer.sample_activity")
    g = Gatherer(
        mocker.Mock(),
        CAD,
        sent_counts=lambda: SentCounts(),
        gather=groups,
        clock=clock,
        sleep=lambda _s: None,
        pr_heads=pr_heads,
    )
    annotate = mocker.spy(pr_heads, "annotate")

    g.tick(active=True, refresh=False, roots=[])
    g.tick(active=True, refresh=False, roots=[])
    clock.t = 3.0
    third = g.tick(active=True, refresh=False, roots=[])
    assert annotate.call_count == 3  # on the base tick too, not carried over

    target = third.groups[0].containers[0].git_status.sync_target
    assert target.fetched_at == 5.0
    assert fetches == [7]


def test_an_exception_inside_pr_head_annotate_leaves_the_gather_intact(mocker):
    pr_heads = mocker.Mock()
    pr_heads.annotate.side_effect = RuntimeError("boom")
    mocker.patch("jailbee.state_service.gatherer.sample_activity")
    g = Gatherer(
        mocker.Mock(),
        CAD,
        sent_counts=lambda: SentCounts(),
        gather=lambda *a, **k: [RepoGroup("alpha", "/a", None, [_ci("x")])],
        clock=_Clock(),
        sleep=lambda _s: None,
        pr_heads=pr_heads,
    )
    result = g.tick(active=True, refresh=False, roots=[])
    assert pr_heads.annotate.called
    assert not isinstance(result, GatherError)
    assert result.groups[0].containers[0].name == "x"
