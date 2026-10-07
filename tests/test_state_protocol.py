"""Wire protocol round-trips — above all, that a snapshot loses no field."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jailbee.accounts.models import AgentActivity
from jailbee.agent_status import AgentSummary
from jailbee.dashboard.model import AppMenuEntry, RepoGroup
from jailbee.git_status import GitStatus, SubmoduleChange
from jailbee.lifecycle import ContainerInfo
from jailbee.procstat import ProcessActivity
from jailbee.state_service.protocol import (
    PROTOCOL,
    Active,
    GatherError,
    Hello,
    ProtocolError,
    Refresh,
    Shutdown,
    Snapshot,
    decode,
    encode,
)

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def _non_defaults(cls, **given):
    """Assert every field of ``cls`` that has a default is given a different value."""
    for f in dataclasses.fields(cls):
        default = (
            f.default
            if f.default is not dataclasses.MISSING
            else f.default_factory()
            if f.default_factory is not dataclasses.MISSING
            else dataclasses.MISSING
        )
        assert f.name in given, f"{cls.__name__}.{f.name} not populated"
        assert given[f.name] != default, f"{cls.__name__}.{f.name} left at its default"
    return given


def _full_snapshot() -> Snapshot:
    git = GitStatus(
        **_non_defaults(
            GitStatus,
            wt="+1 -2",
            ahead_diff="+3 -4",
            ahead_count="5",
            conflict="conflict",
            submodules=(SubmoduleChange("sub", 1, 2, 3, 4, 5, "new", 6),),
            head_sha="abc123",
            remote_contained=True,
            local_diff="+7 -8",
            local_count="9",
            in_progress="merge",
            unmerged=2,
            pending_pr_actions=1,
            pending_issue_actions=3,
            target_diff="+1 -1",
            behind_count="0",
            base_sha="def456",
            base_source="branch",
            tracking_relation="ahead",
            upstream_ref="origin/main",
        )
    )
    container = ContainerInfo(
        **_non_defaults(
            ContainerInfo,
            name="alpha-x",
            state="Running",
            network="strict",
            ip="10.0.0.2",
            memory_limit="4GiB",
            repo="alpha",
            mode="mount",
            loose_until=T0 + timedelta(hours=1),
            base_branch="main",
            repo_dir="/home/u/alpha",
            pr_number=7,
            pr_author=True,
            credential_group="team",
            created_at=T0,
            memory_usage=1024,
            init_pid=4242,
            cpu_usage_ns=99,
            cpu_limit="2",
            cpu_percent=12.5,
            activity=(ProcessActivity("node", 50.0, 2),),
            agent_status=(
                AgentSummary(
                    "claude",
                    "busy",
                    T0,
                    "permission",
                    2,
                    AgentActivity(
                        **_non_defaults(
                            AgentActivity,
                            last_tool="Bash  ls <b>",
                            last_message="[red]done[/red]",
                            subagents=2,
                            shells=1,
                            modified=123.5,
                            state="idle",
                            since=T0 - timedelta(hours=1),
                        )
                    ),
                ),
            ),
            git_status=git,
            job_phase="running",
            job_pid=77,
            job_kind="create",
            job_error="boom",
            optional_mounts=("ssh",),
        )
    )
    group = RepoGroup(
        **_non_defaults(
            RepoGroup,
            prefix="alpha",
            repo_root="/home/u/alpha",
            config_path=Path("/home/u/alpha/.jailbee/config.yaml"),
            containers=[container],
            apps=[AppMenuEntry("ide", "VS Code")],
            loose_ttl_default="30m",
            push_action_default="merge",
            push_source_default="branch",
            column_notice="note",
            agent_homes=(("alpha-x", "claude", Path("/home/u/.claude")),),
            agent_config_homes=(("alpha-x", "claude", Path("/home/u/.claude")),),
            optional_mounts=("ssh", "gpg"),
        )
    )
    return Snapshot(seq=3, gathered_at=T0, git_enabled=True, groups=[group])


@pytest.mark.parametrize(
    "message",
    [
        Hello(PROTOCOL, "1.2.3", "/home/u/alpha"),
        Hello(PROTOCOL, "1.2.3"),
        Active(False),
        Refresh(),
        Shutdown(),
        GatherError("incus is down"),
        _full_snapshot(),
    ],
    ids=lambda m: type(m).__name__,
)
def test_every_message_round_trips(message):
    line = encode(message)
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert decode(line) == message


def test_a_snapshot_decodes_to_the_real_types():
    group = decode(encode(_full_snapshot())).groups[0]
    container = group.containers[0]
    assert isinstance(group.config_path, Path)
    assert isinstance(group.apps[0], AppMenuEntry)
    assert isinstance(container.activity, tuple)
    assert isinstance(container.git_status.submodules[0], SubmoduleChange)
    assert container.created_at == T0


@pytest.mark.parametrize(
    "line",
    [
        b"",
        b"not json\n",
        b"[1]\n",
        b"{}\n",
        b'{"type":"nope"}\n',
        b'{"type":"active"}\n',
        b'{"type":["a"]}\n',
        b'{"type":{}}\n',
        b"\xff\xfe\n",
        b'{"type":"active","value":"maybe"}\n',
    ],
)
def test_garbage_is_a_protocol_error(line):
    with pytest.raises(ProtocolError):
        decode(line)
