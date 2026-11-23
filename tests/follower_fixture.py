"""A container whose agent runs the inbox follower, gathered from a fake /proc.

Shared by `tests/test_agent_idle_follower.py` and the web e2e suite: the
container's AGENT state comes out of the dashboards' real gather step
(`dashboard.model.sample_activity`) with a real `ActivitySampler` on a fake
/proc and cgroup tree and a real Claude session file, not a hand-set state.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from jailbee import procstat
from jailbee.dashboard.model import sample_activity
from tests.web_fakes import container, repo_group

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.dashboard.model import RepoGroup
    from jailbee.lifecycle import ContainerInfo

CLAUDE = 1010  # host pid; 42 inside the container
FOLLOWER = ("/opt/jailbee/venv/bin/python", "-P", "-m", "jailbee.container_main", "inbox", "follow")
WRAPPER = (
    "/bin/bash",
    "-c",
    "source /home/dev/.claude/shell-snapshots/snapshot-bash-1795467655565-mv64l7.sh"
    " 2>/dev/null || true && \\builtin shopt -u extglob 2>/dev/null || true"
    " && eval 'jailbee inbox follow' < /dev/null && pwd -P >| /tmp/claude-ab19-cwd",
)


def _process(proc: Path, pid: int, ppid: int, comm: str, argv: tuple[str, ...]) -> None:
    d = proc / str(pid)
    d.mkdir(parents=True)
    before = ["S", str(ppid)] + ["0"] * 9
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join([*before, "0", "0", *["0"] * 6, "777"]))
    (d / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in argv))
    (d / "status").write_text(f"Name:\t{comm}\nNSpid:\t{pid}\t{pid - CLAUDE + 42}\n")


def gather(
    tmp_path: Path,
    state: str,
    children: list[tuple[int, int, str, tuple[str, ...]]],
    *,
    name: str = "alpha-1",
    prefix: str = "alpha",
) -> tuple[ContainerInfo, RepoGroup]:
    """Gather one container whose Claude session file says `state`.

    `children` are `(host pid, parent host pid, comm, argv)` under the Claude
    process (host pid `CLAUDE`).
    """
    proc, cgroup = tmp_path / "proc", tmp_path / "cgroup"
    (proc / "500").mkdir(parents=True)
    (proc / "500" / "cgroup").write_text(f"0::/lxc.payload.{name}/init.scope\n")
    _process(proc, CLAUDE, 1, "claude", ("claude",))
    for pid, ppid, comm, argv in children:
        _process(proc, pid, ppid, comm, argv)
    procs = cgroup / f"lxc.payload.{name}"
    procs.mkdir(parents=True)
    pids = [CLAUDE, *(pid for pid, *_ in children)]
    (procs / "cgroup.procs").write_text("".join(f"{p}\n" for p in pids))
    home = tmp_path / "session-home"
    (home / "sessions").mkdir(parents=True)
    (home / "sessions" / "42.json").write_text(
        json.dumps({"pid": 42, "procStart": "777", "status": state, "sessionId": "s1"})
    )

    info = container(name, prefix, init_pid=500, cpu_usage_ns=None)
    group = repo_group(prefix, info)
    group.agent_homes = ((name, "claude", home),)
    sampler = procstat.ActivitySampler(proc_root=proc, cgroup_root=cgroup)
    sample_activity([group], sampler)
    return info, group
