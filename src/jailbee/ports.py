"""Port forwarding between a container and its host, via Incus proxy devices.

Every forward is one ``proxy`` device. ``bind=instance`` puts the listener
inside the container and connects on the host — a host service becomes
reachable in the container (``to-container``, the adb case). ``bind=host`` is
the mirror image: the host listens and Incus connects inside the container, so
a container service becomes reachable on the host (``to-host``).

Note that the *availability* direction and the TCP *connection* direction are
opposite in both cases, which is why the vocabulary names the side a service
becomes available on rather than which end opens the socket.

Device names encode where a forward came from, so reconciliation can replace
config-declared forwards without ever touching one a user made by hand:

    port-cfg-<name>          declared in `host_ports`
    port-tc-<proto>-<port>   ad hoc `jailbee port to-container`
    port-th-<proto>-<port>   ad hoc `jailbee port to-host`

`nat` is never set: instance-bound proxies are refused outright by Incus, and
host-bound ones require the connect address to be one of the instance's static
IPs. `uid`/`gid`/`mode` apply only to unix-socket listeners, which the config
schema deliberately does not expose.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from jailbee.config import HostPort

CONFIG_PREFIX = "port-cfg-"
ADHOC_TO_CONTAINER_PREFIX = "port-tc-"
ADHOC_TO_HOST_PREFIX = "port-th-"

Direction = Literal["to-container", "to-host"]
Source = Literal["config", "ad-hoc", "other"]

# Incus stores whichever spelling it was given. `instance` is its own name;
# `container` and `guest` are accepted aliases (verified on 6.0.5), so a
# device someone added by hand may carry either.
_INSTANCE_BINDS = frozenset({"instance", "container", "guest"})


@dataclass(frozen=True)
class Endpoint:
    """One side of a forward, as Incus stores it."""

    proto: str
    address: str
    port: int | None
    raw: str

    @property
    def display(self) -> str:
        """The endpoint without its protocol prefix, for table cells."""
        return self.raw.split(":", 1)[1] if ":" in self.raw else self.raw


@dataclass(frozen=True)
class Forward:
    """One port forward as JailBee describes it."""

    device: str
    direction: Direction
    proto: str
    container: Endpoint
    host: Endpoint
    source: Source


def _addr(address: str) -> str:
    """Bracket an IPv6 literal; leave anything else alone."""
    return f"[{address}]" if ":" in address else address


def config_device_name(entry_name: str) -> str:
    """Incus device name for a `host_ports` entry."""
    return f"{CONFIG_PREFIX}{entry_name}"


def adhoc_device_name(direction: Direction, proto: str, container_port: int) -> str:
    """Deterministic device name for an ad-hoc forward.

    Derived from the container-side port so `jailbee port rm 5037` can resolve
    it without the user learning device names. Addresses are deliberately not
    part of the name: two ad-hoc forwards differing only by address collide
    here, and that case belongs in `host_ports`, where `name` disambiguates.
    """
    prefix = ADHOC_TO_CONTAINER_PREFIX if direction == "to-container" else ADHOC_TO_HOST_PREFIX
    return f"{prefix}{proto}-{container_port}"


def render_device(
    direction: Direction,
    *,
    proto: str,
    container_port: int,
    host_port: int,
    container_address: str,
    host_address: str,
) -> dict[str, str]:
    """Proxy-device properties for one forward."""
    container_ep = f"{proto}:{_addr(container_address)}:{container_port}"
    host_ep = f"{proto}:{_addr(host_address)}:{host_port}"
    if direction == "to-container":
        return {"listen": container_ep, "connect": host_ep, "bind": "instance"}
    return {"listen": host_ep, "connect": container_ep, "bind": "host"}


def entry_device(entry: HostPort) -> tuple[str, dict[str, str]]:
    """(device name, properties) for a `host_ports` entry."""
    return config_device_name(entry.name), render_device(
        "to-container",
        proto=entry.proto,
        container_port=entry.port,
        host_port=entry.effective_host_port,
        container_address=entry.container_address,
        host_address=entry.host_address,
    )


def _source_of(device: str) -> Source:
    if device.startswith(CONFIG_PREFIX):
        return "config"
    if device.startswith((ADHOC_TO_CONTAINER_PREFIX, ADHOC_TO_HOST_PREFIX)):
        return "ad-hoc"
    return "other"


def _parse_endpoint(raw: str) -> Endpoint | None:
    """Parse ``tcp:127.0.0.1:5037`` / ``udp:[fd00::1]:53`` / ``unix:/p``.

    A port range (``6000-6002``) parses with ``port=None``: it is legal in
    Incus and must still be listable, it just cannot be a `rm`-by-number
    target.
    """
    if ":" not in raw:
        return None
    proto, rest = raw.split(":", 1)
    if proto == "unix":
        return Endpoint(proto=proto, address=rest, port=None, raw=raw)
    if ":" not in rest:
        return None
    address, port_token = rest.rsplit(":", 1)
    address = address.removeprefix("[").removesuffix("]")
    port = int(port_token) if port_token.isdigit() else None
    return Endpoint(proto=proto, address=address, port=port, raw=raw)


def parse_device(device: str, props: Mapping[str, object]) -> Forward | None:
    """Read one device entry back as a `Forward`, or None if it isn't one.

    Returns None for non-proxy devices and for proxy devices whose endpoints
    cannot be parsed at all — the caller lists what it can and stays quiet
    about the rest rather than crashing on a shape we did not write.
    """
    if props.get("type") != "proxy":
        return None
    listen = _parse_endpoint(str(props.get("listen", "")))
    connect = _parse_endpoint(str(props.get("connect", "")))
    if listen is None or connect is None:
        return None
    bind = str(props.get("bind", "host"))
    if bind in _INSTANCE_BINDS:
        direction: Direction = "to-container"
        container, host = listen, connect
    else:
        direction = "to-host"
        container, host = connect, listen
    return Forward(
        device=device,
        direction=direction,
        proto=container.proto,
        container=container,
        host=host,
        source=_source_of(device),
    )
