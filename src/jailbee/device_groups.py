"""Grant the container ``dev`` user access to ``host_devices`` via groups.

A passed-in device such as ``/dev/kvm`` carries a udev ``static_node`` rule,
so the container's systemd-udevd resets the node to its distro-default owner
and mode (``root:kvm 0660``) on every boot — regardless of the ``mode`` set on
the Incus profile device. Empirically, neither the profile ``mode`` nor an
in-container udev override survives this; **group membership does**: if the
``dev`` user is in the node's owning group (e.g. ``kvm``), it can open the
device even at ``0660``.

For each ``host_devices`` entry we resolve the group ``dev`` must join — the
explicit ``entry.group``, or (when unset) the host source node's owning group
name — and add ``dev`` to it inside the container. This is idempotent and a
no-op for devices whose group is ``root`` or that resolve to nothing.
"""

from __future__ import annotations

import grp
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee.config import CONTAINER_USERNAME

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus


def _host_source_group(source: str) -> str | None:
    """Owning group name of a host device node, or None if unreadable/unknown."""
    try:
        gid = Path(source).stat().st_gid
        return grp.getgrgid(gid).gr_name
    except (OSError, KeyError):
        return None


def resolve_device_groups(cfg: Config) -> list[str]:
    """Container groups the ``dev`` user must join for ``host_devices`` access.

    For each entry: the group is ``entry.group`` when set, else the host
    source node's owning group (which requires the source to exist on the
    host). ``root`` and unresolvable groups are skipped. Order-preserving,
    de-duplicated.
    """
    groups: list[str] = []
    seen: set[str] = set()
    for dev in cfg.host_devices:
        if dev.group is not None:
            group: str | None = dev.group
        elif Path(dev.effective_source).exists():
            group = _host_source_group(dev.effective_source)
        else:
            group = None
        if not group or group == "root" or group in seen:
            continue
        seen.add(group)
        groups.append(group)
    return groups


def ensure_device_groups(cfg: Config, incus: Incus, name: str) -> list[str]:
    """Add the container ``dev`` user to each resolved ``host_devices`` group.

    Idempotent. A group that doesn't exist inside the container is skipped
    (the device likely isn't group-owned by it there). Returns the groups
    actually added (``dev`` was put into them).
    """
    added: list[str] = []
    for group in resolve_device_groups(cfg):
        # Always exit 0 (incus.exec raises on non-zero); report via stdout.
        # `group` is regex-validated at config load, so interpolation is safe.
        out = incus.exec(
            name,
            [
                "sh",
                "-c",
                f"if getent group {group} >/dev/null 2>&1; then "
                f"usermod -aG {group} {CONTAINER_USERNAME} && echo ADDED; "
                f"else echo NOGROUP; fi",
            ],
        )
        if "ADDED" in out:
            added.append(group)
    return added
