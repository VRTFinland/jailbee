"""Tests for the dashboard's host memory and storage pool reader."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jailbee.dashboard import system
from jailbee.dashboard.system import SystemReader, level, pool_names
from jailbee.incus import IncusError

MEMINFO = "MemTotal: 1000 kB\nMemAvailable: 400 kB\nCached: 100 kB\n"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def proc(tmp_path: Path) -> Path:
    root = tmp_path / "proc"
    root.mkdir()
    (root / "meminfo").write_text(MEMINFO)
    return root


def _incus(source: str = "/dev/nvme1n1", pools: list[str] | None = None) -> MagicMock:
    incus = MagicMock()
    names = pools or ["fast"]
    incus.list_storage_pools.return_value = [
        {"name": n, "config": {"source": source}} for n in names
    ]
    incus.storage_pool_resources.return_value = {"space": {"used": 30, "total": 100}}
    incus.profile_show.return_value = "devices:\n  root:\n    path: /\n    pool: dflt\n    type: disk\n"
    return incus


def _reader(proc: Path, clock: Clock, root: Path, interval: float = 10) -> SystemReader:
    return SystemReader(interval=interval, clock=clock, proc_root=proc, root=root)


def test_level_thresholds() -> None:
    assert level(0.5) == "ok"
    assert level(0.15) == "ok"
    assert level(0.149) == "warn"
    assert level(0.05) == "warn"
    assert level(0.049) == "crit"


def test_pool_names_sorted_distinct() -> None:
    def c(p: str | None) -> SimpleNamespace:
        return SimpleNamespace(root_pool=p)

    groups = [
        SimpleNamespace(containers=[c("b"), c(None), c("a")]),
        SimpleNamespace(containers=[c("b")]),
    ]
    assert pool_names(groups) == ("a", "b")  # type: ignore[arg-type]  # duck-typed groups


def test_reader_reads_pools_once_per_interval(proc: Path, tmp_path: Path) -> None:
    clock, incus = Clock(), _incus()
    reader = _reader(proc, clock, tmp_path)
    reader.read(incus, ["fast"])
    clock.now = 1
    status = reader.read(incus, ["fast"])
    assert incus.storage_pool_resources.call_count == 1
    assert status.memory is not None and status.memory.total == 1000 * 1024
    clock.now = 11
    reader.read(incus, ["fast"])
    assert incus.storage_pool_resources.call_count == 2


def test_reader_rereads_when_pool_set_changes(proc: Path, tmp_path: Path) -> None:
    clock, incus = Clock(), _incus(pools=["fast", "slow"])
    reader = _reader(proc, clock, tmp_path)
    reader.read(incus, ["fast"])
    clock.now = 1
    status = reader.read(incus, ["fast", "slow"])
    assert [p.name for p in status.pools] == ["fast", "slow"]
    assert incus.list_storage_pools.call_count == 2


def test_reader_hides_root_for_dir_pool_on_same_fs(proc: Path, tmp_path: Path) -> None:
    incus = _incus(source=str(tmp_path))
    status = _reader(proc, Clock(), tmp_path).read(incus, ["fast"])
    assert status.root is None
    assert len(status.pools) == 1


def test_reader_shows_root_for_block_device_pool(
    proc: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(system, "same_filesystem", lambda a, b: False)
    status = _reader(proc, Clock(), tmp_path).read(_incus(), ["fast"])
    assert status.root is not None


def test_reader_shows_root_for_zfs_dataset_source(proc: Path, tmp_path: Path) -> None:
    status = _reader(proc, Clock(), tmp_path).read(_incus(source="tank/incus"), ["fast"])
    assert status.root is not None


def test_reader_keeps_previous_pools_when_incus_fails(
    proc: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock, incus = Clock(), _incus()
    reader = _reader(proc, clock, tmp_path)
    first = reader.read(incus, ["fast"])
    clock.now = 11
    incus.storage_pool_resources.side_effect = IncusError("boom")
    with caplog.at_level(logging.DEBUG):
        second = reader.read(incus, ["fast"])
    assert second.pools == first.pools and first.pools
    assert second.memory is not None
    assert second.root is not None


def test_reader_falls_back_to_default_profile_pool_once(proc: Path, tmp_path: Path) -> None:
    clock, incus = Clock(), _incus(pools=["dflt"])
    reader = _reader(proc, clock, tmp_path)
    first = reader.read(incus, [])
    clock.now = 11
    reader.read(incus, [])
    assert incus.profile_show.call_count == 1
    assert [p.name for p in first.pools] == ["dflt"]
