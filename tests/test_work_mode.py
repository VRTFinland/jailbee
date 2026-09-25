"""Tests for stable work-network mode transitions and health state."""

from unittest.mock import MagicMock

from jailbee.work_mode import switch_work_network, work_mode_state


def test_strict_switch_reconciles_extras_before_marker_and_exception_removal(
    make_cfg, tmp_path, mocker
):
    cfg = make_cfg(tmp_path / "repo")
    name = f"{cfg.container_prefix}-feature"
    nic = {
        "type": "nic",
        "network": "jailbee-work",
        "ipv4.address": "10.42.0.2",
        "security.ipv4_filtering": "true",
        "security.acls": "old",
    }
    raw = {
        "name": name,
        "profiles": [f"{cfg.container_prefix}-net-work-loose"],
        "devices": {"eth0": nic},
    }
    incus = MagicMock()
    incus.list_containers.return_value = [raw]
    order = []
    mocker.patch(
        "jailbee.work_acl.apply_work_container_acl",
        side_effect=lambda *_, **__: order.append("nic"),
    )
    incus.profile_assign.side_effect = lambda *_: order.append("marker")
    mocker.patch(
        "jailbee.work_acl.revoke_work_loose", side_effect=lambda *_: order.append("revoke")
    )
    mocker.patch("jailbee.hosts.apply_hosts")

    switch_work_network(cfg, incus, name, "strict")

    assert order == ["nic", "marker", "revoke"]
    incus.profile_assign.assert_called_once()
    assert incus.profile_assign.call_args.args[1] == [f"{cfg.container_prefix}-net-work-strict"]


def test_work_mode_state_requires_bridge_source_policy_agreement(make_cfg, tmp_path, mocker):
    cfg = make_cfg(tmp_path / "repo")
    raw = {
        "profiles": [f"{cfg.container_prefix}-net-work-loose"],
        "devices": {
            "eth0": {
                "type": "nic",
                "network": "jailbee-work",
                "ipv4.address": "10.42.0.2",
                "security.ipv4_filtering": "true",
                "security.acls": "",
            }
        },
    }
    mocker.patch("jailbee.work_acl.work_loose_policy_matches", return_value=False)

    mode, agrees = work_mode_state(cfg, raw, MagicMock())

    assert mode == "strict"
    assert not agrees
