"""Egress allowlist parsing and DNS resolution.

`egress_allow` entries support six forms:
    <hostname>             → resolve, all TCP ports
    <hostname>:<port>      → resolve, TCP/<port> only
    <ipv4>                 → literal, all TCP ports
    <ipv4>:<port>          → literal, TCP/<port> only
    <cidr>                 → literal, all TCP ports
    <cidr>:<port>          → literal, TCP/<port> only

IPv6 is not supported (single `:` is unambiguous as the host/port
separator).
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass

# RFC 1123-ish hostname: labels of [a-z0-9-], not starting/ending with -,
# joined by dots. Pragmatic, not exhaustive.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*"
    r"[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$"
)


@dataclass(frozen=True)
class EgressSpec:
    """One parsed entry from `egress_allow`.

    `target` is a hostname (when `is_literal=False`), or an IPv4/CIDR
    string (when `is_literal=True`). `port` is None for "all TCP ports".
    """

    target: str
    port: int | None
    is_literal: bool


@dataclass(frozen=True)
class EgressEntry:
    """A parsed + resolved entry ready for ACL rule generation."""

    destinations: list[str]  # IPv4 or CIDR strings — what goes in ACL `destination:`
    port: int | None  # None → all TCP ports
    description: str  # original raw entry, for ACL `description:`


class NetworkResolveError(Exception):
    """A hostname in egress_allow could not be resolved to IPv4."""

    def __init__(self, hostname: str, cause: Exception | None = None) -> None:
        self.hostname = hostname
        self.cause = cause
        msg = f"failed to resolve hostname: {hostname!r}"
        if cause is not None:
            msg += f" ({type(cause).__name__}: {cause})"
        super().__init__(msg)


def parse_egress_entry(raw: str) -> EgressSpec:
    """Parse one `egress_allow` entry. Raises ValueError on malformed input."""
    if not raw:
        raise ValueError("empty egress_allow entry")

    target, port = _split_target_port(raw)
    is_literal = _is_ip_or_cidr(target)

    if not is_literal and not _HOSTNAME_RE.match(target):
        raise ValueError(
            f"invalid host in egress_allow entry {raw!r}: "
            f"not a valid hostname, IPv4 address, or CIDR"
        )

    return EgressSpec(target=target, port=port, is_literal=is_literal)


def _split_target_port(raw: str) -> tuple[str, int | None]:
    """Split on the LAST `:`. Returns (target, port_or_None)."""
    if ":" not in raw:
        return raw, None

    head, _sep, tail = raw.rpartition(":")
    # CIDR notation uses "/" not ":", so rpartition is safe.
    try:
        port = int(tail)
    except ValueError as e:
        raise ValueError(
            f"invalid port in egress_allow entry {raw!r}: {tail!r} is not an integer"
        ) from e
    if not (1 <= port <= 65535):
        raise ValueError(
            f"invalid port in egress_allow entry {raw!r}: {port} is out of range 1..65535"
        )
    return head, port


def _is_ip_or_cidr(value: str) -> bool:
    """True if `value` parses as an IPv4 address or CIDR."""
    try:
        net = ipaddress.ip_network(value, strict=False)
    except (ValueError, TypeError):
        return False
    return isinstance(net, ipaddress.IPv4Network)


def resolve_hostnames(names: list[str]) -> dict[str, list[str]]:
    """Resolve each hostname to IPv4 via socket.getaddrinfo.

    Returns {name: [ipv4, ...]} with deduplicated, sorted IPv4 strings.
    Raises NetworkResolveError on any failure (including empty IPv4 result).
    """
    result: dict[str, list[str]] = {}
    for name in names:
        try:
            infos = socket.getaddrinfo(
                name,
                None,
                family=socket.AF_INET,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as e:
            raise NetworkResolveError(name, e) from e
        # info[4] is a sockaddr tuple; for AF_INET it's (host: str, port: int).
        ips = sorted({str(info[4][0]) for info in infos})
        if not ips:
            raise NetworkResolveError(name)
        result[name] = ips
    return result


def build_egress_entries(raw_entries: list[str]) -> list[EgressEntry]:
    """Parse raw `egress_allow` strings and resolve hostnames in one pass.

    Hostnames are deduplicated before resolution so each name hits DNS at
    most once per call. Input order is preserved in the output.
    """
    specs = [parse_egress_entry(raw) for raw in raw_entries]

    hostnames_to_resolve = sorted({spec.target for spec in specs if not spec.is_literal})
    resolved = resolve_hostnames(hostnames_to_resolve)

    entries: list[EgressEntry] = []
    for raw, spec in zip(raw_entries, specs, strict=True):
        if spec.is_literal:
            destinations = [spec.target]
        else:
            destinations = list(resolved[spec.target])
        entries.append(
            EgressEntry(
                destinations=destinations,
                port=spec.port,
                description=raw,
            )
        )
    return entries
