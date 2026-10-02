"""Render Squid rule fragments and the proxy environment for egress_allow.

Pure: no Incus, no filesystem.

Each scope (a repo or one container) becomes a block of ACLs keyed on its
client addresses, so one scope's rules never apply to another's sources.
A scope with no sources or no entries is omitted entirely: Squid rejects an
empty ``src`` ACL, and one bad fragment would break every repo.

Entries are grouped by ``(kind, ports)`` in first-seen order. ``kind`` is
``domain`` (wildcards and hostnames) or ``dst`` (IP/CIDR). ``ports`` is
``None`` (all ports) for a hostname or IP without a port,
``DEFAULT_WILDCARD_PORTS`` for a wildcard without a port, and ``(port,)`` for
an explicit port. Every group becomes one destination ACL, an optional port
ACL, and one ``http_access allow``.

A ``dstdomain`` ACL holding both ``.vendor.com`` and ``vendor.com`` (or
``api.vendor.com``) makes Squid warn that the narrower name is already covered
(Squid 7.2 only warns; other versions may refuse to load it). Within one ACL a
hostname that a wildcard in the same ACL covers is therefore dropped; it matches
when it equals the domain or ends with ``"." + domain``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from jailbee.egress import EgressSpec, WildcardSpec, validate_allow_entry

PROXY_PORT: int = 3128
DEFAULT_WILDCARD_PORTS: tuple[int, ...] = (80, 443)
FRAGMENT_DIR: str = "/etc/squid/jailbee.d"

BASE_SQUID_CONF: str = f"""\
http_port {PROXY_PORT}
include {FRAGMENT_DIR}/*.conf
http_access deny all
cache deny all
via off
forwarded_for delete
access_log daemon:/var/log/squid/access.log squid
"""

_Group = tuple[str, tuple[int, ...] | None]


@dataclass(frozen=True)
class ProxyScope:
    key: str  # squid-safe id, e.g. "myrepo" or "myrepo-feat-x"
    sources: tuple[str, ...]  # client IPv4 addresses, no prefix length
    entries: tuple[str, ...]  # raw egress entries, any kind
    # "r" = a repo prefix, "c" = a container name. Both are [a-z0-9-]+ (no "_"), so
    # ``jb_<kind>_<key>_...`` cannot collide across kinds: a repo ``web-api`` and the
    # container ``web-api`` of repo ``web`` would otherwise share ACL names, which
    # Squid merges when the types match.
    kind: Literal["r", "c"] = "r"


def _group_entries(entries: Iterable[str]) -> dict[_Group, list[str]]:
    groups: dict[_Group, list[str]] = {}
    for raw in entries:
        spec = validate_allow_entry(raw)
        ports: tuple[int, ...] | None
        if isinstance(spec, WildcardSpec):
            ports = (spec.port,) if spec.port is not None else DEFAULT_WILDCARD_PORTS
            kind, name = "domain", f".{spec.domain}"
        else:
            assert isinstance(spec, EgressSpec)
            ports = (spec.port,) if spec.port is not None else None
            if spec.is_literal:
                kind = "dst"
                name = spec.target if "/" in spec.target else f"{spec.target}/32"
            else:
                kind, name = "domain", spec.target
        names = groups.setdefault((kind, ports), [])
        if name not in names:
            names.append(name)
    for (kind, _), names in groups.items():
        if kind == "domain":
            names[:] = _drop_covered(names)
    return groups


def _drop_covered(names: list[str]) -> list[str]:
    wildcards = [n[1:] for n in names if n.startswith(".")]

    def covered(domain: str, *, strict: bool) -> bool:
        return any(domain.endswith(f".{w}") or (not strict and domain == w) for w in wildcards)

    return [
        n for n in names if not covered(n[1:] if n.startswith(".") else n, strict=n.startswith("."))
    ]


# Squid's ACL names are limited (63 characters in older versions); the longest
# suffix this module appends is "_src" / "_d<n>" / "_p<n>" with a small n.
_ACL_NAME_MAX = 63
_ACL_SUFFIX_ROOM = 8


def _acl_base(scope: ProxyScope) -> str:
    """The ACL-name stem of a scope, hashed down when the key would not fit."""
    base = f"jb_{scope.kind}_{scope.key}"
    if len(base) + _ACL_SUFFIX_ROOM <= _ACL_NAME_MAX:
        return base
    digest = hashlib.sha1(scope.key.encode()).hexdigest()[:16]
    return f"jb_{scope.kind}_h{digest}"


def _render_scope(scope: ProxyScope) -> list[str]:
    base = _acl_base(scope)
    src = f"{base}_src"
    lines = [f"acl {src} src " + " ".join(f"{ip}/32" for ip in scope.sources)]
    for g, ((kind, ports), names) in enumerate(_group_entries(scope.entries).items()):
        dst = f"{base}_d{g}"
        if kind == "domain":
            lines.append(f"acl {dst} dstdomain -n " + " ".join(names))
        else:
            lines.append(f"acl {dst} dst " + " ".join(names))
        rule = f"http_access allow {src} {dst}"
        if ports is not None:
            port_acl = f"{base}_p{g}"
            lines.append(f"acl {port_acl} port " + " ".join(str(p) for p in ports))
            rule += f" {port_acl}"
        lines.append(rule)
    return lines


def render_fragment(prefix: str, scopes: Sequence[ProxyScope]) -> str:
    lines = [f"# jailbee egress proxy rules for {prefix} (generated, do not edit)"]
    for scope in scopes:
        if scope.sources and scope.entries:
            lines.extend(_render_scope(scope))
    return "\n".join(lines) + "\n"


def proxy_env(
    proxy_ip: str, raw_entries: Iterable[str], direct_hosts: Iterable[str] = ()
) -> dict[str, str]:
    """The proxy variables. ``direct_hosts`` (other jailbee services) bypass the proxy."""
    url = f"http://{proxy_ip}:{PROXY_PORT}"
    no_proxy = ["localhost", "127.0.0.1", ".incus"]
    for host in direct_hosts:
        if host not in no_proxy:
            no_proxy.append(host)
    for raw in raw_entries:
        spec = validate_allow_entry(raw)
        if isinstance(spec, EgressSpec) and spec.is_literal and spec.target not in no_proxy:
            no_proxy.append(spec.target)
    joined = ",".join(no_proxy)
    return {
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
        "http_proxy": url,
        "https_proxy": url,
        "NO_PROXY": joined,
        "no_proxy": joined,
    }
