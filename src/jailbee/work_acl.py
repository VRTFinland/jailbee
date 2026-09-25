"""Source-scoped ACLs for the shared JailBee work bridge.

Callers must hold ``work_network_lock`` for the complete bridge/NIC/marker
transaction. These helpers deliberately do not acquire that lock.
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING, Any

import yaml

from jailbee.egress_scope import extra_acl_name
from jailbee.network import acl_name, extra_acl_yaml, work_loose_rule
from jailbee.network_generation import WORK_BRIDGE

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus


def _loose_acl_name(cfg: Config) -> str:
    return f"{cfg.container_prefix}-work-loose"


def _work_extras_acl_name(cfg: Config) -> str:
    from jailbee.egress_scope import _capped_acl_name

    return _capped_acl_name(cfg.container_prefix, "-work-container-extras")


def _attached(incus: Incus) -> list[str]:
    raw = incus.network_get(WORK_BRIDGE, "security.acls")
    return list(dict.fromkeys(name.strip() for name in raw.split(",") if name.strip()))


def _write_attached(incus: Incus, names: list[str]) -> None:
    current = _attached(incus)
    updated = list(dict.fromkeys(["jailbee-work-baseline", *names]))
    if current != updated:
        incus.network_set(WORK_BRIDGE, "security.acls", ",".join(updated))


def _work_occupants(incus: Incus) -> list[dict[str, Any]]:
    occupants: list[dict[str, Any]] = []
    seen: dict[ipaddress.IPv4Address, str] = {}
    for raw in incus.list_containers():
        devices: dict[str, Any] = {}
        for device_map in (raw.get("devices") or {}, raw.get("expanded_devices") or {}):
            devices.update(device_map)
        work_devices = [
            (device_name, device)
            for device_name, device in devices.items()
            if isinstance(device, dict) and device.get("network") == WORK_BRIDGE
        ]
        if not work_devices:
            continue
        name = raw.get("name")
        if not isinstance(name, str):
            raise ValueError(f"Unexpected instance on {WORK_BRIDGE}: {name!r}")
        profiles = raw.get("profiles") or []
        markers = [
            p
            for p in profiles
            if isinstance(p, str) and p.endswith(("-net-work-strict", "-net-work-loose"))
        ]
        if len(markers) != 1:
            raise ValueError(
                f"Unexpected instance on {WORK_BRIDGE}: {name} has conflicting or "
                "missing work markers"
            )
        local_devices = raw.get("devices") or {}
        local_eth0 = local_devices.get("eth0") if isinstance(local_devices, dict) else None
        if not isinstance(local_eth0, dict) or local_eth0.get("network") != WORK_BRIDGE:
            raise ValueError(f"{name} has no authoritative local eth0 work NIC")
        if local_eth0.get("security.ipv4_filtering") != "true":
            raise ValueError(f"{name} work NIC requires security.ipv4_filtering=true")
        if not isinstance(local_eth0.get("ipv4.address"), str):
            raise ValueError(f"{name} work NIC has no reserved IPv4 address")
        eth0 = next((device for device_name, device in work_devices if device_name == "eth0"), None)
        if eth0 is None:
            raise ValueError(f"{name} has incompatible work NIC configuration (missing eth0)")
        if any(
            local_eth0.get(key) != eth0.get(key)
            for key in ("network", "ipv4.address", "security.ipv4_filtering")
        ):
            raise ValueError(f"{name} local and effective work eth0 configuration disagree")
        if eth0.get("type") != "nic":
            raise ValueError(f"{name} work eth0 is not a NIC")
        if eth0.get("security.ipv4_filtering") != "true":
            raise ValueError(f"{name} work NIC requires security.ipv4_filtering=true")
        address = eth0.get("ipv4.address")
        try:
            parsed = ipaddress.IPv4Address(address)
        except (ipaddress.AddressValueError, TypeError) as exc:
            raise ValueError(f"{name} has malformed work NIC IPv4 address: {address!r}") from exc
        if parsed in seen:
            raise ValueError(f"Duplicate work NIC IPv4 address {parsed}: {seen[parsed]} and {name}")
        seen[parsed] = name
        occupants.append({"name": name, "ip": str(parsed), "profiles": profiles, "device": eth0})
    return occupants


def _set_loose_acl(
    cfg: Config, incus: Incus, occupants: list[dict[str, Any]], include: str | None = None
) -> None:
    prefix = f"{cfg.container_prefix}-"
    repo = [item for item in occupants if item["name"].startswith(prefix)]
    loose = [
        item
        for item in repo
        if item["name"] == include
        or any(isinstance(p, str) and p.endswith("-net-work-loose") for p in item["profiles"])
    ]
    name = _loose_acl_name(cfg)
    if not loose:
        attached = [acl for acl in _attached(incus) if acl != name]
        _write_attached(incus, attached)
        if incus.network_acl_exists(name):
            incus.network_acl_delete(name)
        return
    acl = {
        "name": name,
        "description": "JailBee source-scoped loose work egress",
        "egress": [work_loose_rule(item["ip"]) for item in loose],
        "ingress": [],
    }
    if not incus.network_acl_exists(name):
        incus.network_acl_create(name)
    incus.network_acl_set_yaml(name, yaml.safe_dump(acl, sort_keys=False))
    attached = _attached(incus)
    if name not in attached:
        _write_attached(incus, [*attached, name])


def ensure_work_repo_acl(cfg: Config, incus: Incus) -> None:
    """Attach the repo's ordinary allowlist and live extras union to work bridge."""
    occupants = _work_occupants(incus)
    if not incus.network_acl_exists(acl_name(cfg)):
        raise ValueError(f"Missing repo allowlist ACL {acl_name(cfg)}")
    attached = _attached(incus)
    additions = [acl_name(cfg)]
    extra_names = sorted(
        {
            extra_acl_name(item["name"])
            for item in occupants
            if item["name"].startswith(f"{cfg.container_prefix}-")
        }
    )
    from jailbee.egress import EgressEntry
    from jailbee.network import entries_from_acl_yaml

    merged: dict[tuple[str, int | None], EgressEntry] = {}
    for extra in extra_names:
        if not incus.network_acl_exists(extra):
            continue
        payload = incus.network_acl_show(extra)
        if isinstance(payload, str):
            for entry in entries_from_acl_yaml(payload):
                key = (entry.description, entry.port)
                prior = merged.get(key)
                merged[key] = (
                    entry
                    if prior is None
                    else EgressEntry(
                        destinations=list(
                            dict.fromkeys([*prior.destinations, *entry.destinations])
                        ),
                        port=entry.port,
                        description=entry.description,
                    )
                )
    union_name = _work_extras_acl_name(cfg)
    if merged:
        if not incus.network_acl_exists(union_name):
            incus.network_acl_create(union_name)
        from jailbee.network import BRIDGE_EXTRAS_ACL_DESC

        incus.network_acl_set_yaml(
            union_name,
            extra_acl_yaml(union_name, list(merged.values()), description=BRIDGE_EXTRAS_ACL_DESC),
        )
        additions.append(union_name)
    else:
        additions = [acl for acl in additions if acl != union_name]
        if union_name in attached:
            attached = [acl for acl in attached if acl != union_name]
            _write_attached(incus, attached)
        if incus.network_acl_exists(union_name):
            incus.network_acl_delete(union_name)
    additions.extend(acl for acl in attached if acl not in additions)
    _write_attached(incus, additions)


def apply_work_container_acl(
    cfg: Config, incus: Incus, name: str, *, mode: str | None = None
) -> None:
    """Rebuild one work NIC's private extra ACL without replacing its NIC."""
    from jailbee.egress_scope import (
        _resolve_entries_tolerant,
        container_extras,
        extra_acl_name,
    )
    from jailbee.network import acl_name, extra_acl_yaml

    raw = next((c for c in incus.list_containers() if c.get("name") == name), None)
    if raw is None:
        raise ValueError(f"Container '{name}' not found")
    verified = next((item for item in _work_occupants(incus) if item["name"] == name), None)
    if verified is None:
        raise ValueError(f"{name} has no verified work NIC")
    devices: dict[str, Any] = {}
    for mapping in (raw.get("devices") or {}, raw.get("expanded_devices") or {}):
        devices.update(mapping)
    nic = devices.get("eth0")
    if not isinstance(nic, dict) or nic.get("network") != WORK_BRIDGE:
        raise ValueError(f"{name} has no verified work eth0")
    if nic.get("security.ipv4_filtering") != "true" or not nic.get("ipv4.address"):
        raise ValueError(f"{name} work eth0 is not filtered and reserved")
    profiles = raw.get("profiles") or []
    markers = [
        p
        for p in profiles
        if isinstance(p, str) and p.endswith(("-net-work-strict", "-net-work-loose"))
    ]
    if len(markers) != 1:
        raise ValueError(f"{name} has conflicting or missing work mode markers")
    marker_loose = any(isinstance(p, str) and p.endswith("-net-work-loose") for p in profiles)
    loose = mode == "loose" if mode is not None else marker_loose
    extras_name = extra_acl_name(name)
    raw_extras = container_extras(incus, name)
    entries = _resolve_entries_tolerant(name, raw_extras)
    if not loose and raw_extras and entries:
        if not incus.network_acl_exists(extras_name):
            incus.network_acl_create(extras_name)
        incus.network_acl_set_yaml(extras_name, extra_acl_yaml(extras_name, entries))
    keep_extra_acl = bool(raw_extras) and (bool(entries) or incus.network_acl_exists(extras_name))
    acls = [] if loose else [acl_name(cfg), *([extras_name] if keep_extra_acl else [])]
    local_devices = raw.get("devices") or {}
    local_nic = local_devices.get("eth0")
    if not isinstance(local_nic, dict) or local_nic.get("network") != WORK_BRIDGE:
        raise ValueError(f"{name} has no authoritative local eth0 work NIC")
    desired = dict(local_nic)
    desired["security.acls"] = ",".join(acls)
    if local_nic != desired:
        incus.config_device_set(name, "eth0", desired)
    if not raw_extras and incus.network_acl_exists(extras_name):
        incus.network_acl_delete(extras_name)
    ensure_work_repo_acl(cfg, incus)


def grant_work_loose(cfg: Config, incus: Incus, name: str) -> None:
    """Prepare the verified source exception before a strict-to-loose switch."""
    occupants = _work_occupants(incus)
    target = next((item for item in occupants if item["name"] == name), None)
    if target is None:
        raise ValueError(f"{name} has no verified NIC on {WORK_BRIDGE}")
    if not name.startswith(f"{cfg.container_prefix}-"):
        raise ValueError(f"{name} does not belong to repo {cfg.container_prefix}")
    if not incus.network_acl_exists(acl_name(cfg)):
        raise ValueError(f"Missing repo allowlist ACL {acl_name(cfg)}")
    _set_loose_acl(cfg, incus, occupants, include=name)


def revoke_work_loose(cfg: Config, incus: Incus, name: str) -> None:
    """Remove source exceptions no longer backed by durable loose markers."""
    if not name.startswith(f"{cfg.container_prefix}-"):
        raise ValueError(f"{name} does not belong to repo {cfg.container_prefix}")
    _set_loose_acl(cfg, incus, _work_occupants(incus))


def reconcile_work_acl(cfg: Config, incus: Incus) -> None:
    """Rebuild this repo's source exceptions from actual marker profiles."""
    _set_loose_acl(cfg, incus, _work_occupants(incus))


def work_loose_policy_matches(cfg: Config, incus: Incus) -> bool:
    """Read-only comparison of this repo's durable loose sources and ACL state."""
    from jailbee.work_network import work_network_lock

    with work_network_lock():
        return _work_loose_policy_matches_unlocked(cfg, incus)


def _work_loose_policy_matches_unlocked(cfg: Config, incus: Incus) -> bool:
    """Compare policy while the caller holds the work network lock."""
    from jailbee.network import work_loose_rule

    occupants = _work_occupants(incus)
    prefix = f"{cfg.container_prefix}-"
    expected = sorted(
        item["ip"]
        for item in occupants
        if item["name"].startswith(prefix)
        and any(
            isinstance(profile, str) and profile.endswith("-net-work-loose")
            for profile in item["profiles"]
        )
    )
    name = _loose_acl_name(cfg)
    attached = name in _attached(incus)
    if not expected:
        return not attached and not incus.network_acl_exists(name)
    if not attached or not incus.network_acl_exists(name):
        return False
    payload = incus.network_acl_show(name)
    if not isinstance(payload, str):
        return False
    acl = yaml.safe_load(payload) or {}
    expected_rules = sorted(
        (work_loose_rule(address) for address in expected), key=lambda rule: rule["source"]
    )
    actual_rules = acl.get("egress")
    if not isinstance(actual_rules, list) or not all(
        isinstance(rule, dict) for rule in actual_rules
    ):
        return False
    actual_rules = sorted(actual_rules, key=lambda rule: str(rule.get("source", "")))
    return (
        acl.get("name") == name
        and actual_rules == expected_rules
        and acl.get("ingress") in (None, [])
    )
