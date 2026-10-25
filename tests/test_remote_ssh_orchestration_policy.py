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


# --- read_orchestrator_rights -------------------------------------------------

from unittest.mock import MagicMock  # noqa: E402

from jailbee.remote_ssh.orchestration_policy import read_orchestrator_rights  # noqa: E402
from tests.conftest import make_cfg  # noqa: E402

NAME = "proj-feat"


def _rights_cfg(tmp_path, readonly=False):
    host = tmp_path / "data"
    host.mkdir(exist_ok=True)
    cfg = make_cfg(
        tmp_path,
        optional_mounts={"data": {"host": str(host), "container": "/data", "readonly": readonly}},
    )
    return cfg, host


def _incus(devices=None, config=None):
    incus = MagicMock()
    incus.list_containers.return_value = [
        {"name": NAME, "devices": devices or {}, "config": config or {}},
        {"name": "other", "devices": {"optional-zzz": {"source": "/x"}}, "config": {}},
    ]
    return incus


def _dev(source, readonly=None):
    props = {"type": "disk", "source": str(source), "path": "/data"}
    if readonly is not None:
        props["readonly"] = readonly
    return {"optional-data": props}


@pytest.fixture
def quiet(mocker):
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.loose_revert.autostart_holds", return_value=False)
    mocker.patch("jailbee.egress_scope.container_extras", return_value=[])


def test_rights_count_a_permanent_as_defined_mount(tmp_path, quiet):
    cfg, host = _rights_cfg(tmp_path)
    assert read_orchestrator_rights(cfg, _incus(_dev(host)), NAME).mounts == ("data",)


def test_rights_count_a_readonly_mount_and_skip_an_unforced_one(tmp_path, quiet):
    cfg, host = _rights_cfg(tmp_path, readonly=True)
    assert read_orchestrator_rights(cfg, _incus(_dev(host, "true")), NAME).mounts == ("data",)
    # An absent `readonly` means writable in Incus, which exceeds a read-only config.
    assert read_orchestrator_rights(cfg, _incus(_dev(host)), NAME).mounts == ()


def test_rights_skip_a_mount_with_a_deadline(tmp_path, quiet):
    cfg, host = _rights_cfg(tmp_path)
    config = {"user.jailbee.mount_until.data": "2999-01-01T00:00:00+00:00"}
    assert read_orchestrator_rights(cfg, _incus(_dev(host), config), NAME).mounts == ()


def test_rights_skip_a_forced_readonly_mount(tmp_path, quiet):
    cfg, host = _rights_cfg(tmp_path, readonly=False)
    assert read_orchestrator_rights(cfg, _incus(_dev(host, "true")), NAME).mounts == ()


def test_rights_skip_a_mount_from_another_source(tmp_path, quiet):
    cfg, _ = _rights_cfg(tmp_path)
    assert read_orchestrator_rights(cfg, _incus(_dev(tmp_path / "elsewhere")), NAME).mounts == ()


def test_rights_loose_only_when_permanent(tmp_path, mocker):
    cfg, _ = _rights_cfg(tmp_path)
    mocker.patch("jailbee.egress_scope.container_extras", return_value=[])
    mode = mocker.patch("jailbee.lifecycle.current_network_mode", return_value="loose")
    held = mocker.patch("jailbee.loose_revert.autostart_holds", return_value=False)
    assert read_orchestrator_rights(cfg, _incus(), NAME).loose is True
    with_ttl = _incus(config={"user.jailbee.loose_until": "2999-01-01T00:00:00+00:00"})
    assert read_orchestrator_rights(cfg, with_ttl, NAME).loose is False
    held.return_value = True
    assert read_orchestrator_rights(cfg, _incus(), NAME).loose is False
    held.return_value = False
    mode.return_value = "strict"
    assert read_orchestrator_rights(cfg, _incus(), NAME).loose is False


def test_rights_egress_is_repo_plus_container_extras(tmp_path, mocker):
    cfg, _ = _rights_cfg(tmp_path)
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.loose_revert.autostart_holds", return_value=False)
    mocker.patch("jailbee.egress_scope.container_extras", return_value=["extra.example.com"])
    expected = (*cfg.effective_egress_allow(), "extra.example.com")
    assert read_orchestrator_rights(cfg, _incus(), NAME).egress == expected


@pytest.mark.parametrize(
    "label", ["garbage", "", "2999-01-01T00:00:00", "2000-01-01T00:00:00+00:00"]
)
def test_rights_any_mount_until_label_makes_a_mount_temporary(tmp_path, quiet, label):
    cfg, host = _rights_cfg(tmp_path)
    config = {"user.jailbee.mount_until.data": label}
    assert read_orchestrator_rights(cfg, _incus(_dev(host), config), NAME).mounts == ()


def test_rights_are_empty_when_the_container_row_is_missing(tmp_path, quiet):
    cfg, _ = _rights_cfg(tmp_path)
    rights = read_orchestrator_rights(cfg, _incus(), "no-such")
    assert rights.mounts == () and rights.loose is False


@pytest.mark.parametrize("spelling", ["1", "yes", "on", "TRUE"])
def test_rights_forced_readonly_in_any_incus_spelling_is_not_counted(tmp_path, quiet, spelling):
    cfg, host = _rights_cfg(tmp_path, readonly=False)
    assert read_orchestrator_rights(cfg, _incus(_dev(host, spelling)), NAME).mounts == ()


def test_rights_readonly_config_matches_a_numeric_readonly_device(tmp_path, quiet):
    cfg, host = _rights_cfg(tmp_path, readonly=True)
    assert read_orchestrator_rights(cfg, _incus(_dev(host, "1")), NAME).mounts == ("data",)


# --- wildcard direction, in-bound grants, rights_from_info --------------------


def test_wildcard_grant_is_not_covered_by_an_exact_stored_entry():
    b = GrantBound("ssh", frozenset(), False, ())
    current = _grant(egress=("api.example.com",))
    assert len(grant_excess(_grant(egress=("*.example.com",)), current, b)) == 1


def test_wildcard_grant_is_not_covered_by_an_exact_orchestrator_bound():
    b = GrantBound("orchestrator", frozenset(), False, ("api.example.com",))
    assert len(grant_excess(_grant(egress=("*.example.com",)), None, b)) == 1


def test_mount_inside_the_bound_passes():
    b = GrantBound("orchestrator", frozenset({"data"}), False, ())
    assert grant_excess(_grant(mounts=("data",)), None, b) == []


def test_loose_passes_when_the_bound_allows_it():
    b = GrantBound("ssh", frozenset(), True, None)
    assert grant_excess(_grant(loose=True), None, b) == []


def test_loose_passes_when_the_stored_grant_already_has_it():
    b = GrantBound("ssh", frozenset(), False, ())
    assert grant_excess(_grant(loose=True), _grant(loose=True), b) == []


def _info(**fields):
    from jailbee.lifecycle import ContainerInfo

    base = {"name": NAME, "state": "running", "network": "strict", "ip": None}
    return ContainerInfo(**{**base, "memory_limit": None, **fields})


def test_rights_from_info_drops_mounts_with_a_deadline():
    from datetime import UTC, datetime

    from jailbee.remote_ssh.orchestration_policy import rights_from_info

    info = _info(
        optional_mounts=("data", "aws"),
        mount_until={"aws": datetime(2999, 1, 1, tzinfo=UTC)},
    )
    assert rights_from_info(info).mounts == ("data",)


def test_rights_from_info_loose_needs_no_deadline():
    from datetime import UTC, datetime

    from jailbee.remote_ssh.orchestration_policy import rights_from_info

    timed = _info(network="loose", loose_until=datetime(2999, 1, 1, tzinfo=UTC))
    assert rights_from_info(timed).loose is False
    assert rights_from_info(_info(network="loose", loose_until=None)).loose is True
    assert rights_from_info(_info(network="strict")).loose is False


def test_rights_from_info_egress_is_the_container_extras():
    from jailbee.remote_ssh.orchestration_policy import rights_from_info

    assert rights_from_info(_info(egress_extra=("a.example.com",))).egress == ("a.example.com",)
