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
