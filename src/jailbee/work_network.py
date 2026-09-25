"""Host-wide stable IPv4 reservations for JailBee work containers."""

from __future__ import annotations

import fcntl
import ipaddress
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from jailbee.db import state_dir
from jailbee.network_generation import WORK_BRIDGE

if TYPE_CHECKING:
    from jailbee.incus import Incus


@contextmanager
def work_network_lock() -> Iterator[None]:
    """Lock allocation and shared work-network operations across processes."""
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "work-network.lock").open("a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def reserve_work_ipv4(incus: Incus, name: str) -> str:
    """Return an existing verified reservation or allocate a free host address.

    The caller must hold ``work_network_lock`` through instance creation; this
    function intentionally does not acquire it recursively.
    """
    raw_cidr = incus.network_get(WORK_BRIDGE, "ipv4.address")
    try:
        interface = ipaddress.ip_interface(raw_cidr)
    except ValueError as exc:
        raise ValueError(f"{WORK_BRIDGE} has no valid effective IPv4 CIDR: {raw_cidr!r}") from exc
    if not isinstance(interface, ipaddress.IPv4Interface):
        raise ValueError(f"{WORK_BRIDGE} has no valid effective IPv4 CIDR: {raw_cidr!r}")

    containers = incus.list_containers()
    occupied: set[ipaddress.IPv4Address] = {interface.ip}
    existing: str | None = None
    for container in containers:
        devices = container.get("devices") or container.get("expanded_devices") or {}
        for device in devices.values():
            if not isinstance(device, dict) or device.get("network") != WORK_BRIDGE:
                continue
            address = device.get("ipv4.address")
            if not isinstance(address, str) or not address:
                continue
            try:
                parsed = ipaddress.IPv4Address(address)
            except ipaddress.AddressValueError:
                continue
            occupied.add(parsed)
            if container.get("name") == name:
                if device.get("security.ipv4_filtering") != "true":
                    raise ValueError(f"{name} has a work NIC reservation without IPv4 filtering")
                if (
                    parsed not in interface.network
                    or parsed == interface.ip
                    or parsed
                    in {interface.network.network_address, interface.network.broadcast_address}
                ):
                    raise ValueError(
                        f"{name} has a work NIC address outside the bridge subnet: {parsed}"
                    )
                existing = str(parsed)
    if existing is not None:
        return existing

    for lease in incus.network_leases(WORK_BRIDGE):
        address = lease.get("address")
        if isinstance(address, str):
            try:
                occupied.add(ipaddress.IPv4Address(address))
            except ipaddress.AddressValueError:
                pass

    for address in interface.network.hosts():
        if address not in occupied:
            return str(address)
    raise ValueError(f"No free IPv4 addresses remain on {WORK_BRIDGE}; expand its subnet")


def work_nic(ip: str, acl_names: list[str]) -> dict[str, str]:
    """Build the authoritative local eth0 device for a work container."""
    result = {
        "type": "nic",
        "network": WORK_BRIDGE,
        "ipv4.address": ip,
        "security.ipv4_filtering": "true",
    }
    if acl_names:
        result["security.acls"] = ",".join(acl_names)
    return result


def verify_work_nic(incus: Incus, name: str, ip: str) -> None:
    """Verify the authoritative local and expanded eth0 before starting."""
    raw = next((item for item in incus.list_containers() if item.get("name") == name), None)
    if raw is None:
        raise ValueError(f"{name} is missing after work NIC configuration")
    local_devices = raw.get("devices") or {}
    expanded_devices = raw.get("expanded_devices") or {}
    local_eth0 = local_devices.get("eth0")
    expanded_eth0 = expanded_devices.get("eth0") if expanded_devices else local_eth0
    if not isinstance(local_eth0, dict) or local_eth0.get("type") != "nic":
        raise ValueError(f"{name} has no authoritative local eth0 work NIC")
    for eth0, scope in ((local_eth0, "local"), (expanded_eth0, "effective")):
        if not isinstance(eth0, dict) or eth0.get("type") != "nic":
            raise ValueError(f"{name} has no {scope} eth0 work NIC")
        if eth0.get("network") != WORK_BRIDGE or eth0.get("ipv4.address") != ip:
            raise ValueError(f"{name} has incompatible {scope} work NIC bridge or IPv4 reservation")
        if eth0.get("security.ipv4_filtering") != "true":
            raise ValueError(f"{name} {scope} work NIC requires security.ipv4_filtering=true")
