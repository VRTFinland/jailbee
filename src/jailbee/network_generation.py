"""Host-level opt-in and instance markers for the work network generation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import yaml
from sqlmodel import Session

from jailbee.db.models import HostNetworkDefault

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

Generation = Literal["legacy", "work"]
WORK_BRIDGE: str = "jailbee-work"
_OWNER = "work-network-v1"
_BASELINE_ACL = "jailbee-work-baseline"


def default_generation(session: Session) -> Generation:
    """Return the stored host default, falling back to legacy for old DBs."""
    row = session.get(HostNetworkDefault, 1)
    if row is None:
        return "legacy"
    return "work" if row.generation == "work" else "legacy"


def set_default_generation(session: Session, generation: Generation) -> None:
    """Persist the default used for future instances only."""
    row = session.get(HostNetworkDefault, 1)
    if row is None:
        row = HostNetworkDefault(id=1, generation=generation)
    else:
        row.generation = generation
    session.add(row)
    session.commit()


def generation_of(cfg: Config, raw: dict[str, Any]) -> Generation:
    """Determine an instance generation from its profile marker, not host DB."""
    del cfg  # Marker names are globally reserved and deliberately repo-independent.
    profiles = raw.get("profiles") or []
    if any(
        isinstance(profile, str)
        and profile.endswith(("-net-work-strict", "-net-work-loose"))
        for profile in profiles
    ):
        return "work"
    return "legacy"


def ensure_work_bridge(incus: Incus) -> None:
    """Create/verify the owned IPv4-only bridge and its default-deny baseline."""
    if incus.network_exists(WORK_BRIDGE):
        if incus.network_get(WORK_BRIDGE, "user.jailbee.owner") != _OWNER:
            raise ValueError("jailbee-work already exists but is not JailBee-owned")
        verify_work_bridge_config(incus)
        return
    create_owned_ipv4_only_work_bridge(incus)


def verify_work_bridge_config(incus: Incus) -> None:
    """Refuse an owned network whose managed bridge contract has drifted."""
    expected = {
        "ipv4.address": "auto",
        "ipv4.nat": "true",
        "ipv6.address": "none",
        "ipv6.nat": "false",
    }
    if incus.network_type(WORK_BRIDGE) != "bridge":
        raise ValueError("jailbee-work has incompatible network type (expected bridge)")
    for key, value in expected.items():
        actual = incus.network_get(WORK_BRIDGE, key)
        if value is not None and actual != value:
            raise ValueError(f"jailbee-work has incompatible {key}: {actual!r}")
    if incus.network_get(WORK_BRIDGE, "security.acls") != _BASELINE_ACL:
        raise ValueError("jailbee-work is missing its default-deny baseline ACL")
    _verify_baseline_acl(incus)


def create_owned_ipv4_only_work_bridge(incus: Incus) -> None:
    """Create the bridge and baseline atomically as far as Incus operations allow."""
    created = False
    acl_created = False
    try:
        incus.network_create(WORK_BRIDGE)
        created = True
        incus.network_set(WORK_BRIDGE, "ipv4.address", "auto")
        incus.network_set(WORK_BRIDGE, "ipv4.address", "auto")
        incus.network_set(WORK_BRIDGE, "ipv4.nat", "true")
        incus.network_set(WORK_BRIDGE, "ipv6.address", "none")
        incus.network_set(WORK_BRIDGE, "ipv6.nat", "false")
        incus.network_set(WORK_BRIDGE, "user.jailbee.owner", _OWNER)
        incus.network_acl_create(_BASELINE_ACL)
        acl_created = True
        incus.network_acl_set_yaml(_BASELINE_ACL, _baseline_acl_yaml())
        # Attach only after ACL creation/configuration succeeds.
        incus.network_set(WORK_BRIDGE, "security.acls", _BASELINE_ACL)
        verify_work_bridge_config(incus)
    except Exception:
        if created:
            try:
                incus.network_delete(WORK_BRIDGE)
            finally:
                if acl_created:
                    incus.network_acl_delete(_BASELINE_ACL)
        raise


def _baseline_acl_yaml() -> str:
    return yaml.safe_dump(
        {
            "name": _BASELINE_ACL,
            "description": "JailBee work bridge baseline default-deny",
            "egress": [
                {
                    "action": "allow",
                    "protocol": "udp",
                    "destination_port": "67",
                    "description": "DHCPv4 client to server",
                    "state": "enabled",
                },
                {
                    "action": "allow",
                    "protocol": "udp",
                    "destination_port": "53",
                    "description": "DNS over UDP",
                    "state": "enabled",
                },
                {
                    "action": "allow",
                    "protocol": "tcp",
                    "destination_port": "53",
                    "description": "DNS over TCP",
                    "state": "enabled",
                },
            ],
            "ingress": [
                {
                    "action": "allow",
                    "protocol": "udp",
                    "destination_port": "68",
                    "description": "DHCPv4 server to client",
                    "state": "enabled",
                }
            ],
        },
        sort_keys=False,
    )


def _verify_baseline_acl(incus: Incus) -> None:
    acl = yaml.safe_load(incus.network_acl_show(_BASELINE_ACL)) or {}
    egress = acl.get("egress") or []
    egress_policy = {
        (rule.get("action"), rule.get("protocol"), str(rule.get("destination_port")))
        for rule in egress
    }
    ingress = acl.get("ingress") or []
    ingress_policy = {
        (rule.get("action"), rule.get("protocol"), str(rule.get("destination_port")))
        for rule in ingress
    }
    if egress_policy != {
        ("allow", "udp", "67"),
        ("allow", "udp", "53"),
        ("allow", "tcp", "53"),
    } or ingress_policy != {("allow", "udp", "68")}:
        raise ValueError("jailbee-work baseline ACL is not default-deny DHCP/DNS-only")
