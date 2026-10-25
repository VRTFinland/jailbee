"""Tests for the remote orchestration bound and its excess check."""

from __future__ import annotations

import pytest

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.orchestration.grant import Grant, Grantable
from jailbee.remote_ssh.orchestration_policy import (
    GrantBound,
    OrchestratorRights,
    grant_bound,
    grant_excess,
)

ssh = "(remote.ssh.orchestration: ssh)"


def _grant(mounts=(), loose=False, egress=(), children=4):
    return Grant(
        max_running_children=children,
        grantable=Grantable(mounts=mounts, loose=loose, egress=egress),
    )


def test_off_has_no_bound():
    assert grant_bound(RemoteSSHConfig(), ["data"], None) is None


def test_ssh_bound_without_unlocks_allows_nothing_grantable():
    b = grant_bound(RemoteSSHConfig(orchestration="ssh"), ["data"], None)
    assert b == GrantBound("ssh", frozenset(), False, ())


def test_ssh_bound_ignores_mount_roots(tmp_path):
    policy = RemoteSSHConfig(orchestration="ssh", mount_roots=[{"path": str(tmp_path)}])
    assert grant_bound(policy, ["data"], None).mounts == frozenset()


def test_ssh_bound_with_unlocks():
    b = grant_bound(RemoteSSHConfig(orchestration="ssh", network=True, mounts=True), ["data"], None)
    assert b == GrantBound("ssh", frozenset({"data"}), True, None)


def test_orchestrator_bound_is_its_rights_and_ignores_unlocks():
    rights = OrchestratorRights(mounts=("data",), loose=False, egress=("pypi.org",))
    b = grant_bound(
        RemoteSSHConfig(orchestration="orchestrator", network=True, mounts=True),
        ["data", "aws"],
        rights,
    )
    assert b == GrantBound("orchestrator", frozenset({"data"}), False, ("pypi.org",))


def test_orchestrator_bound_requires_rights():
    with pytest.raises(ValueError):
        grant_bound(RemoteSSHConfig(orchestration="orchestrator"), ["data"], None)


def test_excess_names_every_widening_element():
    b = GrantBound("ssh", frozenset(), False, ())
    assert grant_excess(_grant(mounts=("data",), loose=True, egress=("evil.org",)), None, b) == [
        f"mount `data` is beyond what this remote session may grant {ssh}",
        f"loose for children is beyond what this remote session may grant {ssh}",
        f"egress `evil.org` is beyond what this remote session may grant {ssh}",
    ]


def test_excess_keeps_what_the_stored_grant_already_had():
    b = GrantBound("ssh", frozenset(), False, ())
    current = _grant(mounts=("data",), loose=True, egress=("*.example.com",))
    new = _grant(
        mounts=("data",),
        loose=True,
        egress=("api.example.com", "*.example.com"),
        children=8,
    )
    assert grant_excess(new, current, b) == []


def test_excess_ignores_limit_and_operations():
    b = GrantBound("ssh", frozenset(), False, ())
    grant = Grant(max_running_children=64, operations=("destroy", "message", "spawn"))
    assert grant_excess(grant, None, b) == []


def test_egress_any_bound_allows_any_entry():
    b = GrantBound("ssh", frozenset(), True, None)
    assert grant_excess(_grant(egress=("evil.org",)), None, b) == []


def test_orchestrator_egress_bound_uses_coverage():
    b = GrantBound("orchestrator", frozenset(), False, ("*.example.com",))
    assert grant_excess(_grant(egress=("api.example.com",)), None, b) == []
    assert len(grant_excess(_grant(egress=("evil.org",)), None, b)) == 1
