"""Wire protocol round-trips — above all, that a snapshot loses no field."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jailbee.accounts.models import ActivityEvent, AgentActivity
from jailbee.agent_status import AgentSummary
from jailbee.dashboard.model import AppMenuEntry, RepoAdvisory, RepoGroup
from jailbee.git_status import GitStatus, SubmoduleChange
from jailbee.lifecycle import ContainerInfo
from jailbee.orchestration.view import ChildView, GrantView
from jailbee.procstat import ProcessActivity, ProcessMemory
from jailbee.state_service import protocol
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
from jailbee.update_check import UpdateNotice

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
            unread_inbox=3,
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
            memory_override=True,
            repo="alpha",
            mode="mount",
            loose_until=T0 + timedelta(hours=1),
            base_branch="main",
            repo_dir="/home/u/alpha",
            pr_number=7,
            pr_author=True,
            credential_group="team",
            autostart_profile="light",
            autostart_failed=2,
            autostart_rerunnable=True,
            created_at=T0,
            uuid="u-alpha",
            outbox_pr=2,
            outbox_issue=1,
            memory_usage=1024,
            init_pid=4242,
            cpu_usage_ns=99,
            cpu_limit="2",
            cpu_percent=12.5,
            activity=(ProcessActivity("node", 50.0, 2),),
            top_cpu=(ProcessActivity("node", 50.0, 2), ProcessActivity("sshd", 1.0, 1)),
            top_mem=(ProcessMemory("qemu", 1 << 30, 3),),
            root_pool="default",
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
                            background_clear=False,
                            shells=1,
                            modified=123.5,
                            state="idle",
                            since=T0 - timedelta(hours=1),
                            last_event_at=T0 - timedelta(seconds=14),
                            recent=(
                                ActivityEvent("tool", "Read  /a.py"),
                                ActivityEvent("message", "[b]plan[/b]"),
                            ),
                        )
                    ),
                ),
            ),
            git_status=git,
            job_live=True,
            sent_count=2,
            job_phase="running",
            job_pid=77,
            job_kind="create",
            job_error="boom",
            optional_mounts=("ssh",),
            configured_mounts=("ssh", "gpg"),
            mount_until={"ssh": T0 + timedelta(minutes=15)},
            egress_extra=("api.example.com",),
            alias="login",
            fork_of="alpha-y",
            grant=GrantView(
                4,
                ("message", "spawn"),
                ("data",),
                True,
                ("pypi.org",),
                "claude opus",
                publish_issues=("comment", "create"),
                publish_foreign_repos=("org/lib",),
                publish_foreign_actions=("comment",),
                publish_prs=True,
                publish_pr_reviews=True,
                publish_rate=12,
                publish_projects=(("org/3", ("remove", "set")),),
            ),
            child=ChildView("alpha-orch", "done", ("data",), True, ("pypi.org",), "claude-jb opus"),
            running_children=2,
            queued_children=3,
            orchestrator_running=True,
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
            memory_default="8GiB",
            autostart_profiles=("full", "light"),
            autostart_default_profile="full",
            autostart_services=("db", "web"),
            orchestration_profiles=("bugfixer",),
            default_runner="claude haiku",
            default_runner_source="repo",
            runner_profiles=("grok",),
            sent_count=4,
            push_action_default="merge",
            push_source_default="branch",
            column_notice="note",
            agent_homes=(("alpha-x", "claude", Path("/home/u/.claude")),),
            agent_config_homes=(("alpha-x", "claude", Path("/home/u/.claude")),),
            optional_mounts=("ssh", "gpg"),
            mount_ttl_defaults={"ssh": "15m", "gpg": None},
            remote_mountable=("data",),
            checkout="feat-1",
            advisories=(
                RepoAdvisory("apply", "alpha", ("l1", "l2"), "1.7.3", "owed", "apply"),
                RepoAdvisory("legacy-chrome-block", "/f.yaml", ("n",), "", "n"),
            ),
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
        Refresh(8.0),
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


def test_snapshot_round_trips_the_update_notice():
    notice = UpdateNotice(current="1.4.0", latest="1.5.0", command="uv tool upgrade jailbee")
    snapshot = dataclasses.replace(_full_snapshot(), update=notice)

    assert decode(encode(snapshot)).update == notice


def test_a_snapshot_without_an_update_notice_decodes_to_none():
    assert decode(encode(_full_snapshot())).update is None


def test_snapshot_preserves_mount_ttl_defaults():
    snapshot = _full_snapshot()
    snapshot.groups[0].mount_ttl_defaults = {"aws": "15m", "docs": None}
    restored = decode(encode(snapshot))
    assert restored.groups[0].mount_ttl_defaults == {"aws": "15m", "docs": None}


def test_snapshot_preserves_remote_mountable():
    snapshot = _full_snapshot()
    snapshot.groups[0] = snapshot.groups[0].__class__(
        **{**snapshot.groups[0].__dict__, "remote_mountable": ("data",)}
    )
    restored = decode(encode(snapshot))
    assert restored.groups[0].remote_mountable == ("data",)


def test_a_snapshot_decodes_to_the_real_types():
    group = decode(encode(_full_snapshot())).groups[0]
    container = group.containers[0]
    assert isinstance(group.config_path, Path)
    assert isinstance(group.apps[0], AppMenuEntry)
    assert isinstance(container.activity, tuple)
    assert isinstance(container.git_status.submodules[0], SubmoduleChange)
    assert container.created_at == T0
    assert container.mount_until == {"ssh": T0 + timedelta(minutes=15)}
    assert container.egress_extra == ("api.example.com",)
    assert isinstance(container.mount_until["ssh"], datetime)
    activity = container.agent_status[0].activity
    assert isinstance(activity.recent, tuple)
    assert [type(e) for e in activity.recent] == [ActivityEvent, ActivityEvent]


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


def test_the_protocol_version_and_namespace_carry_the_activity_history():
    # Event ages require a server which supplies last_event_at; 8 added RepoGroup.advisories,
    # Snapshot.system and each container's top_cpu, top_mem and root_pool.
    # 15 added repo/container autostart profile state.
    assert protocol._NAMESPACE["RepoAdvisory"] is RepoAdvisory
    # Pinned so the entry survives ActivityEvent ever moving to a module that
    # imports it under TYPE_CHECKING, where pydantic could no longer resolve it.
    assert protocol._NAMESPACE["ActivityEvent"] is ActivityEvent


def test_an_unknown_activity_event_kind_is_a_protocol_error():
    line = encode(_full_snapshot()).replace(b'"kind":"tool"', b'"kind":"bogus"', 1)
    assert b'"kind":"bogus"' in line
    with pytest.raises(ProtocolError):
        decode(line)


def test_snapshot_round_trips_top_processes():
    container = ContainerInfo(
        name="alpha-x",
        state="Running",
        network=None,
        ip=None,
        memory_limit=None,
        top_cpu=(ProcessActivity("node", 50.0, 2),),
        top_mem=(ProcessMemory("qemu", 1 << 30, 3), ProcessMemory("node", 4096, 1)),
    )
    group = RepoGroup(
        prefix="alpha", repo_root="/r", config_path=Path("/r/c.yaml"), containers=[container]
    )
    snapshot = Snapshot(seq=1, gathered_at=T0, git_enabled=False, groups=[group])

    assert decode(encode(snapshot)) == snapshot


def test_snapshot_round_trips_system_status():
    from jailbee.dashboard.system import PoolStatus, SystemStatus
    from jailbee.sysstat import DiskStatus, LoadStatus, MemoryStatus

    system = SystemStatus(
        memory=MemoryStatus(
            total=16 << 30,
            available=4 << 30,
            cache=1 << 30,
            swap_total=8 << 30,
            swap_free=1 << 30,
            swap_cached=1 << 20,
            zswapped=5 << 30,
        ),
        pools=(PoolStatus("default", DiskStatus(total=100, available=40)),),
        root=DiskStatus(total=50, available=10),
        load=LoadStatus(load1=1.25, cpus=8),
    )
    snapshot = Snapshot(seq=1, gathered_at=T0, git_enabled=False, groups=[], system=system)

    assert decode(encode(snapshot)) == snapshot
    assert decode(encode(snapshot)).system == system


def test_a_snapshot_without_load_decodes_to_none():
    from jailbee.dashboard.system import SystemStatus

    snapshot = Snapshot(
        seq=1, gathered_at=T0, git_enabled=False, groups=[], system=SystemStatus(None, (), None)
    )
    line = encode(snapshot)
    assert b',"load":null' in line
    restored = decode(line.replace(b',"load":null', b"", 1))
    assert restored.system is not None and restored.system.load is None


def test_combined_protocol_preserves_builtin_choices_and_rerun_state():
    snapshot = _full_snapshot()
    snapshot.groups[0] = dataclasses.replace(
        snapshot.groups[0], autostart_profiles=("full", "light", "none", "all")
    )
    group = decode(encode(snapshot)).groups[0]
    assert group.autostart_profiles == ("full", "light", "none", "all")
    assert group.autostart_services == ("db", "web")
    container = group.containers[0]
    assert container.autostart_failed == 2
    assert container.job_live is True
    assert container.autostart_rerunnable is True


def test_snapshot_round_trips_sent_counts():
    container = ContainerInfo(
        name="alpha-x", state="Running", network=None, ip=None, memory_limit=None, sent_count=3
    )
    group = RepoGroup(
        prefix="alpha",
        repo_root="/r",
        config_path=Path("/r/c.yaml"),
        containers=[container],
        sent_count=7,
    )
    snapshot = Snapshot(seq=1, gathered_at=T0, git_enabled=False, groups=[group])

    restored = decode(encode(snapshot)).groups[0]

    assert restored.sent_count == 7
    assert restored.containers[0].sent_count == 3


def test_wire_schema_names_every_field_that_crosses_the_wire():
    schema = protocol.wire_schema()

    memory = schema["snapshot"]["$defs"]["MemoryStatus"]["properties"]
    assert {"swap_cached", "zswapped"} <= memory.keys()
    assert schema["hello"]["properties"].keys() == {"protocol", "version", "cwd_root"}


def test_wire_schema_leaves_out_docstrings_and_titles():
    # Neither travels on the wire, so rewording a docstring is no protocol change.
    # (A field named "title" maps to a schema dict, never a string.)
    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str):
                    yield key
                yield from keys(value)
        elif isinstance(node, list):
            for item in node:
                yield from keys(item)

    found = set(keys(protocol.wire_schema()))
    assert not found & {"description", "title"}


# The wire schema PROTOCOL was last bumped for. A schema change under the same
# PROTOCOL fails below: a client of another version would misread it. A pydantic
# upgrade that only reshapes its schema output trips this too; a bump is then
# harmless (a protocol mismatch makes the dashboard replace the state service).
WIRE_SCHEMA = (23, "ab57952e895a9a861061bf34b6dc4a9477a4cd740e2ac7bde5b7eef94ff4de30")


def test_a_wire_schema_change_bumps_the_protocol():
    canonical = json.dumps(protocol.wire_schema(), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    pinned_protocol, pinned_digest = WIRE_SCHEMA
    if PROTOCOL == pinned_protocol:
        assert digest == pinned_digest, (
            f"the wire schema changed under PROTOCOL {PROTOCOL}: bump PROTOCOL in "
            f"state_service/protocol.py (and its history comment), then set "
            f"WIRE_SCHEMA = ({PROTOCOL + 1}, {digest!r})"
        )
    else:
        assert PROTOCOL > pinned_protocol, "PROTOCOL only goes up"
        pytest.fail(f"PROTOCOL was bumped: set WIRE_SCHEMA = ({PROTOCOL}, {digest!r})")
