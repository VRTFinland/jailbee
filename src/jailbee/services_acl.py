"""Host-global ACL for JailBee service containers.

Strict NICs and both managed bridges reference this name. Create it before
writing any reference, including paths reached without a preceding apply on
an upgraded host (egress refresh, mode switches and snapshot restores).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from jailbee.network import SERVICES_ACL, services_acl_yaml

if TYPE_CHECKING:
    from jailbee.incus import Incus

LITELLM_LABEL = "jailbee LiteLLM proxy"
EGRESS_PROXY_LABEL = "jailbee egress proxy"


def ensure_services_acl(incus: Incus) -> None:
    """Create an empty services ACL if this host has not seen it yet."""
    if incus.network_acl_exists(SERVICES_ACL):
        return
    from jailbee.egress_pool import _apply_acl_with_nft_quirk

    incus.network_acl_create(SERVICES_ACL)
    _apply_acl_with_nft_quirk(incus, SERVICES_ACL, services_acl_yaml({}))


def set_service(incus: Incus, label: str, endpoint: tuple[list[str], list[int]] | None) -> None:
    """Replace (or, with None, remove) one service's rules; the others stay untouched."""
    ensure_services_acl(incus)
    raw = incus.network_acl_show(SERVICES_ACL)
    parsed = yaml.safe_load(raw) if isinstance(raw, str) else None
    live = parsed.get("egress") if isinstance(parsed, dict) else None
    kept = [r for r in live or [] if isinstance(r, dict) and r.get("description") != label]
    fresh = yaml.safe_load(services_acl_yaml({} if endpoint is None else {label: endpoint}))
    acl = yaml.safe_load(services_acl_yaml({}))
    acl["egress"] = sorted([*kept, *fresh["egress"]], key=lambda r: str(r.get("description", "")))
    incus.network_acl_set_yaml(SERVICES_ACL, yaml.safe_dump(acl, sort_keys=False))


def other_service_ips(incus: Incus, excluding: str) -> list[str]:
    """Addresses of every service in the ACL except the one labelled ``excluding``.

    The egress proxy's clients must reach these directly: sending a service's
    own traffic through Squid hits a 403, since the proxy has no rule for it.
    Reads the live ACL; an absent or unparsable ACL yields no addresses.
    """
    if not incus.network_acl_exists(SERVICES_ACL):
        return []
    raw = incus.network_acl_show(SERVICES_ACL)
    parsed = yaml.safe_load(raw) if isinstance(raw, str) else None
    rules = parsed.get("egress") if isinstance(parsed, dict) else None
    found: list[str] = []
    for rule in rules or []:
        if not isinstance(rule, dict) or rule.get("description") == excluding:
            continue
        destination = rule.get("destination")
        if isinstance(destination, str):
            ip = destination.removesuffix("/32")
            if ip and ip not in found:
                found.append(ip)
    return sorted(found)
