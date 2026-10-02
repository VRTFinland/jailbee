from __future__ import annotations

from ipaddress import IPv4Address
from typing import Any

import pytest

from jailbee.bridge_ipv4 import free_ipv4, occupied_ipv4

BRIDGE = "jb-test"


def _incus(mocker: Any, cidr: str, leases: list[dict[str, str]]) -> Any:
    incus = mocker.MagicMock()
    incus.network_get.return_value = cidr
    incus.network_leases.return_value = leases
    return incus


def _containers() -> list[dict[str, Any]]:
    return [
        {
            "name": "a",
            "devices": {"eth0": {"type": "nic", "network": BRIDGE, "ipv4.address": "10.5.0.2"}},
            "expanded_devices": {},
        },
        {
            "name": "b",
            "devices": {"eth0": {"type": "nic", "network": "other", "ipv4.address": "10.5.0.3"}},
            "expanded_devices": {},
        },
    ]


def test_occupied_ipv4_collects_bridge_devices_and_leases(mocker: Any) -> None:
    incus = _incus(mocker, "10.5.0.1/24", [{"address": "10.5.0.4"}])

    result = occupied_ipv4(incus, BRIDGE, _containers())

    assert result == {IPv4Address("10.5.0.1"), IPv4Address("10.5.0.2"), IPv4Address("10.5.0.4")}


def test_free_ipv4_returns_first_unoccupied_host(mocker: Any) -> None:
    incus = _incus(mocker, "10.5.0.1/24", [{"address": "10.5.0.4"}])
    incus.list_containers.return_value = _containers()

    assert free_ipv4(incus, BRIDGE) == "10.5.0.3"


def test_free_ipv4_raises_when_subnet_full(mocker: Any) -> None:
    incus = _incus(mocker, "10.5.0.1/30", [{"address": "10.5.0.2"}])
    incus.list_containers.return_value = []

    with pytest.raises(ValueError, match="No free IPv4 addresses remain on jb-test"):
        free_ipv4(incus, BRIDGE)
