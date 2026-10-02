"""Squid egress proxy — an Incus container that filters outbound HTTP(S) by domain.

One ``jailbee-egress-proxy`` container sits on ``jailbee-loose`` (no bridge ACL,
so its own upstream traffic is unfiltered) and carries one extra NIC per client
bridge (``incusbr0`` and, when it exists, ``jailbee-work``). Clients reach it on
its address on their own bridge, port 3128. The client NICs get an address and
**no route**, so upstream traffic can only leave through ``eth0``.

The container is created and repaired only by ``proxy_up``; the rule-push
helpers never provision anything.
"""

from __future__ import annotations

import ipaddress
import time
from enum import StrEnum
from importlib import resources
from typing import TYPE_CHECKING, Any

import yaml

from jailbee.bridge_ipv4 import free_ipv4
from jailbee.egress_proxy_render import BASE_SQUID_CONF, PROXY_PORT
from jailbee.incus import Incus, IncusError
from jailbee.loose_bridge import loose_bridge_host_ip
from jailbee.network_generation import WORK_BRIDGE
from jailbee.services_acl import EGRESS_PROXY_LABEL, set_service
from jailbee.work_network import work_network_lock

if TYPE_CHECKING:
    from collections.abc import Callable

PROXY_CONTAINER = "jailbee-egress-proxy"
PROXY_PROFILE = "jailbee-egress-proxy-profile"
CLIENT_BRIDGES: tuple[str, ...] = ("incusbr0", WORK_BRIDGE)

_PROXY_BRIDGE = "jailbee-loose"
_PROXY_IMAGE = "images:ubuntu/26.04/cloud"
_SERVICE = "squid"
_SERVICE_WAIT_SECONDS = 60
_PROVISION_PKG = "jailbee.provision"
_PROVISION_SUBDIR = "egress-proxy"
_NETPLAN_PATH = "/etc/netplan/60-jailbee-egress-proxy.yaml"
_DEVICE_PREFIX = "cl-"


def _no_steps(_message: str) -> None:
    """Default `on_step`: report nowhere."""


class ProxyStatus(StrEnum):
    """Reported state of the proxy container and its Squid service."""

    RUNNING = "running"
    DEGRADED = "degraded"
    STOPPED = "stopped"
    MISSING = "missing"


def _service_state(incus: Incus) -> str:
    try:
        out = incus.exec(PROXY_CONTAINER, ["systemctl", "is-active", _SERVICE], timeout=10)
    except IncusError as e:
        return str(e)
    return out.strip()


def _container_present(incus: Incus) -> dict[str, Any] | None:
    for c in incus.list_containers():
        if c.get("name") == PROXY_CONTAINER:
            return c
    return None


def proxy_status(incus: Incus) -> ProxyStatus:
    """``running`` needs the container Running and ``squid`` active inside it."""
    info = _container_present(incus)
    if info is None:
        return ProxyStatus.MISSING
    if info.get("status") != "Running":
        return ProxyStatus.STOPPED
    if _service_state(incus) == "active":
        return ProxyStatus.RUNNING
    return ProxyStatus.DEGRADED


def _device_name(bridge: str) -> str:
    return _DEVICE_PREFIX + bridge.removeprefix("jailbee-")


def _nic_name(bridge: str) -> str:
    return f"eth{CLIENT_BRIDGES.index(bridge) + 1}"


def _client_devices(info: dict[str, Any] | None) -> dict[str, str]:
    """``bridge -> address`` of the client NICs in a raw ``incus list`` entry."""
    found: dict[str, str] = {}
    devices = (info or {}).get("devices") or {}
    for bridge in CLIENT_BRIDGES:
        device = devices.get(_device_name(bridge))
        if isinstance(device, dict) and isinstance(device.get("ipv4.address"), str):
            found[bridge] = device["ipv4.address"]
    return found


def client_endpoints(incus: Incus) -> dict[str, str]:
    """Bridge -> the proxy's address on that bridge."""
    return _client_devices(_container_present(incus))


def endpoint_for_bridge(incus: Incus, bridge: str) -> str | None:
    return client_endpoints(incus).get(bridge)


def _read_provision_text(filename: str) -> str:
    return (
        resources.files(_PROVISION_PKG).joinpath(_PROVISION_SUBDIR).joinpath(filename).read_text()
    )


def _ensure_profile(incus: Incus) -> None:
    if not incus.network_exists(_PROXY_BRIDGE):
        incus.network_create(_PROXY_BRIDGE)
    if not incus.profile_exists(PROXY_PROFILE):
        incus.profile_create(PROXY_PROFILE)
    eth0: dict[str, str] = {"type": "nic", "name": "eth0", "network": _PROXY_BRIDGE}
    address = loose_bridge_host_ip(incus, 2)
    if address is not None:
        eth0["ipv4.address"] = address
    profile = {
        "name": PROXY_PROFILE,
        "description": "network for the jailbee-egress-proxy container",
        "config": {},
        "devices": {"eth0": eth0},
    }
    incus.profile_set_yaml(PROXY_PROFILE, yaml.safe_dump(profile, sort_keys=False))


def _create(incus: Incus) -> None:
    incus.init(_PROXY_IMAGE, PROXY_CONTAINER)
    incus.profile_assign(PROXY_CONTAINER, ["default", PROXY_PROFILE])
    incus.config_set(PROXY_CONTAINER, "boot.autostart", "true")
    incus.start(PROXY_CONTAINER)


def _ensure_client_nics(incus: Incus) -> dict[str, str]:
    """Add any missing client NIC; return ``bridge -> address`` for all present."""
    have = _client_devices(_container_present(incus))
    for bridge in CLIENT_BRIDGES:
        if bridge in have or not incus.network_exists(bridge):
            continue
        props = {"type": "nic", "network": bridge, "name": _nic_name(bridge)}
        if bridge == WORK_BRIDGE:
            with work_network_lock():
                have[bridge] = _add_nic(incus, bridge, props)
        else:
            have[bridge] = _add_nic(incus, bridge, props)
    return have


def _add_nic(incus: Incus, bridge: str, props: dict[str, str]) -> str:
    address = free_ipv4(incus, bridge)
    incus.config_device_add(
        PROXY_CONTAINER,
        _device_name(bridge),
        "nic",
        {**props, "ipv4.address": address, "security.ipv4_filtering": "true"},
    )
    return address


def _write_netplan(incus: Incus, addresses: dict[str, str]) -> None:
    """Static, route-less client NICs; only ``eth0`` (dhcp4) gets a default route."""
    ethernets: dict[str, Any] = {"eth0": {"dhcp4": True}}
    for bridge, address in addresses.items():
        prefix = ipaddress.IPv4Interface(
            incus.network_get(bridge, "ipv4.address")
        ).network.prefixlen
        ethernets[_nic_name(bridge)] = {"dhcp4": False, "addresses": [f"{address}/{prefix}"]}
    body = yaml.safe_dump({"network": {"version": 2, "ethernets": ethernets}}, sort_keys=False)
    script = f"""\
set -euo pipefail
cat > {_NETPLAN_PATH} <<'JAILBEE_NETPLAN_EOF'
{body.rstrip()}
JAILBEE_NETPLAN_EOF
chmod 0600 {_NETPLAN_PATH}
netplan apply
"""
    incus.exec_with_input(PROXY_CONTAINER, ["bash", "-s"], script, timeout=120)


def _provision(incus: Incus) -> None:
    install_body = _read_provision_text("install.sh")
    script = f"""\
{install_body.rstrip()}
cat > /etc/squid/squid.conf <<'JAILBEE_SQUID_EOF'
{BASE_SQUID_CONF.rstrip()}
JAILBEE_SQUID_EOF
squid -k parse
systemctl enable --now squid
systemctl restart squid
"""
    incus.exec_with_input(PROXY_CONTAINER, ["bash", "-s"], script, timeout=600)


def _squid_installed(incus: Incus) -> bool:
    try:
        incus.exec(PROXY_CONTAINER, ["test", "-x", "/usr/sbin/squid"], timeout=15)
    except IncusError:
        return False
    return True


def _wait_for_service(incus: Incus, on_step: Callable[[str], None]) -> str | None:
    deadline = time.monotonic() + _SERVICE_WAIT_SECONDS
    while True:
        state = _service_state(incus)
        if state == "active":
            return None
        now = time.monotonic()
        if now >= deadline:
            return f"{_SERVICE} did not become active within {_SERVICE_WAIT_SECONDS}s ({state})"
        on_step(f"waiting for {_SERVICE} - {state}, {int(deadline - now)}s left")
        time.sleep(2)


def _ensure_service(incus: Incus, on_step: Callable[[str], None]) -> None:
    """Wait for squid; on failure reinstall once, then raise."""
    reason = _wait_for_service(incus, on_step)
    if reason is None:
        return
    on_step("squid did not come up; reinstalling it once")
    try:
        _provision(incus)
    except IncusError as e:
        reason = f"{reason}; reinstalling failed: {e}"
    else:
        second = _wait_for_service(incus, on_step)
        if second is None:
            return
        reason = f"reinstalled once; {second}"
    raise RuntimeError(
        f"{reason}. Run `jailbee apply` to retry, or delete {PROXY_CONTAINER} and apply again."
    )


def proxy_up(incus: Incus, *, on_step: Callable[[str], None] = _no_steps) -> None:
    """Bring the proxy container up. Idempotent; repairs in place."""
    on_step("preparing the proxy profile")
    _ensure_profile(incus)

    info = _container_present(incus)
    if info is None:
        on_step(f"creating {PROXY_CONTAINER} from {_PROXY_IMAGE} (first run downloads it)")
        _create(incus)
    elif info.get("status") != "Running":
        on_step(f"starting {PROXY_CONTAINER}")
        incus.start(PROXY_CONTAINER)

    on_step("attaching the client networks")
    addresses = _ensure_client_nics(incus)
    _write_netplan(incus, addresses)

    if not _squid_installed(incus):
        on_step("installing squid in the container (apt, up to 10 min)")
        _provision(incus)

    _ensure_service(incus, on_step)
    set_service(incus, EGRESS_PROXY_LABEL, (sorted(client_endpoints(incus).values()), [PROXY_PORT]))
