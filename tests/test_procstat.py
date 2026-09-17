"""Unit tests for the host-side /proc reader behind the CPU/DOING columns.

Every test drives the module against a fake filesystem root under
`tmp_path` — nothing here reads the real /proc.
"""

from __future__ import annotations

from pathlib import Path

from jailbee import procstat


def write_stat(
    proc_root: Path,
    pid: int,
    *,
    comm: str = "claude",
    utime: int = 0,
    stime: int = 0,
    starttime: int = 1000,
) -> None:
    """Write a /proc/<pid>/stat line with the fields the reader parses.

    Field numbering follows proc(5): 1 pid, 2 comm, 3 state, 14 utime,
    15 stime, 22 starttime. The filler keeps every parsed field at its real
    offset, which is the whole point — an off-by-one here would make the
    tests agree with a broken parser.
    """
    d = proc_root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    before = ["S", "1", "1", "0", "-1", "4194304", "0", "0", "0", "0", "0"]  # fields 3-13
    middle = ["0", "0", "20", "0", "1", "0"]  # fields 16-21
    parts = [*before, str(utime), str(stime), *middle, str(starttime)]
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(parts) + "\n")


def test_read_process_returns_summed_cpu_ticks(tmp_path):
    write_stat(tmp_path, 42, comm="pytest", utime=700, stime=300, starttime=99)

    sample = procstat.read_process(42, proc_root=tmp_path)

    assert sample is not None
    assert sample.comm == "pytest"
    assert sample.ticks == 1000  # utime + stime, not either alone
    assert sample.starttime == 99


def test_read_process_survives_a_comm_containing_parens_and_spaces(tmp_path):
    """`comm` is the one free-form field in stat. Splitting on whitespace,
    or on the FIRST ')', shifts every field after it — so a process named
    ") evil (" would silently poison utime, stime and starttime."""
    write_stat(tmp_path, 7, comm="wat) ( wat", utime=5, stime=5, starttime=3)

    sample = procstat.read_process(7, proc_root=tmp_path)

    assert sample is not None
    assert sample.comm == "wat) ( wat"
    assert sample.ticks == 10
    assert sample.starttime == 3


def test_read_process_returns_none_for_a_pid_that_is_gone(tmp_path):
    assert procstat.read_process(1234, proc_root=tmp_path) is None


def test_read_process_returns_none_for_a_truncated_stat_line(tmp_path):
    d = tmp_path / "8"
    d.mkdir()
    (d / "stat").write_text("8 (claude) S 1 1\n")

    assert procstat.read_process(8, proc_root=tmp_path) is None
