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

import yaml

from jailbee.config import Config
from jailbee.egress import EgressEntry

ALLOWLIST_DESC_PREFIX = "allowlisted: "


def acl_name(cfg: Config) -> str:
    """Per-repo ACL name."""
    return f"{cfg.container_prefix}-allowlist"


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
    egress: list[dict[str, str]] = []

    # DHCP — required for the container to acquire an IPv4/IPv6 lease
    # from incusbr0's own dnsmasq. The NIC's implicit default-reject
    # would otherwise drop DHCP frames.
    egress.append(
        {
            "action": "allow",
            "destination_port": "67",
            "protocol": "udp",
            "description": "DHCPv4 client → server",
            "state": "enabled",
        }
    )
    egress.append(
        {
            "action": "allow",
            "destination_port": "547",
            "protocol": "udp",
            "description": "DHCPv6 client → server",
            "state": "enabled",
        }
    )

    # DNS — always allowed (resolver itself needs port 53).
    egress.append(
        {
            "action": "allow",
            "destination_port": "53",
            "protocol": "udp",
            "description": "DNS",
            "state": "enabled",
        }
    )
    egress.append(
        {
            "action": "allow",
            "destination_port": "53",
            "protocol": "tcp",
            "description": "DNS over TCP",
            "state": "enabled",
        }
    )

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
            egress.append(rule)

    # No explicit default-reject rule: Incus prioritises by action type
    # so it would be evaluated before allow rules. The NIC's implicit
    # default-reject (rendered at the chain tail) provides default-deny.

    acl = {
        "name": acl_name(cfg),
        "description": "jailbee container egress allowlist (default-deny)",
        "egress": egress,
        "ingress": [
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
        ],
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


def extra_acl_yaml(name: str, entries: list[EgressEntry]) -> str:
    """Generate a per-container extra allowlist ACL.

    Allow rules only. DHCP, DNS and the registry-mirror rules deliberately
    stay out: this ACL is applied to the same NIC as `<repo>-allowlist`,
    which already carries them, and Incus combines the rules of every ACL on
    a NIC. Default-deny is unaffected — it comes from the NIC's implicit
    default action at the chain tail, not from any rule here.

    Descriptions use the same `ALLOWLIST_DESC_PREFIX` as the repo ACL, so
    `entries_from_acl_yaml` reads this ACL back unchanged and `/etc/hosts`
    pinning works identically for both.
    """
    egress: list[dict[str, str]] = []
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
            egress.append(rule)

    acl = {
        "name": name,
        "description": "jailbee per-container egress additions",
        "egress": egress,
        "ingress": [],
    }
    return yaml.safe_dump(acl, sort_keys=False)
