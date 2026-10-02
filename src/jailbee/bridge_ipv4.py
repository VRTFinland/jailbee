"""IPv4 occupancy and allocation on a managed Incus bridge."""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jailbee.incus import Incus


def _bridge_interface(incus: Incus, bridge: str) -> ipaddress.IPv4Interface:
    raw_cidr = incus.network_get(bridge, "ipv4.address")
    try:
        interface = ipaddress.ip_interface(raw_cidr)
    except ValueError as exc:
        raise ValueError(f"{bridge} has no valid effective IPv4 CIDR: {raw_cidr!r}") from exc
    if not isinstance(interface, ipaddress.IPv4Interface):
        raise ValueError(f"{bridge} has no valid effective IPv4 CIDR: {raw_cidr!r}")
    return interface


def occupied_ipv4(
    incus: Incus, bridge: str, containers: list[dict[str, object]]
) -> set[ipaddress.IPv4Address]:
    """Return every IPv4 address in use on ``bridge``.

    Covers the bridge's own address, NIC reservations of any container (local
    and expanded devices) attached to the bridge, and the bridge's DHCP leases.
    """
    occupied: set[ipaddress.IPv4Address] = {_bridge_interface(incus, bridge).ip}
    for container in containers:
        for key in ("devices", "expanded_devices"):
            device_map = container.get(key) or {}
            if not isinstance(device_map, dict):
                continue
            for device in device_map.values():
                if not isinstance(device, dict) or device.get("network") != bridge:
                    continue
                address = device.get("ipv4.address")
                if not isinstance(address, str) or not address:
                    continue
                try:
                    occupied.add(ipaddress.IPv4Address(address))
                except ipaddress.AddressValueError:
                    continue
    for lease in incus.network_leases(bridge):
        address = lease.get("address")
        if isinstance(address, str):
            try:
                occupied.add(ipaddress.IPv4Address(address))
            except ipaddress.AddressValueError:
                continue
    return occupied


def free_ipv4(incus: Incus, bridge: str) -> str:
    """Return the first unoccupied host address of the bridge subnet."""
    interface = _bridge_interface(incus, bridge)
    occupied = occupied_ipv4(incus, bridge, incus.list_containers())
    for address in interface.network.hosts():
        if address not in occupied:
            return str(address)
    raise ValueError(f"No free IPv4 addresses remain on {bridge}; expand its subnet")
