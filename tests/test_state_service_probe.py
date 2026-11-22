from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from jailbee.dashboard.model import RepoGroup
from jailbee.lifecycle import ContainerInfo, ServiceStatus
from jailbee.state_service.gatherer import Cadence, Gatherer
from jailbee.state_service.protocol import Snapshot
from jailbee.state_service.service_probe import ServiceProbe


def _ci(name: str, state: str = "Running") -> ContainerInfo:
    return ContainerInfo(name, state, "strict", None, None, repo="alpha")


class _Clock:
    t = 0.0

    def __call__(self) -> float:
        return self.t


def _groups(*containers: ContainerInfo) -> list[RepoGroup]:
    return [RepoGroup("alpha", "/a", None, list(containers))]


def test_running_containers_get_their_services() -> None:
    calls: list[tuple[Path, str]] = []

    def fetch(root: Path, name: str):
        calls.append((root, name))
        return [{"name": "web", "state": "ready"}, {"name": "db", "state": "failed"}]

    groups = _groups(_ci("alpha-x"))
    ServiceProbe(fetch).annotate(groups)

    c = groups[0].containers[0]
    assert c.services == (ServiceStatus("web", "ready"), ServiceStatus("db", "failed"))
    assert c.services_unknown is False
    assert calls == [(Path("/a"), "alpha-x")]


def test_a_stopped_container_is_not_probed() -> None:
    calls: list[str] = []
    groups = _groups(_ci("alpha-x", "Stopped"))
    ServiceProbe(lambda r, n: calls.append(n) or []).annotate(groups)

    assert calls == []
    assert groups[0].containers[0].services is None
    assert groups[0].containers[0].services_unknown is False


def test_a_failed_probe_is_unknown_not_an_error() -> None:
    def fetch(root: Path, name: str):
        raise RuntimeError("exec failed")

    groups = _groups(_ci("alpha-x"))
    ServiceProbe(fetch).annotate(groups)

    c = groups[0].containers[0]
    assert c.services is None and c.services_unknown is True


def test_the_listing_is_reused_until_the_interval_has_passed() -> None:
    clock = _Clock()
    fetch_calls: list[str] = []

    def fetch(root: Path, name: str):
        fetch_calls.append(name)
        return [{"name": "web", "state": "ready"}]

    probe = ServiceProbe(fetch, interval=30.0, clock=clock)
    probe.annotate(_groups(_ci("alpha-x")))
    clock.t = 10.0
    again = _groups(_ci("alpha-x"))
    probe.annotate(again)
    assert fetch_calls == ["alpha-x"]
    assert again[0].containers[0].services == (ServiceStatus("web", "ready"),)  # from cache
    clock.t = 31.0
    probe.annotate(_groups(_ci("alpha-x")))
    assert fetch_calls == ["alpha-x", "alpha-x"]


def test_the_gatherer_survives_a_probe_that_raises(mocker) -> None:
    mocker.patch("jailbee.state_service.gatherer.sample_activity")
    probe = mocker.Mock()
    probe.annotate.side_effect = RuntimeError("boom")
    g = Gatherer(
        mocker.Mock(),
        Cadence(1.0, 3.0, True),
        gather=lambda *a, **k: _groups(_ci("alpha-x")),
        clock=_Clock(),
        sleep=lambda _s: None,
        wall_clock=lambda: datetime(2026, 11, 22, tzinfo=UTC),
        sent_counts=lambda: mocker.Mock(by_prefix={}, by_uuid={}),
        services=probe,
    )

    assert isinstance(g.tick(active=True, refresh=False, roots=[]), Snapshot)


def test_the_gatherer_carries_services_in_the_snapshot(mocker) -> None:
    mocker.patch("jailbee.state_service.gatherer.sample_activity")
    probe = ServiceProbe(lambda r, n: [{"name": "web", "state": "ready"}])
    g = Gatherer(
        mocker.Mock(),
        Cadence(1.0, 3.0, True),
        gather=lambda *a, **k: _groups(_ci("alpha-x")),
        clock=_Clock(),
        sleep=lambda _s: None,
        wall_clock=lambda: datetime(2026, 11, 22, tzinfo=UTC),
        sent_counts=lambda: mocker.Mock(by_prefix={}, by_uuid={}),
        services=probe,
    )

    snap = g.tick(active=True, refresh=False, roots=[])

    assert snap.groups[0].containers[0].services == (ServiceStatus("web", "ready"),)
