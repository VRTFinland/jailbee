"""/etc/hosts pinning for strict-profile containers.

Strict-mode containers freeze their view of egress_allow hostnames to
the IPs the ACL currently allows, so the container's `getaddrinfo`
returns the exact IPs the ACL enforces. Without this, GSLB-rotating
hosts (e.g. github.com) drift between jailbee's resolved IP and the
container's current DNS answer.

**Source of truth: the ACL.** `apply_hosts` reads the live ACL via
`incus network acl show` by default and mirrors its destinations into
`/etc/hosts`. Callers that already hold a resolved
`list[EgressEntry]` (e.g. during `jailbee init` or `jailbee net refresh`)
pass it via `entries=` to skip the round-trip and guarantee ACL/hosts
consistency within the same operation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jailbee.egress import EgressEntry, parse_egress_entry

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

_BEGIN_SENTINEL_PREFIX = "# BEGIN jailbee-managed allowlist"
BEGIN_SENTINEL = f"{_BEGIN_SENTINEL_PREFIX} (do not edit, managed by `jailbee net refresh`)"
END_SENTINEL = "# END jailbee-managed allowlist"

# The awk patterns match on the sentinel *prefix*, which is why
# `_BEGIN_SENTINEL_PREFIX` exists as its own constant: the full
# `BEGIN_SENTINEL` carries a parenthesised note with backticks, and
# embedding that in an awk regex would need escaping for no benefit.
_STRIP_MANAGED_BLOCK_AWK = f"""\
awk '
  /^{_BEGIN_SENTINEL_PREFIX}/ {{ in_block=1; next }}
  /^{END_SENTINEL}/ {{ in_block=0; next }}
  !in_block
' /etc/hosts"""


def render_hosts_block(
    entries: list[EgressEntry],
    *,
    mirror_endpoint: tuple[str, int] | None = None,
) -> str:
    """Build the jailbee-managed /etc/hosts block from resolved entries.

    `entries` is the single source of IPs — pre-resolved by the caller
    (or read from the live ACL). Hostname-derived entries become
    `<ip> <hostname>` lines; IP/CIDR-literal entries are skipped.

    `mirror_endpoint=(ip, port)` adds an `<ip> jailbee-registry-mirror.incus`
    row inside the same sentinel block. Required for strict containers,
    which sit on `incusbr0` and query its dnsmasq — but the mirror lives
    on `jailbee-loose`, whose dnsmasq is the only one with the `.incus` record
    for the mirror. The port is ignored here; only the IP
    is pinned, because the URL already carries the port.

    Returns an empty string when there are no rows to emit (neither
    hostname entries nor a mirror_endpoint).
    """
    seen: dict[str, list[str]] = {}
    for entry in entries:
        spec = parse_egress_entry(entry.description)
        if spec.is_literal:
            continue
        if spec.target in seen:
            continue
        seen[spec.target] = list(entry.destinations)

    if not seen and mirror_endpoint is None:
        return ""

    lines = [BEGIN_SENTINEL]
    for host, ips in seen.items():
        for ip in ips:
            lines.append(f"{ip} {host}")
    if mirror_endpoint is not None:
        from jailbee.docker_daemon import MIRROR_DNS_NAME

        lines.append(f"{mirror_endpoint[0]} {MIRROR_DNS_NAME}")
    lines.append(END_SENTINEL)
    return "\n".join(lines) + "\n"


def apply_hosts(
    cfg: Config,
    incus: Incus,
    name: str,
    *,
    entries: list[EgressEntry] | None = None,
    mirror_endpoint: tuple[str, int] | None = None,
) -> None:
    """Write the jailbee-managed block into the container's /etc/hosts.

    By default, derives the IP set from the live ACL (`<repo>-allowlist`)
    so /etc/hosts always mirrors what the ACL actually allows. Callers
    that have just resolved hostnames themselves should pass `entries=`
    so the same resolution feeds both ACL and /etc/hosts.

    `mirror_endpoint=(ip, port)` adds the registry mirror's IPv4 row to
    the block — required for strict containers, which can't resolve
    `jailbee-registry-mirror.incus` via `incusbr0`'s dnsmasq.

    Runs a single `bash -c` via `incus exec` that:
      1. awk-strips any existing BEGIN/END jailbee-managed block from
         /etc/hosts into a tmp file.
      2. Appends the freshly rendered block (if non-empty).
      3. Atomically moves the tmp file into place.

    No-op when the ACL has no hostname-derived rules and no
    mirror_endpoint is passed. Propagates `incus.IncusError` from the
    underlying exec; the caller decides whether to warn or abort.
    """
    if entries is None:
        entries = _entries_from_live_acl(cfg, incus)
    block = render_hosts_block(entries, mirror_endpoint=mirror_endpoint)
    if not block:
        return

    # Bash heredoc with explicit terminator so the rendered block
    # (which may contain '#' lines) is preserved verbatim.
    script = f"""\
set -euo pipefail
tmp=$(mktemp)
{_STRIP_MANAGED_BLOCK_AWK} > "$tmp"
cat >> "$tmp" <<'JAILBEE_HOSTS_EOF'
{block.rstrip()}
JAILBEE_HOSTS_EOF
mv "$tmp" /etc/hosts
"""
    incus.exec(name, ["bash", "-c", script])


def clear_hosts(cfg: Config, incus: Incus, name: str) -> None:
    """Remove the jailbee-managed block from /etc/hosts.

    Strips only the BEGIN..END range; user-written entries are preserved.
    Idempotent: containers with no block produce a no-op rewrite.
    Does not touch DNS.
    """
    del cfg  # signature parity with apply_hosts; no config needed here
    script = f"""\
set -euo pipefail
tmp=$(mktemp)
{_STRIP_MANAGED_BLOCK_AWK} > "$tmp"
mv "$tmp" /etc/hosts
"""
    incus.exec(name, ["bash", "-c", script])


def _entries_from_live_acl(cfg: Config, incus: Incus) -> list[EgressEntry]:
    """Read the live `<repo>-allowlist` ACL and return its allowlisted entries.

    Local import keeps `jailbee --help` startup fast (`network` pulls yaml).
    Returns `[]` if the wrapper returns a non-string payload (defensive
    guard so unit tests that pass a `MagicMock` incus don't accidentally
    feed a mock object to `yaml.safe_load`).
    """
    from jailbee.network import acl_name, entries_from_acl_yaml

    yaml_text = incus.network_acl_show(acl_name(cfg))
    if not isinstance(yaml_text, str):
        return []
    return entries_from_acl_yaml(yaml_text)
