"""Tests for source-scoped work bridge ACL management."""

from unittest.mock import MagicMock

import pytest
import yaml

from jailbee.egress import EgressEntry
from jailbee.egress_scope import extra_acl_name
from jailbee.network import extra_acl_yaml
from jailbee.work_acl import (
    ensure_work_repo_acl,
    grant_work_loose,
    reconcile_work_acl,
    revoke_work_loose,
)


def container(name: str, ip: str = "10.42.0.2", mode: str = "loose") -> dict:
    return {
        "name": name,
        "profiles": [f"{name.split('-')[0]}-net-work-{mode}"],
        "devices": {
            "eth0": {
                "type": "nic",
                "network": "jailbee-work",
                "ipv4.address": ip,
                "security.ipv4_filtering": "true",
                "security.acls": f"{name.split('-')[0]}-allowlist",
            }
        },
    }


def test_grant_adds_repo_acl_without_losing_baseline_or_other_repo(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path / "repo")
    name = f"{cfg.container_prefix}-a"
    incus = MagicMock()
    incus.network_get.return_value = "jailbee-work-baseline,other-allowlist"
    incus.list_containers.return_value = [container(name)]
    incus.network_acl_exists.return_value = True

    grant_work_loose(cfg, incus, name)

    incus.network_set.assert_called_once_with(
        "jailbee-work",
        "security.acls",
        f"jailbee-work-baseline,other-allowlist,{cfg.container_prefix}-work-loose",
    )
    rendered = yaml.safe_load(incus.network_acl_set_yaml.call_args.args[1])
    assert rendered["egress"] == [{"action": "allow", "source": "10.42.0.2/32", "state": "enabled"}]


def test_ensure_repo_acl_attaches_allowlist_extras_and_preserves_other_repos(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path / "repo")
    name = f"{cfg.container_prefix}-a"
    repo_extra = extra_acl_name(name)
    union_name = f"{cfg.container_prefix}-container-extras"
    incus = MagicMock()
    incus.network_get.return_value = (
        f"jailbee-work-baseline,other-repo-allowlist,{cfg.container_prefix}-allowlist,"
        "jailbee-work-baseline"
    )
    incus.list_containers.return_value = [container(name)]
    incus.network_acl_exists.return_value = True
    incus.network_acl_show.return_value = extra_acl_yaml(
        repo_extra,
        [EgressEntry(destinations=["203.0.113.8", "203.0.113.9"], port=443, description="test")],
    )

    ensure_work_repo_acl(cfg, incus)

    incus.network_set.assert_called_once_with(
        "jailbee-work",
        "security.acls",
        f"jailbee-work-baseline,{cfg.container_prefix}-allowlist,{union_name},other-repo-allowlist",
    )
    incus.network_acl_set_yaml.assert_called_once()
    union = yaml.safe_load(incus.network_acl_set_yaml.call_args.args[1])
    assert union["name"] == union_name
    assert [rule["destination"] for rule in union["egress"]] == [
        "203.0.113.8",
        "203.0.113.9",
    ]


@pytest.mark.parametrize(
    "occupant, message",
    [
        (
            {
                **container("repo-a", mode="strict"),
                "devices": {
                    "eth0": {
                        **container("repo-a")["devices"]["eth0"],
                        "security.ipv4_filtering": "false",
                    }
                },
            },
            "ipv4_filtering",
        ),
        (
            {"name": "foreign", "profiles": [], "devices": {"eth0": {"network": "jailbee-work"}}},
            "Unexpected",
        ),
        (container("repo-a", ip="not-an-ip"), "IPv4"),
    ],
)
def test_grant_rejects_unverified_bridge_occupants(make_cfg, tmp_path, occupant, message):
    cfg = make_cfg(tmp_path / "repo")
    name = f"{cfg.container_prefix}-a"
    if occupant.get("name") == "repo-a":
        occupant["name"] = name
    incus = MagicMock()
    incus.network_get.return_value = "jailbee-work-baseline"
    incus.list_containers.return_value = [occupant]

    with pytest.raises(ValueError, match=message):
        grant_work_loose(cfg, incus, name)
    incus.network_set.assert_not_called()
    incus.network_acl_set_yaml.assert_not_called()


def test_grant_rejects_duplicate_ips(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path / "repo")
    first, second = f"{cfg.container_prefix}-a", f"{cfg.container_prefix}-b"
    incus = MagicMock()
    incus.network_get.return_value = "jailbee-work-baseline"
    incus.list_containers.return_value = [container(first), container(second)]
    with pytest.raises(ValueError, match="Duplicate"):
        grant_work_loose(cfg, incus, first)
    incus.network_set.assert_not_called()


def test_revoke_preserves_baseline_and_unrelated_acl_names(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path / "repo")
    incus = MagicMock()
    incus.network_get.return_value = "jailbee-work-baseline,other-allowlist,repo-work-loose"
    incus.list_containers.return_value = []
    revoke_work_loose(cfg, incus, f"{cfg.container_prefix}-a")
    incus.network_set.assert_called_once_with(
        "jailbee-work", "security.acls", "jailbee-work-baseline,other-allowlist"
    )


def test_reconcile_is_idempotent_and_uses_loose_markers(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path / "repo")
    name = f"{cfg.container_prefix}-a"
    incus = MagicMock()
    attached = ["jailbee-work-baseline"]
    incus.network_get.side_effect = lambda _bridge, _key: ",".join(attached)

    def set_network(_bridge, _key, value):
        attached[:] = value.split(",")

    incus.network_set.side_effect = set_network
    incus.list_containers.return_value = [container(name)]
    incus.network_acl_exists.return_value = True
    reconcile_work_acl(cfg, incus)
    incus.network_set.reset_mock()
    reconcile_work_acl(cfg, incus)
    incus.network_set.assert_not_called()
