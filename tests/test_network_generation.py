from __future__ import annotations

import yaml

from jailbee.network_generation import (
    WORK_BRIDGE,
    default_generation,
    ensure_work_bridge,
    generation_of,
    set_default_generation,
)


def test_missing_host_default_is_legacy(db_session):
    assert default_generation(db_session) == "legacy"


def test_host_default_can_be_migrated_and_undone_repeatedly(db_session):
    set_default_generation(db_session, "work")
    set_default_generation(db_session, "work")
    assert default_generation(db_session) == "work"
    set_default_generation(db_session, "legacy")
    set_default_generation(db_session, "legacy")
    assert default_generation(db_session) == "legacy"


def test_existing_work_profile_identifies_generation(tmp_path):
    from tests.conftest import make_config

    cfg = make_config(tmp_path / "repo")
    assert generation_of(cfg, {"profiles": ["repo-net-work-strict"]}) == "work"
    assert generation_of(cfg, {"profiles": ["repo-net-strict"]}) == "legacy"


def test_unowned_existing_work_bridge_is_not_changed(mocker):
    incus = mocker.MagicMock()
    incus.network_exists.return_value = True
    incus.network_get.return_value = "someone-else"
    try:
        ensure_work_bridge(incus)
    except ValueError as exc:
        assert "not JailBee-owned" in str(exc)
    else:
        raise AssertionError("expected unowned bridge to be refused")
    incus.network_set.assert_not_called()
    incus.network_acl_create.assert_not_called()


def test_work_bridge_is_created_with_deny_baseline_before_return(mocker):
    incus = mocker.MagicMock()
    incus.network_exists.return_value = False
    incus.network_get.side_effect = lambda name, key: {
        "ipv4.address": "auto",
        "ipv4.nat": "true",
        "ipv6.address": "none",
        "ipv6.nat": "false",
        "security.acls": "jailbee-work-baseline",
    }.get(key, "")
    incus.network_type.return_value = "bridge"
    incus.network_acl_show.return_value = yaml.safe_dump(
        {
            "egress": [
                {"action": "allow", "protocol": "udp", "destination_port": "67"},
                {"action": "allow", "protocol": "udp", "destination_port": "53"},
                {"action": "allow", "protocol": "tcp", "destination_port": "53"},
            ],
            "ingress": [{"action": "allow", "protocol": "udp", "destination_port": "68"}],
        }
    )
    ensure_work_bridge(incus)
    incus.network_create.assert_called_once_with(WORK_BRIDGE)
    set_calls = [call.args for call in incus.network_set.call_args_list]
    assert (WORK_BRIDGE, "ipv6.address", "none") in set_calls
    assert (WORK_BRIDGE, "ipv6.nat", "false") in set_calls
    assert (WORK_BRIDGE, "user.jailbee.owner", "work-network-v1") in set_calls
    assert (WORK_BRIDGE, "security.acls", "jailbee-work-baseline") in set_calls
    acl_yaml = incus.network_acl_set_yaml.call_args.args[1]
    acl = yaml.safe_load(acl_yaml)
    assert [r["destination_port"] for r in acl["egress"]] == ["67", "53", "53"]
    assert all(r["action"] == "allow" for r in acl["egress"])


def test_failed_work_bridge_setup_removes_only_its_new_resources(mocker):
    incus = mocker.MagicMock()
    incus.network_exists.return_value = False
    incus.network_set.side_effect = RuntimeError("setup failed")
    try:
        ensure_work_bridge(incus)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected setup failure")
    incus.network_delete.assert_called_once_with(WORK_BRIDGE)
