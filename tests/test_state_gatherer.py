from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from jailbee.dashboard import RepoGroup
from jailbee.git_status import GitStatus
from jailbee.global_config import DashboardRefresh
from jailbee.lifecycle import ContainerInfo
from jailbee.state_service.gatherer import Cadence, Gatherer, refresh_due
from jailbee.state_service.protocol import GatherError, Snapshot

CAD = Cadence(interval=3.0, git_interval=10.0, git=True)


def due(**kw):
    args = {
        "now": 100.0,
        "last_base": 99.0,
        "last_full": 95.0,
        "cadence": CAD,
        "active": True,
        "refresh": False,
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


def _gatherer(mocker, gather, clock):
    mocker.patch("jailbee.state_service.gatherer.sample_activity")
    return Gatherer(
        mocker.Mock(),
        CAD,
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
    assert gather.call_args_list[0].kwargs == {"with_git": False}
    assert gather.call_args_list[1].kwargs == {"with_git": True}
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
