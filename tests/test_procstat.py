"""Unit tests for the host-side /proc reader behind the CPU/DOING columns.

Every test drives the module against a fake filesystem root under
`tmp_path` — nothing here reads the real /proc.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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


def write_cgroup(proc_root: Path, pid: int, path: str) -> None:
    """Write a cgroup v2 line for `pid` (``0::<path>``)."""
    d = proc_root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cgroup").write_text(f"0::{path}\n")


def write_cgroup_procs(cgroup_root: Path, path: str, pids: list[int]) -> None:
    d = cgroup_root / path.lstrip("/")
    d.mkdir(parents=True, exist_ok=True)
    (d / "cgroup.procs").write_text("".join(f"{p}\n" for p in pids))


def test_read_container_pids_walks_the_whole_subtree(tmp_path):
    """A systemd container keeps almost nothing in its root cgroup: pid 1 is
    in init.scope and the real work is under system.slice/user.slice.
    Reading only the root's cgroup.procs reports an empty container, which
    looks exactly like the feature not working."""
    proc, cg = tmp_path / "proc", tmp_path / "cgroup"
    write_cgroup(proc, 500, "/lxc.payload.gie-demo/init.scope")
    write_cgroup_procs(cg, "/lxc.payload.gie-demo", [])
    write_cgroup_procs(cg, "/lxc.payload.gie-demo/init.scope", [500])
    write_cgroup_procs(cg, "/lxc.payload.gie-demo/system.slice/ssh.service", [610])
    write_cgroup_procs(cg, "/lxc.payload.gie-demo/user.slice/session.scope", [700, 701])

    pids = procstat.read_container_pids(500, "gie-demo", proc_root=proc, cgroup_root=cg)

    assert sorted(pids) == [500, 610, 700, 701]


def test_read_container_pids_climbs_out_of_init_scope(tmp_path):
    """init's own cgroup is a CHILD of the container's. Using it as the base
    would return pid 1 and nothing else."""
    proc, cg = tmp_path / "proc", tmp_path / "cgroup"
    write_cgroup(proc, 500, "/lxc.payload.gie-demo/init.scope")
    write_cgroup_procs(cg, "/lxc.payload.gie-demo/init.scope", [500])
    write_cgroup_procs(cg, "/lxc.payload.gie-demo/system.slice/work.service", [900])

    pids = procstat.read_container_pids(500, "gie-demo", proc_root=proc, cgroup_root=cg)

    assert 900 in pids


def test_read_container_pids_falls_back_to_the_first_component(tmp_path):
    """The container's name is the cut marker, but a cgroup layout that does
    not carry it must still yield a base rather than nothing."""
    proc, cg = tmp_path / "proc", tmp_path / "cgroup"
    write_cgroup(proc, 500, "/some.scope/deeper")
    write_cgroup_procs(cg, "/some.scope/deeper", [501])

    assert procstat.read_container_pids(500, "gie-demo", proc_root=proc, cgroup_root=cg) == [501]


def test_read_container_pids_is_empty_when_the_cgroup_is_unreadable(tmp_path):
    proc, cg = tmp_path / "proc", tmp_path / "cgroup"
    write_cgroup(proc, 500, "/lxc.payload.gie-demo")

    assert procstat.read_container_pids(500, "gie-demo", proc_root=proc, cgroup_root=cg) == []


def test_read_container_pids_is_empty_without_a_cgroup_file(tmp_path):
    assert (
        procstat.read_container_pids(
            500, "gie-demo", proc_root=tmp_path / "proc", cgroup_root=tmp_path / "cgroup"
        )
        == []
    )


def make_sampler(tmp_path, ticks):
    """A sampler on a fake filesystem, with a scripted monotonic clock."""
    clock = iter(ticks)
    return procstat.ActivitySampler(
        proc_root=tmp_path / "proc",
        cgroup_root=tmp_path / "cgroup",
        clock=lambda: next(clock),
    )


def stage_container(tmp_path, *, name="gie-demo", init_pid=500, pids=()):
    """Put a container's init cgroup and one busy cgroup in place."""
    proc, cg = tmp_path / "proc", tmp_path / "cgroup"
    write_cgroup(proc, init_pid, f"/lxc.payload.{name}/init.scope")
    write_cgroup_procs(cg, f"/lxc.payload.{name}/system.slice/work.service", list(pids))


def test_first_sample_primes_and_reports_nothing(tmp_path):
    """A delta needs two readings. The first call must not invent one."""
    stage_container(tmp_path, pids=[600])
    write_stat(tmp_path / "proc", 600, comm="claude", utime=100, stime=0)
    sampler = make_sampler(tmp_path, [10.0])

    out = sampler.sample([procstat.SampleInput("gie-demo", 500, 1_000_000_000)])

    assert out["gie-demo"].cpu_percent is None
    assert out["gie-demo"].processes == ()


def test_container_percent_comes_from_the_usage_delta(tmp_path):
    """1.82 s of CPU time burned over 1 s of wall time is 182% of one core."""
    stage_container(tmp_path, pids=[])
    sampler = make_sampler(tmp_path, [10.0, 11.0])
    item_before = procstat.SampleInput("gie-demo", 500, 5_000_000_000)
    item_after = procstat.SampleInput("gie-demo", 500, 6_820_000_000)

    sampler.sample([item_before])
    out = sampler.sample([item_after])

    assert out["gie-demo"].cpu_percent == pytest.approx(182.0)


def test_processes_are_aggregated_by_name_with_a_count(tmp_path):
    """Eight pytest workers are one answer to "what is it doing", not eight."""
    stage_container(tmp_path, pids=[600, 601, 602])
    proc = tmp_path / "proc"
    for pid in (600, 601, 602):
        write_stat(proc, pid, comm="pytest", utime=0, stime=0, starttime=5)
    sampler = make_sampler(tmp_path, [10.0, 11.0])
    item = procstat.SampleInput("gie-demo", 500, None)
    sampler.sample([item])
    for pid in (600, 601, 602):
        # 50 ticks over 1 s is 50% of one core each, at the usual 100 Hz.
        write_stat(proc, pid, comm="pytest", utime=procstat.CLOCK_TICKS // 2, stime=0, starttime=5)

    out = sampler.sample([item])

    assert [(p.comm, p.count) for p in out["gie-demo"].processes] == [("pytest", 3)]
    assert out["gie-demo"].processes[0].percent == pytest.approx(150.0)


def test_an_idling_process_is_below_the_threshold(tmp_path):
    """The threshold IS the feature: "working, not idling"."""
    stage_container(tmp_path, pids=[600])
    proc = tmp_path / "proc"
    write_stat(proc, 600, comm="sshd", utime=0, stime=0, starttime=5)
    sampler = make_sampler(tmp_path, [10.0, 11.0])
    item = procstat.SampleInput("gie-demo", 500, None)
    sampler.sample([item])
    write_stat(proc, 600, comm="sshd", utime=1, stime=0, starttime=5)  # ~1% of a core

    assert sampler.sample([item])["gie-demo"].processes == ()


def test_a_recycled_pid_is_not_credited_with_the_old_counter(tmp_path):
    """Same pid, different starttime: a different process. Without the guard
    it inherits the dead process's cumulative ticks and renders an absurd
    percentage."""
    stage_container(tmp_path, pids=[600])
    proc = tmp_path / "proc"
    write_stat(proc, 600, comm="claude", utime=10_000, stime=0, starttime=5)
    sampler = make_sampler(tmp_path, [10.0, 11.0])
    item = procstat.SampleInput("gie-demo", 500, None)
    sampler.sample([item])
    write_stat(proc, 600, comm="bash", utime=0, stime=0, starttime=9999)

    assert sampler.sample([item])["gie-demo"].processes == ()


def test_a_restarted_container_does_not_report_negative_cpu(tmp_path):
    """A restart zeroes the container's cumulative usage."""
    stage_container(tmp_path, pids=[])
    sampler = make_sampler(tmp_path, [10.0, 11.0])
    sampler.sample([procstat.SampleInput("gie-demo", 500, 9_000_000_000)])

    out = sampler.sample([procstat.SampleInput("gie-demo", 500, 1_000_000)])

    assert out["gie-demo"].cpu_percent is None


def test_container_percent_survives_an_unreadable_cgroup(tmp_path):
    """The two halves are deliberately independent: CPU comes from Incus's
    own usage counter, so a host whose cgroup files cannot be read still
    gets a working CPU column — only DOING goes dark."""
    sampler = make_sampler(tmp_path, [10.0, 11.0])  # nothing staged: no /proc, no cgroups
    sampler.sample([procstat.SampleInput("gie-demo", 500, 1_000_000_000)])

    out = sampler.sample([procstat.SampleInput("gie-demo", 500, 2_000_000_000)])

    assert out["gie-demo"].cpu_percent == pytest.approx(100.0)
    assert out["gie-demo"].processes == ()


def test_a_stopped_container_is_reported_empty(tmp_path):
    """No init pid, no usage — and no file reads attempted."""
    sampler = make_sampler(tmp_path, [10.0, 11.0])
    item = procstat.SampleInput("gie-stopped", None, None)
    sampler.sample([item])

    out = sampler.sample([item])

    assert out["gie-stopped"].cpu_percent is None
    assert out["gie-stopped"].processes == ()
