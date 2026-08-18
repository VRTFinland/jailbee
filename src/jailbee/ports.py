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

import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from jailbee.config import HostPort
from jailbee.incus import Incus, IncusError

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


class PortError(Exception):
    """A port-forwarding failure with a message meant for the user."""


def list_forwards(incus: Incus, names: Sequence[str]) -> dict[str, list[Forward]]:
    """Forwards per container, for the given container names.

    One `incus list` call for the whole set. Containers with no proxy device
    map to an empty list, so a caller can render "none" without a second
    lookup. Names that Incus does not report are omitted.
    """
    wanted = set(names)
    by_container: dict[str, list[Forward]] = {}
    for raw in incus.list_containers():
        name = str(raw.get("name", ""))
        if name not in wanted:
            continue
        devices = raw.get("devices") or {}
        rows = [
            fwd
            for device, props in sorted(devices.items())
            if (fwd := parse_device(device, props)) is not None
        ]
        by_container[name] = rows
    return by_container


def forwards_for(incus: Incus, container: str) -> list[Forward]:
    """Forwards on one container, sorted by device name."""
    return list_forwards(incus, [container]).get(container, [])


def add_forward(
    incus: Incus,
    container: str,
    *,
    direction: Direction,
    proto: str,
    container_port: int,
    host_port: int,
    container_address: str,
    host_address: str,
) -> Forward:
    """Attach one ad-hoc forward, translating Incus's failures.

    Checks for an existing device of the same name first so the duplicate
    message can name the endpoints already in place — Incus's own
    "The device already exists" says nothing about what is there.
    """
    device = adhoc_device_name(direction, proto, container_port)
    existing = {f.device: f for f in forwards_for(incus, container)}
    if device in existing:
        clash = existing[device]
        raise PortError(
            f"Port {container_port}/{proto} is already forwarded in "
            f"{container}: {clash.container.display} ↔ {clash.host.display} "
            f"(device {device}). Remove it first with "
            f"`jailbee port rm {device}`."
        )
    props = render_device(
        direction,
        proto=proto,
        container_port=container_port,
        host_port=host_port,
        container_address=container_address,
        host_address=host_address,
    )
    try:
        incus.config_device_add(container, device, "proxy", props)
    except IncusError as e:
        raise _translate(
            e,
            container=container,
            container_port=container_port,
            host_port=host_port,
        ) from e
    return Forward(
        device=device,
        direction=direction,
        proto=proto,
        container=Endpoint(
            proto=proto,
            address=container_address,
            port=container_port,
            raw=f"{proto}:{_addr(container_address)}:{container_port}",
        ),
        host=Endpoint(
            proto=proto,
            address=host_address,
            port=host_port,
            raw=f"{proto}:{_addr(host_address)}:{host_port}",
        ),
        source="ad-hoc",
    )


def _translate(
    exc: IncusError,
    *,
    container: str,
    container_port: int,
    host_port: int,
) -> PortError:
    """Turn a known Incus failure into something a user can act on.

    The strings are Incus 6.0.5's, recorded verbatim in
    docs/manual-testing.md. The third is the one that must never reach a
    user raw: it names neither the port nor the cause.
    """
    message = str(exc)
    if "already exists" in message.lower():
        return PortError(
            f"Port {container_port} is already forwarded in {container}. "
            f"Remove it first with `jailbee port rm {container_port}`."
        )
    if "address already in use" in message:
        return PortError(
            f"host port {host_port} is already in use, so it cannot receive "
            f"{container}'s forward. Pick another with `--host-port N`, or "
            f"let jailbee choose with `--host-port auto`."
        )
    if "Failed to receive fd from listener process" in message:
        return PortError(
            f"Could not open port {container_port} inside {container} — "
            f"something is already listening on port {container_port} inside "
            f"the container. Stop it, or forward to a different container "
            f"port."
        )
    return PortError(f"Incus refused the forward: {message}")


def resolve_handle(forwards: Sequence[Forward], handle: str) -> Forward:
    """Find one forward by device name, config entry name, or port number.

    Resolution order is exact device name, then `host_ports` entry name, then
    container-side port. An ambiguous port is an error naming both devices
    rather than a guess.
    """
    for fwd in forwards:
        if fwd.device == handle:
            return fwd
    named = [f for f in forwards if f.device == config_device_name(handle)]
    if named:
        return named[0]
    if handle.isdigit():
        port = int(handle)
        matches = [f for f in forwards if f.container.port == port]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            devices = ", ".join(f.device for f in matches)
            raise PortError(
                f"Port {handle} matches more than one forward ({devices}). "
                f"Name the device instead."
            )
    known = ", ".join(f.device for f in forwards) or "none"
    raise PortError(f"There is no forward matching {handle!r}. Present: {known}.")


def remove_forward(incus: Incus, container: str, handle: str) -> Forward:
    """Detach one forward, resolved from a device name, entry name or port."""
    fwd = resolve_handle(forwards_for(incus, container), handle)
    incus.config_device_remove(container, fwd.device)
    return fwd


# How many times `--host-port auto` asks the OS for a port before giving up.
# Each attempt returns an OS-chosen free port; the loop only repeats when that
# port is already claimed by another container's declared forward, which is
# rare enough that a handful of tries is plenty.
_AUTO_ALLOCATE_ATTEMPTS = 10


def host_port_free(host_address: str, port: int) -> bool:
    """True if nothing is listening on ``host_address:port`` right now.

    A bind test, not a connect test: a port with a listener refuses the bind
    even when it accepts connections, and a port with nothing on it binds
    cleanly. The result is inherently a snapshot — Incus may still lose the
    race — which is why the caller also translates Incus's own
    "address already in use".
    """
    family = socket.AF_INET6 if ":" in host_address else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host_address, port))
        except OSError:
            return False
    return True


def _probe_free_port(host_address: str) -> int:
    """Ask the OS for a free port by binding port 0 and reading it back."""
    family = socket.AF_INET6 if ":" in host_address else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.bind((host_address, 0))
        port: int = probe.getsockname()[1]
    return port


def declared_host_ports(incus: Incus, *, exclude: str | None = None) -> dict[int, str]:
    """Host ports claimed by `to-host` forwards, mapped to their container.

    Includes stopped containers on purpose: their forwards want the port back
    on boot, so handing it to someone else now only moves the failure.
    Only `to-host` forwards are listeners; a `to-container` forward's host
    side is a connect target and occupies nothing.
    """
    claimed: dict[int, str] = {}
    for raw in incus.list_containers():
        name = str(raw.get("name", ""))
        if not name or name == exclude:
            continue
        for device, props in (raw.get("devices") or {}).items():
            fwd = parse_device(device, props)
            if fwd is None or fwd.direction != "to-host" or fwd.host.port is None:
                continue
            claimed.setdefault(fwd.host.port, name)
    return claimed


def allocate_host_port(host_address: str, taken: set[int]) -> int:
    """Pick a free host port the OS offers and no other container claims."""
    for _ in range(_AUTO_ALLOCATE_ATTEMPTS):
        port = _probe_free_port(host_address)
        if port not in taken:
            return port
    raise PortError(
        f"could not find a free host port on {host_address} after "
        f"{_AUTO_ALLOCATE_ATTEMPTS} attempts — pass `--host-port N` instead."
    )


def check_host_port(
    incus: Incus,
    host_address: str,
    port: int,
    *,
    container: str,
) -> None:
    """Refuse an explicit host port that is taken, before calling Incus.

    Two ways it can be taken: another container declared a forward on it, or
    something on the host is listening. The first is worth naming, because it
    is ours and the user can move it.
    """
    claimed = declared_host_ports(incus, exclude=container)
    if port in claimed:
        raise PortError(
            f"Host port {port} is already forwarded to container "
            f"{claimed[port]}. Pick another with `--host-port N`, or "
            f"`--host-port auto`."
        )
    if not host_port_free(host_address, port):
        raise PortError(
            f"Host port {port} is already in use on the host. Pick another "
            f"with `--host-port N`, or `--host-port auto`."
        )
