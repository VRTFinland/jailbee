"""Live strict/loose transitions for stable work-network instances."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from jailbee.profiles import profile_names
from jailbee.work_network import verify_work_nic, work_network_lock

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus


def switch_work_network(
    cfg: Config,
    incus: Incus,
    name: str,
    mode: Literal["strict", "loose"],
    *,
    mirror_endpoint: tuple[str, int] | None = None,
) -> None:
    """Switch policy on the existing work NIC, preserving its bridge and IP."""
    raw = next((item for item in incus.list_containers() if item.get("name") == name), None)
    if raw is None:
        raise ValueError(f"Container '{name}' not found")
    devices = raw.get("devices") or {}
    nic = devices.get("eth0")
    if not isinstance(nic, dict) or nic.get("network") != "jailbee-work":
        raise ValueError(f"{name} has no authoritative work eth0 NIC")
    ip = nic.get("ipv4.address")
    if not isinstance(ip, str):
        raise ValueError(f"{name} has no reserved work IPv4 address")
    verify_work_nic(incus, name, ip)

    from jailbee import work_acl

    from jailbee.network import acl_name

    strict_acl = acl_name(cfg)
    target = f"{cfg.container_prefix}-net-work-{mode}"
    marker_profiles = {
        f"{cfg.container_prefix}-net-work-strict",
        f"{cfg.container_prefix}-net-work-loose",
    }
    profiles = [p for p in (raw.get("profiles") or []) if p not in marker_profiles]
    profiles.append(target)
    with work_network_lock():
        if mode == "loose":
            work_acl.grant_work_loose(cfg, incus, name)
            incus.config_device_set(name, "eth0", {"security.acls": ""})
            try:
                incus.profile_assign(name, profiles)
            except Exception:
                # The strict marker remains authoritative until profile assign
                # succeeds; restore its enforcement before surfacing failure.
                incus.config_device_set(name, "eth0", {"security.acls": strict_acl})
                raise
            work_acl.revoke_work_loose(cfg, incus, name)
        else:
            incus.config_device_set(name, "eth0", {"security.acls": strict_acl})
            incus.profile_assign(name, profiles)
            work_acl.revoke_work_loose(cfg, incus, name)

    from jailbee.hosts import apply_hosts, clear_hosts

    if mode == "strict":
        apply_hosts(cfg, incus, name, mirror_endpoint=mirror_endpoint)
    else:
        clear_hosts(cfg, incus, name)
