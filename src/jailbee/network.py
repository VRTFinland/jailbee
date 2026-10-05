"""Network ACL generation for the strict profile.

The ACL is per-repo, named ``<repo>-allowlist`` and applied via the
strict profile's eth0 device. It is default-deny on both egress and
ingress, with explicit allow rules for the destinations listed in
``config.egress_allow``.

Default-deny is implemented via Incus' implicit per-NIC default action
(rendered to the END of the generated nftables chain), NOT via an
explicit ``action: reject`` rule in the ACL — Incus prioritises ACL
rules by action type (drop > reject > allow > default), so an explicit
reject rule in the ACL would be evaluated BEFORE allow rules and drop
DHCP/DNS before they could match.

Hostnames in egress_allow are resolved to IPv4 addresses at ACL-apply
time (Incus 6.x ACLs require IP destinations). See `egress.py` for the
parser and resolver.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from jailbee.config import Config
from jailbee.egress import EgressEntry

if TYPE_CHECKING:
    from collections.abc import Mapping

ALLOWLIST_DESC_PREFIX = "allowlisted: "
SERVICES_ACL = "jailbee-services"
"""Host-global ACL carrying allow rules to jailbee's own service containers
(today: the LiteLLM proxy). Appended to every strict NIC's ACL list and
attached to `incusbr0` and `jailbee-work`, so one rule written by
`jailbee litellm up` passes both filter chains for every repo."""


def work_loose_rule(ip: str) -> dict[str, str]:
    """Allow only traffic sourced by one verified work-container address."""
    return {"action": "allow", "source": f"{ip}/32", "state": "enabled"}


def _allow_rules(entries: list[EgressEntry]) -> list[dict[str, str]]:
    """The `allow` egress rules for `entries`, shared by both ACL renderers.

    `allowlist_acl_yaml` (the repo ACL) and `extra_acl_yaml` (a container's
    own extra ACL) used to duplicate this loop verbatim. Drift between the
    two copies would silently break `entries_from_acl_yaml`'s round-trip —
    which `/etc/hosts` pinning depends on for both ACL kinds — since it
    parses both back with the same rule shape.
    """
    rules: list[dict[str, str]] = []
    for entry in entries:
        for dest in entry.destinations:
            rule: dict[str, str] = {
                "action": "allow",
                "destination": dest,
                "description": f"{ALLOWLIST_DESC_PREFIX}{entry.description}",
                "state": "enabled",
            }
            if entry.port is not None:
                rule["protocol"] = "tcp"
                rule["destination_port"] = str(entry.port)
            rules.append(rule)
    return rules


def acl_name(cfg: Config) -> str:
    """Per-repo ACL name."""
    return f"{cfg.container_prefix}-allowlist"


def strict_nic_acls(cfg: Config, *extra: str) -> list[str]:
    """The ACL list of a strict NIC: repo allowlist, container extras, services."""
    return [acl_name(cfg), *extra, SERVICES_ACL]


def _base_egress_rules() -> list[dict[str, str]]:
    """DHCP and DNS allowances shared by repo and service-container ACLs."""

    # DHCP — required for the container to acquire an IPv4/IPv6 lease
    # from incusbr0's own dnsmasq. The NIC's implicit default-reject
    # would otherwise drop DHCP frames.
    return [
        {
            "action": "allow",
            "destination_port": "67",
            "protocol": "udp",
            "description": "DHCPv4 client → server",
            "state": "enabled",
        },
        {
            "action": "allow",
            "destination_port": "547",
            "protocol": "udp",
            "description": "DHCPv6 client → server",
            "state": "enabled",
        },
        # DNS — always allowed (resolver itself needs port 53).
        {
            "action": "allow",
            "destination_port": "53",
            "protocol": "udp",
            "description": "DNS",
            "state": "enabled",
        },
        {
            "action": "allow",
            "destination_port": "53",
            "protocol": "tcp",
            "description": "DNS over TCP",
            "state": "enabled",
        },
    ]


def _gateway_cidr(address: str) -> str:
    return f"{address}/{128 if ':' in address else 32}"


def _scoped_egress_rules(gateways: list[str]) -> list[dict[str, str]]:
    """DHCP and DNS allowances pinned to the bridge, for token-holding containers.

    The unscoped `_base_egress_rules` allow tcp/udp 53 and udp 67/547 to any
    host, which is an open covert channel for a container whose whole point is
    to be limited to its provider hosts. Here DNS may only reach the bridge's
    own addresses (`gateways`, where dnsmasq listens), and DHCP only the
    broadcast/multicast group and the on-link server. Without `gateways` no DNS
    rule is emitted: the container fails closed rather than open.
    """
    dhcp4 = ["255.255.255.255/32", *(_gateway_cidr(g) for g in gateways if ":" not in g)]
    rules = [
        {
            "action": "allow",
            "destination": ",".join(dhcp4),
            "destination_port": "67",
            "protocol": "udp",
            "description": "DHCPv4 client → server",
            "state": "enabled",
        },
        # Link-local covers the server's unicast renewals; it never leaves the link.
        {
            "action": "allow",
            "destination": "ff02::1:2/128,fe80::/10",
            "destination_port": "547",
            "protocol": "udp",
            "description": "DHCPv6 client → server",
            "state": "enabled",
        },
    ]
    if gateways:
        dns = ",".join(_gateway_cidr(g) for g in gateways)
        for protocol, description in (("udp", "DNS"), ("tcp", "DNS over TCP")):
            rules.append(
                {
                    "action": "allow",
                    "destination": dns,
                    "destination_port": "53",
                    "protocol": protocol,
                    "description": f"{description} (bridge resolver only)",
                    "state": "enabled",
                }
            )
    return rules


def _base_ingress_rules() -> list[dict[str, str]]:
    """DHCP replies shared by repo and service-container ACLs."""
    return [
        {
            "action": "allow",
            "destination_port": "68",
            "protocol": "udp",
            "description": "DHCPv4 server → client",
            "state": "enabled",
        },
        {
            "action": "allow",
            "destination_port": "546",
            "protocol": "udp",
            "description": "DHCPv6 server → client",
            "state": "enabled",
        },
    ]


def allowlist_acl_yaml(
    cfg: Config,
    entries: list[EgressEntry],
    mirror_endpoint: tuple[str, int] | None = None,
) -> str:
    """Generate the <repo>-allowlist ACL YAML from already-resolved entries.

    `entries` is required: the caller owns resolution, so the same DNS
    answers feed both the ACL and `/etc/hosts`. It used to default to
    resolving `cfg.egress_allow` here, which became a silent bug once
    host-local repo overrides existed — that path would render an ACL
    missing them, with no error. Callers build the list with
    `egress_scope.effective_repo_entries` + `egress.build_egress_entries`
    (or from the IP pool, in `egress_pool._write_acl`).

    Pass `mirror_endpoint=(ip, port)` to auto-inject an allow rule for
    the host Docker registry mirror. The mirror rule's
    description deliberately does not use the "allowlisted: " prefix, so
    it is invisible to `entries_from_acl_yaml` and the /etc/hosts
    pinning path.
    """
    egress = _base_egress_rules()

    # Docker registry mirror on the host's incusbr0 gateway.
    if mirror_endpoint is not None:
        ip, port = mirror_endpoint
        egress.append(
            {
                "action": "allow",
                "destination": ip,
                "destination_port": str(port),
                "protocol": "tcp",
                "description": "Docker registry mirror",
                "state": "enabled",
            }
        )

    # Allowlist rules from config.
    egress.extend(_allow_rules(entries))

    # No explicit default-reject rule: Incus prioritises by action type
    # so it would be evaluated before allow rules. The NIC's implicit
    # default-reject (rendered at the chain tail) provides default-deny.

    acl = {
        "name": acl_name(cfg),
        "description": "jailbee container egress allowlist (default-deny)",
        "egress": egress,
        "ingress": _base_ingress_rules(),
    }
    return yaml.safe_dump(acl, sort_keys=False)


def services_acl_yaml(services: Mapping[str, tuple[list[str], list[int]]]) -> str:
    """Render the services ACL: one rule per ip and port, described by its service label."""
    egress: list[dict[str, str]] = []
    for label in sorted(services):
        ips, ports = services[label]
        for ip in ips:
            for port in ports:
                egress.append(
                    {
                        "action": "allow",
                        "destination": f"{ip}/32",
                        "destination_port": str(port),
                        "protocol": "tcp",
                        "description": label,
                        "state": "enabled",
                    }
                )
    acl = {
        "name": SERVICES_ACL,
        "description": "jailbee service containers reachable from strict containers",
        "egress": egress,
        "ingress": [],
    }
    return yaml.safe_dump(acl, sort_keys=False)


def service_container_acl_yaml(
    name: str,
    entries: list[EgressEntry],
    *,
    listen_ports: list[int],
    gateways: list[str],
) -> str:
    """NIC ACL of a jailbee service container: DHCP/DNS, `entries` out, `listen_ports` in.

    `gateways` are the bridge's own addresses; DNS is allowed to them only.
    """
    ingress = _base_ingress_rules()
    for port in listen_ports:
        ingress.append(
            {
                "action": "allow",
                "destination_port": str(port),
                "protocol": "tcp",
                "description": "jailbee service port",
                "state": "enabled",
            }
        )
    acl = {
        "name": name,
        "description": "jailbee service container egress allowlist (default-deny)",
        "egress": [*_scoped_egress_rules(gateways), *_allow_rules(entries)],
        "ingress": ingress,
    }
    return yaml.safe_dump(acl, sort_keys=False)


def entries_from_acl_yaml(acl_yaml: str) -> list[EgressEntry]:
    """Reconstruct the `EgressEntry` list embedded in an applied ACL YAML.

    The reverse of the allowlist rules generated by `allowlist_acl_yaml`.
    Used as the source of truth for `/etc/hosts` pinning when callers
    need ACL/hosts consistency without re-resolving DNS (which would
    desync for GSLB-rotating hosts).

    Rules without an ``allowlisted: <raw>`` description (DNS, DHCP) are
    skipped. Rules with the same description coalesce into a single
    entry, preserving their order in the ACL.
    """
    if not acl_yaml.strip():
        return []
    parsed = yaml.safe_load(acl_yaml) or {}
    rules = parsed.get("egress") or []

    # Use dict-ordered grouping to preserve first-seen order per description.
    by_desc: dict[str, EgressEntry] = {}
    for rule in rules:
        desc = rule.get("description", "")
        if not desc.startswith(ALLOWLIST_DESC_PREFIX):
            continue
        raw = desc[len(ALLOWLIST_DESC_PREFIX) :]
        dest = rule.get("destination")
        if not dest:
            continue
        port_str = rule.get("destination_port")
        port = int(port_str) if port_str is not None else None
        if raw in by_desc:
            existing = by_desc[raw]
            by_desc[raw] = EgressEntry(
                destinations=[*existing.destinations, dest],
                port=existing.port,
                description=raw,
            )
        else:
            by_desc[raw] = EgressEntry(
                destinations=[dest],
                port=port,
                description=raw,
            )
    return list(by_desc.values())


EXTRA_ACL_DESC = "jailbee per-container egress additions"
BRIDGE_EXTRAS_ACL_DESC = (
    "jailbee container-scope egress additions, union over the repo's "
    "containers — attached to the bridge network only, never to a NIC"
)


def extra_acl_yaml(
    name: str,
    entries: list[EgressEntry],
    *,
    description: str = EXTRA_ACL_DESC,
) -> str:
    """Generate a per-container extra allowlist ACL.

    Allow rules only. DHCP, DNS and the registry-mirror rules deliberately
    stay out: this ACL is applied to the same NIC as `<repo>-allowlist`,
    which already carries them, and Incus combines the rules of every ACL on
    a NIC. Default-deny is unaffected — it comes from the NIC's implicit
    default action at the chain tail, not from any rule here.

    Descriptions use the same `ALLOWLIST_DESC_PREFIX` as the repo ACL, so
    `entries_from_acl_yaml` reads this ACL back unchanged and `/etc/hosts`
    pinning works identically for both.

    `description` is the ACL's own human-readable label, not a rule
    description. `egress_scope.sync_bridge_extras` renders the bridge-level
    union ACL with the same rule shape and passes
    `BRIDGE_EXTRAS_ACL_DESC` so `incus network acl list` says which of the
    two kinds a given ACL is.
    """
    acl = {
        "name": name,
        "description": description,
        "egress": _allow_rules(entries),
        "ingress": [],
    }
    return yaml.safe_dump(acl, sort_keys=False)
