"""The shared remote checks refuse everything under the default `remote.web` policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from jailbee.config.models_remote import RemoteMountRoot, RemotePolicy, RemoteWebConfig
from jailbee.remote_ssh.mount_policy import grantable_kinds, remote_mount_grant
from jailbee.remote_ssh.orchestration_policy import grant_bound
from jailbee.remote_ssh.repo_scope import RemoteRepoScope
from jailbee.remote_ssh.router import (
    NO_UNLOCKS,
    RemoteUnlocks,
    RouteError,
    allowed_command_paths,
    policy_allows,
)


def _web() -> RemotePolicy:
    return RemoteWebConfig().policy


def test_default_web_policy_refuses_every_command() -> None:
    policy = _web()
    unlocks = RemoteUnlocks.of(policy, transport="web")
    assert unlocks == RemoteUnlocks(transport="web")
    assert (
        allowed_command_paths(policy.commands, restrict_host=policy.restrict_host, unlocks=unlocks)
        == frozenset()
    )
    with pytest.raises(RouteError):
        policy_allows(
            ("ls",), policy.commands, restrict_host=policy.restrict_host, unlocks=unlocks
        )


def test_default_web_policy_refuses_host_access() -> None:
    policy = _web()
    assert policy.restrict_host is True
    for argv in (("config", "edit"), ("mount",), ("orchestrate", "grant")):
        with pytest.raises(RouteError):
            policy_allows(
                argv, policy.commands, restrict_host=policy.restrict_host, unlocks=NO_UNLOCKS
            )


def test_web_refusal_names_the_web_switch() -> None:
    policy = RemotePolicy(commands={"mode": "full"})
    unlocks = RemoteUnlocks.of(policy, transport="web")
    with pytest.raises(RouteError, match=r"remote\.web\.orchestration"):
        policy_allows(
            ("orchestrate", "grant"), policy.commands, restrict_host=True, unlocks=unlocks
        )


def test_default_web_policy_refuses_mounts(make_cfg, tmp_path: Path) -> None:
    host = (tmp_path / "t" / "data").resolve()
    host.mkdir(parents=True)
    cfg = make_cfg(tmp_path, optional_mounts={"d": {"host": str(host), "container": "/d"}})
    policy = _web()
    decision = remote_mount_grant(policy, cfg, "d", transport="web")
    assert isinstance(decision, str)
    assert "remote.web.mount_roots" in decision
    assert grantable_kinds(policy, cfg, transport="web") == ()


def test_web_mount_root_grants_only_that_root(make_cfg, tmp_path: Path) -> None:
    host = (tmp_path / "t" / "data").resolve()
    host.mkdir(parents=True)
    cfg = make_cfg(tmp_path, optional_mounts={"d": {"host": str(host), "container": "/d"}})
    policy = RemotePolicy(mount_roots=[RemoteMountRoot(path=host)])
    assert grantable_kinds(policy, cfg, transport="web") == ("d",)


def test_default_web_policy_has_no_orchestration_bound() -> None:
    assert grant_bound(_web(), ["data"], None) is None


def test_repo_scope_reads_the_policy_excluded_repos() -> None:
    scope = RemoteRepoScope.of(RemotePolicy(excluded_repos=["secret"]))
    assert not scope.allows("secret")
    assert scope.allows("other")
