"""Live strict/loose transitions for stable work-network instances."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from jailbee.work_network import verify_work_nic, work_network_lock

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus


def work_mode_state(
    cfg: Config, raw: dict[str, Any], incus: Incus | None = None
) -> tuple[str | None, bool]:
    """Return conservative mode and whether marker and authoritative NIC agree."""
    profiles = raw.get("profiles") or []
    marker = next(
        (
            profile.rsplit("-", 1)[-1]
            for profile in profiles
            if isinstance(profile, str)
            and profile.endswith(("-net-work-strict", "-net-work-loose"))
        ),
        None,
    )
    devices = raw.get("devices") or {}
    expanded = raw.get("expanded_devices") or {}
    effective_devices = {**expanded, **devices}
    nic = effective_devices.get("eth0") if isinstance(effective_devices, dict) else None
    valid_nic = (
        isinstance(nic, dict)
        and nic.get("type") == "nic"
        and nic.get("network") == "jailbee-work"
        and nic.get("security.ipv4_filtering") == "true"
        and isinstance(nic.get("ipv4.address"), str)
    )
    acls = nic.get("security.acls", "") if isinstance(nic, dict) else ""
    from jailbee.network import acl_name

    nic_mode = "strict" if acl_name(cfg) in str(acls).split(",") else "loose"
    if marker not in ("strict", "loose"):
        return None, False
    if not valid_nic:
        return "strict", False
    if incus is not None:
        from jailbee.work_acl import work_loose_policy_matches

        try:
            if not work_loose_policy_matches(cfg, incus):
                return "strict", False
        except Exception:
            return "strict", False
    return (marker, True) if marker == nic_mode else ("strict", False)


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
            work_acl.apply_work_container_acl(cfg, incus, name, mode="strict")
            incus.profile_assign(name, profiles)
            work_acl.revoke_work_loose(cfg, incus, name)

    from jailbee.hosts import apply_hosts, clear_hosts

    if mode == "strict":
        apply_hosts(cfg, incus, name, mirror_endpoint=mirror_endpoint)
    else:
        clear_hosts(cfg, incus, name)
