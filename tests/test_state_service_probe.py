from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from jailbee.dashboard.model import RepoGroup
from jailbee.lifecycle import ContainerInfo, ServiceState
from jailbee.state_service.gatherer import Cadence, Gatherer
from jailbee.state_service.protocol import Snapshot
from jailbee.state_service.service_probe import ServiceProbe


def _ci(name: str, state: str = "Running") -> ContainerInfo:
    return ContainerInfo(name, state, "strict", None, None, repo="alpha")


class _Clock:
    t = 0.0

    def __call__(self) -> float:
        return self.t


def _sync(job) -> None:
    job()


def _groups(*containers: ContainerInfo) -> list[RepoGroup]:
    return [RepoGroup("alpha", "/a", None, list(containers))]


def test_running_containers_get_their_services() -> None:
    calls: list[tuple[Path, str]] = []

    def fetch(root: Path, name: str):
        calls.append((root, name))
        return [{"name": "web", "state": "ready"}, {"name": "db", "state": "failed"}]

    groups = _groups(_ci("alpha-x"))
    ServiceProbe(fetch, submit=_sync).annotate(groups)

    c = groups[0].containers[0]
    assert c.services == (ServiceState("web", "ready"), ServiceState("db", "failed"))
    assert c.services_unknown is False
    assert calls == [(Path("/a"), "alpha-x")]


def test_a_stopped_container_is_not_probed() -> None:
    calls: list[str] = []
    groups = _groups(_ci("alpha-x", "Stopped"))
    ServiceProbe(lambda r, n: calls.append(n) or [], submit=_sync).annotate(groups)

    assert calls == []
    assert groups[0].containers[0].services is None
    assert groups[0].containers[0].services_unknown is False


def test_a_failed_probe_is_unknown_not_an_error() -> None:
    def fetch(root: Path, name: str):
        raise RuntimeError("exec failed")

    groups = _groups(_ci("alpha-x"))
    ServiceProbe(fetch, submit=_sync).annotate(groups)

    c = groups[0].containers[0]
    assert c.services is None and c.services_unknown is True


def test_the_listing_is_reused_until_the_interval_has_passed() -> None:
    clock = _Clock()
    fetch_calls: list[str] = []

    def fetch(root: Path, name: str):
        fetch_calls.append(name)
        return [{"name": "web", "state": "ready"}]

    probe = ServiceProbe(fetch, interval=30.0, clock=clock, submit=_sync)
    probe.annotate(_groups(_ci("alpha-x")))
    clock.t = 10.0
    again = _groups(_ci("alpha-x"))
    probe.annotate(again)
    assert fetch_calls == ["alpha-x"]
    assert again[0].containers[0].services == (ServiceState("web", "ready"),)  # from cache
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
    probe.annotate.assert_called_once()


def test_the_gatherer_carries_services_in_the_snapshot(mocker) -> None:
    mocker.patch("jailbee.state_service.gatherer.sample_activity")
    probe = ServiceProbe(lambda r, n: [{"name": "web", "state": "ready"}], submit=_sync)
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

    assert snap.groups[0].containers[0].services == (ServiceState("web", "ready"),)


def test_a_malformed_row_reads_unknown() -> None:
    groups = _groups(_ci("alpha-x"))
    ServiceProbe(lambda r, n: [{"name": "web"}], submit=_sync).annotate(groups)

    assert groups[0].containers[0].services_unknown is True


class _Deferred:
    """A submit seam that holds the jobs until the test runs them."""

    def __init__(self) -> None:
        self.jobs: list = []

    def __call__(self, job) -> None:
        self.jobs.append(job)

    def run_all(self) -> None:
        jobs, self.jobs = self.jobs, []
        for job in jobs:
            job()


def _ok(root: Path, name: str):
    return [{"name": "web", "state": "ready"}]


def test_annotate_never_waits_and_the_result_lands_on_a_later_tick() -> None:
    held = _Deferred()
    probe = ServiceProbe(_ok, submit=held)
    first = _groups(_ci("alpha-x"))
    probe.annotate(first)

    c = first[0].containers[0]
    assert c.services is None and c.services_unknown is False  # in flight: no line yet
    held.run_all()
    second = _groups(_ci("alpha-x"))
    probe.annotate(second)
    assert second[0].containers[0].services == (ServiceState("web", "ready"),)


def test_a_container_with_a_probe_in_flight_is_not_resubmitted() -> None:
    held = _Deferred()
    clock = _Clock()
    probe = ServiceProbe(_ok, interval=30.0, timeout=10.0, clock=clock, submit=held)
    for t in (0.0, 31.0, 100.0):
        clock.t = t
        probe.annotate(_groups(_ci("alpha-x")))

    assert len(held.jobs) == 1


def test_an_in_flight_probe_past_the_timeout_reads_unknown() -> None:
    held = _Deferred()
    clock = _Clock()
    probe = ServiceProbe(_ok, timeout=10.0, clock=clock, submit=held)
    probe.annotate(_groups(_ci("alpha-x")))
    clock.t = 11.0
    groups = _groups(_ci("alpha-x"))
    probe.annotate(groups)

    assert groups[0].containers[0].services_unknown is True


def test_a_hanging_probe_neither_blocks_annotate_nor_other_containers() -> None:
    import threading
    import time

    release = threading.Event()
    seen: list[str] = []

    def fetch(root: Path, name: str):
        seen.append(name)
        if name == "alpha-hang":
            release.wait(5)
        return [{"name": "web", "state": "ready"}]

    probe = ServiceProbe(fetch, timeout=0.05)
    try:
        for _ in range(3):
            started = time.monotonic()
            groups = _groups(_ci("alpha-hang"), _ci("alpha-ok"))
            probe.annotate(groups)
            assert time.monotonic() - started < 0.5
            time.sleep(0.1)
        assert seen.count("alpha-hang") == 1
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            groups = _groups(_ci("alpha-hang"), _ci("alpha-ok"))
            probe.annotate(groups)
            if groups[0].containers[1].services is not None:
                break
            time.sleep(0.02)
        assert groups[0].containers[1].services == (ServiceState("web", "ready"),)
        assert groups[0].containers[0].services_unknown is True
    finally:
        release.set()
        probe.shutdown()


def test_a_container_without_jailbee_shows_no_line_and_backs_off() -> None:
    from jailbee.container_app import JailbeeAbsentError

    clock = _Clock()
    calls: list[str] = []

    def fetch(root: Path, name: str):
        calls.append(name)
        raise JailbeeAbsentError("not mounted")

    probe = ServiceProbe(fetch, interval=30.0, clock=clock, submit=_sync)
    groups = _groups(_ci("alpha-x"))
    probe.annotate(groups)
    c = groups[0].containers[0]
    assert c.services is None and c.services_unknown is False
    clock.t = 31.0
    probe.annotate(_groups(_ci("alpha-x")))
    assert calls == ["alpha-x"]  # backed off past the normal interval
    clock.t = 10_000.0
    probe.annotate(_groups(_ci("alpha-x")))
    assert calls == ["alpha-x", "alpha-x"]


def test_a_vanished_container_is_forgotten_and_probed_afresh() -> None:
    calls: list[str] = []
    probe = ServiceProbe(lambda r, n: calls.append(n) or [], submit=_sync)
    probe.annotate(_groups(_ci("alpha-x")))
    probe.annotate(_groups())
    probe.annotate(_groups(_ci("alpha-x")))

    assert calls == ["alpha-x", "alpha-x"]


def test_a_group_without_a_repo_root_is_skipped() -> None:
    calls: list[str] = []
    groups = [RepoGroup("alpha", None, None, [_ci("alpha-x")])]
    ServiceProbe(lambda r, n: calls.append(n) or [], submit=_sync).annotate(groups)

    assert calls == []
    assert groups[0].containers[0].services is None


def test_after_shutdown_nothing_is_submitted_and_workers_are_daemons() -> None:
    import threading

    held = _Deferred()
    probe = ServiceProbe(_ok, submit=held)
    probe.shutdown()
    probe.annotate(_groups(_ci("alpha-x")))
    assert held.jobs == []

    seen: list[bool] = []
    done = threading.Event()

    def fetch(root: Path, name: str):
        seen.append(threading.current_thread().daemon)
        done.set()
        return []

    live = ServiceProbe(fetch)
    live.annotate(_groups(_ci("alpha-x")))
    assert done.wait(2) and seen == [True]


def test_the_live_fetch_reads_in_probe_mode(mocker) -> None:
    from jailbee.state_service.service_probe import live_fetch

    rows = mocker.patch("jailbee.services.host.service_rows", return_value=[])
    mocker.patch("jailbee.config.loader.load_repo_config", return_value="cfg")
    live_fetch(mocker.Mock())(Path("/a"), "alpha-x")

    assert rows.call_args.kwargs == {"probe": True}
