"""The shared inbox-waiter classification: which background jobs are only the follower."""

from __future__ import annotations

from pathlib import Path

import pytest

from jailbee.inbox_waiters import settle_state

HOME = Path("/home/dev")
FOLLOWER = ["/opt/jailbee/venv/bin/python", "-P", "-m", "jailbee.container_main", "inbox", "follow"]
# The wrapper Claude Code's Monitor tool runs the follower under (2.1.330).
WRAPPER = [
    "/bin/bash",
    "-c",
    "source /home/dev/.claude/shell-snapshots/snapshot-bash-1795467655565-mv64l7.sh"
    " 2>/dev/null || true && \\builtin shopt -u extglob 2>/dev/null || true && "
    "{ \\builtin unalias -- 'which'; \\builtin unset -f -- 'which'; } >/dev/null 2>&1 || true"
    " && eval 'jailbee inbox follow' < /dev/null && pwd -P >| /tmp/claude-ab19-cwd",
]


def test_follower_only_is_idle() -> None:
    assert settle_state("shell", [FOLLOWER], home=HOME) == "idle"


def test_wrapper_around_follower_is_idle() -> None:
    assert settle_state("shell", [WRAPPER, FOLLOWER], home=HOME) == "idle"


def test_follower_and_dev_server_stays_shell() -> None:
    assert settle_state("shell", [FOLLOWER, ["npm", "run", "dev"]], home=HOME) == "shell"


@pytest.mark.parametrize("other", [["sleep", "600"], ["bash", "-c", "pytest; jailbee inbox wait"]])
def test_any_other_job_stays_shell(other: list[str]) -> None:
    assert settle_state("shell", [WRAPPER, FOLLOWER, other], home=HOME) == "shell"


def test_no_children_keeps_claudes_state() -> None:
    assert settle_state("shell", [], home=HOME) == "shell"
    assert settle_state("idle", [], home=HOME) == "idle"


@pytest.mark.parametrize("state", ["busy", "waiting", "idle", "odd"])
def test_other_states_are_untouched(state: str) -> None:
    assert settle_state(state, [FOLLOWER], home=HOME) == state


def test_snapshot_outside_home_is_not_trusted() -> None:
    assert settle_state("shell", [WRAPPER, FOLLOWER], home=Path("/home/other")) == "shell"
