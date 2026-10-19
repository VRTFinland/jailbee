"""Tests for the host-wide memory and filesystem readers in `jailbee.sysstat`.

Everything runs against fake /proc files under ``tmp_path`` or a monkeypatched
``os.statvfs``, never the host's real /proc.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jailbee.sysstat import (
    DiskStatus,
    MemoryStatus,
    read_disk,
    read_memory,
    same_filesystem,
)

MEMINFO = """\
MemTotal:       16000000 kB
MemFree:         1000000 kB
MemAvailable:    4000000 kB
Buffers:          100000 kB
Cached:          2000000 kB
SReclaimable:     300000 kB
Shmem:            400000 kB
SwapTotal:       2000000 kB
SwapFree:        1500000 kB
"""


def _write_meminfo(proc_root: Path, text: str) -> Path:
    proc_root.mkdir(parents=True, exist_ok=True)
    (proc_root / "meminfo").write_text(text)
    return proc_root


def test_read_memory_parses_meminfo(tmp_path: Path) -> None:
    proc_root = _write_meminfo(tmp_path / "proc", MEMINFO)

    status = read_memory(proc_root=proc_root)

    assert status == MemoryStatus(
        16000000 * 1024, 4000000 * 1024, 2000000 * 1024, 2000000 * 1024, 1500000 * 1024
    )
    assert status is not None
    assert status.free_ratio == 0.25
    assert status.used == (16000000 - 4000000) * 1024


def test_read_memory_without_swap_lines_has_no_swap(tmp_path: Path) -> None:
    proc_root = _write_meminfo(tmp_path / "proc", "MemTotal: 1000 kB\nMemAvailable: 100 kB\n")

    status = read_memory(proc_root=proc_root)

    assert status is not None
    assert (status.swap_total, status.swap_free) == (0, 0)


def test_swap_used_and_free_ratio() -> None:
    status = MemoryStatus(100, 50, 0, swap_total=100, swap_free=25)
    assert status.swap_used == 75
    assert status.swap_free_ratio == 0.25


def test_read_memory_cache_is_capped_at_available(tmp_path: Path) -> None:
    text = "MemTotal: 1000 kB\nMemAvailable: 100 kB\nCached: 5000 kB\n"
    proc_root = _write_meminfo(tmp_path / "proc", text)

    status = read_memory(proc_root=proc_root)

    assert status is not None
    assert status.cache == 100 * 1024


def test_read_memory_cache_never_negative(tmp_path: Path) -> None:
    text = "MemTotal: 1000 kB\nMemAvailable: 500 kB\nShmem: 900 kB\n"
    proc_root = _write_meminfo(tmp_path / "proc", text)

    status = read_memory(proc_root=proc_root)

    assert status is not None
    assert status.cache == 0


def test_read_memory_missing_file_is_none(tmp_path: Path) -> None:
    assert read_memory(proc_root=tmp_path / "absent") is None


@pytest.mark.parametrize(
    "text",
    [
        "MemAvailable: 4000000 kB\n",  # MemTotal missing
        "MemTotal: 16000000 kB\n",  # MemAvailable missing
        "",
    ],
)
def test_read_memory_missing_field_is_none(tmp_path: Path, text: str) -> None:
    proc_root = _write_meminfo(tmp_path / "proc", text)

    assert read_memory(proc_root=proc_root) is None


def test_memory_free_ratio_zero_total() -> None:
    assert MemoryStatus(0, 0, 0).free_ratio == 0.0


def test_disk_free_ratio_and_used() -> None:
    status = DiskStatus(4096000, 409600)

    assert status.used == 4096000 - 409600
    assert status.free_ratio == pytest.approx(0.1)


def test_disk_free_ratio_zero_total() -> None:
    assert DiskStatus(0, 0).free_ratio == 0.0


class _FakeStatvfs:
    def __init__(self, blocks: int, bavail: int, frsize: int) -> None:
        self.f_blocks = blocks
        self.f_bavail = bavail
        self.f_frsize = frsize


def test_read_disk_uses_bavail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_statvfs(path: object) -> _FakeStatvfs:
        return _FakeStatvfs(blocks=1000, bavail=100, frsize=4096)

    monkeypatch.setattr(os, "statvfs", fake_statvfs)

    assert read_disk(tmp_path) == DiskStatus(4096000, 409600)


def test_read_disk_oserror_is_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_statvfs(path: object) -> _FakeStatvfs:
        raise OSError("gone")

    monkeypatch.setattr(os, "statvfs", fake_statvfs)

    assert read_disk(tmp_path) is None


def test_read_disk_real_path_is_populated(tmp_path: Path) -> None:
    status = read_disk(tmp_path)

    assert status is not None
    assert status.total > 0
    assert 0 <= status.available <= status.total


def test_same_filesystem(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    assert same_filesystem(a, b) is True


def test_same_filesystem_missing_path_is_false(tmp_path: Path) -> None:
    assert same_filesystem(tmp_path, tmp_path / "nonexistent") is False
