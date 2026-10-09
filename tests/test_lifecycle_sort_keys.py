"""Every `ls` field sorts, and each kind of value sorts the way a person expects."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from jailbee import lifecycle
from jailbee.agent_status import AgentSummary
from jailbee.git_status import GitStatus
from jailbee.lifecycle import ContainerInfo, ls_field_specs

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

# Fields that may opt out of sorting. Empty on purpose: a new field must
# define `sort`, or be added here with a reason.
UNSORTABLE: frozenset[str] = frozenset()


def _c(**over: object) -> ContainerInfo:
    base = ContainerInfo(
        name="p-a", state="Running", network="strict", ip=None, memory_limit=None, repo="p"
    )
    return dataclasses.replace(base, **over)  # type: ignore[arg-type]  # test helper


def _key(field: str, c: ContainerInfo):  # type: ignore[no-untyped-def]
    spec = next(f for f in ls_field_specs(now=NOW) if f.name == field)
    assert spec.sort is not None
    return spec.sort(c)


def test_every_field_has_a_sort_key():
    missing = [f.name for f in ls_field_specs(now=NOW) if f.sort is None]
    assert sorted(set(missing) - UNSORTABLE) == []


def test_text_keys_ignore_case_and_missing_is_none():
    assert _key("base", _c(base_branch="Main")) == _key("base", _c(base_branch="main"))
    assert _key("base", _c(base_branch=None)) is None
    assert _key("group", _c(credential_group="")) is None


def test_state_ranks_running_frozen_stopped_then_the_rest():
    keys = [_key("state", _c(state=s)) for s in ("Running", "Frozen", "Stopped", "Error")]
    assert keys == sorted(keys)


def test_network_puts_the_soonest_loose_expiry_first_then_no_revert_then_strict():
    soon = _c(network="loose", loose_until=NOW + timedelta(minutes=5))
    later = _c(network="loose", loose_until=NOW + timedelta(hours=2))
    never = _c(network="loose", loose_until=None)
    strict = _c(network="strict")
    keys = [_key("network", c) for c in (soon, later, never, strict)]
    assert keys == sorted(keys)
    assert _key("network", _c(network=None)) is None


def test_ttl_is_none_unless_loose():
    assert _key("ttl", _c(network="strict")) is None
    assert _key("ttl", _c(network="loose", loose_until=NOW + timedelta(minutes=5))) < _key(
        "ttl", _c(network="loose", loose_until=None)
    )


def test_live_memory_and_cpu_use_coarse_steps():
    mib = 1024 * 1024
    assert _key("mem_used", _c(memory_usage=10 * mib)) == _key("mem_used", _c(memory_usage=60 * mib))
    assert _key("mem_used", _c(memory_usage=10 * mib)) < _key("mem_used", _c(memory_usage=70 * mib))
    assert _key("mem_used", _c(state="Stopped", memory_usage=10 * mib)) is None
    assert _key("cpu", _c(cpu_percent=1.0)) == _key("cpu", _c(cpu_percent=4.9))
    assert _key("cpu", _c(cpu_percent=4.9)) < _key("cpu", _c(cpu_percent=5.0))
    assert _key("cpu", _c(cpu_percent=None)) is None
    limited = {"memory_limit": "1GiB"}
    assert _key("mem_pct", _c(memory_usage=10 * mib, **limited)) == _key(
        "mem_pct", _c(memory_usage=40 * mib, **limited)
    )


def test_memory_limit_sorts_by_bytes_not_text():
    assert _key("memory_limit", _c(memory_limit="512MiB")) < _key(
        "memory_limit", _c(memory_limit="2GiB")
    )
    assert _key("memory_limit", _c(memory_limit=None)) is None


def test_ip_sorts_numerically():
    assert _key("ip", _c(ip="10.0.0.9")) < _key("ip", _c(ip="10.0.0.10"))
    assert _key("ip", _c(ip="not-an-ip")) is None


def test_created_sorts_by_time():
    old = _c(created_at=NOW - timedelta(days=2))
    new = _c(created_at=NOW)
    assert _key("created", old) < _key("created", new)
    assert _key("created", _c(created_at=None)) is None


def _git(**over: object) -> GitStatus:
    base = GitStatus(wt="clean", ahead_diff="clean", ahead_count="0", conflict="ok")
    return dataclasses.replace(base, **over)  # type: ignore[arg-type]  # test helper


def test_diff_fields_rank_clean_below_dirty_and_dirty_by_size():
    clean = _key("wt", _c(git_status=_git(wt="clean")))
    small = _key("wt", _c(git_status=_git(wt="+1 -1")))
    big = _key("wt", _c(git_status=_git(wt="+120 -30")))
    assert clean < small < big
    assert _key("wt", _c(git_status=_git(wt="?"))) is None
    assert _key("wt", _c(git_status=None)) is None


def test_counts_parse_and_unknown_is_none():
    assert _key("ahead_count", _c(git_status=_git(ahead_count="3"))) == (3,)
    assert _key("ahead_count", _c(git_status=_git(ahead_count="—"))) is None


def test_conflict_ranks_conflicts_above_ok():
    assert _key("conflict", _c(git_status=_git(conflict="ok"))) < _key(
        "conflict", _c(git_status=_git(conflict="conflict"))
    )


def test_outbox_zero_is_none_so_empty_cells_sort_last():
    assert _key("outbox", _c(git_status=_git())) is None
    assert _key("outbox", _c(git_status=_git(pending_pr_actions=2, pending_issue_actions=1))) == (3,)


def test_agent_ranks_by_urgency():
    def agent(state: str) -> ContainerInfo:
        return _c(agent_status=(AgentSummary("claude", state, NOW, None, 1),))

    keys = [_key("agent", agent(s)) for s in ("waiting", "busy", "shell", "idle", "weird")]
    assert keys == sorted(keys)
    assert _key("agent", _c()) is None
    assert _key("agent_compact", agent("waiting")) == _key("agent", agent("waiting"))


@pytest.mark.parametrize(
    ("field", "expected"),
    [("created", True), ("cpu", True), ("outbox", True), ("name", False), ("state", False), ("network", False)],
)
def test_first_direction(field: str, expected: bool):
    spec = next(f for f in ls_field_specs(now=NOW) if f.name == field)
    assert spec.sort_desc_first is expected


def test_helpers_are_module_level():
    """The key helpers are shared by several fields; pin them so a refactor keeps one copy."""
    assert lifecycle._diff_key("+2 -3") == (1, 5)
    assert lifecycle._diff_key("clean") == (0, 0)
    assert lifecycle._count_key("7") == (7,)
    assert lifecycle._text_key("  ") is None
