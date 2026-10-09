"""Container aliases: a second, instantly changeable name (`jailbee rename`).

The real Incus name never changes — every piece of state keyed on it (jobs,
ACLs, the `.local` share, snapshots) stays put. The alias is one label,
settable on a running container, that `lifecycle.resolve_container_name`
accepts and the listings show.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

ALIAS_LABEL = "user.jailbee.alias"
_ALIAS_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


class AliasError(ValueError):
    """An alias that cannot be set; the message is user-facing."""


def set_alias(cfg: Config, incus: Incus, full_name: str, alias: str) -> None:
    """Give ``full_name`` the alias ``alias``; its own short name clears it."""
    from jailbee.lifecycle import find_by_alias, short_name

    if not _ALIAS_RE.match(alias):
        raise AliasError(
            f"invalid alias '{alias}': use lowercase letters, digits and '-', "
            "starting and ending with a letter or digit"
        )
    if alias == short_name(cfg, full_name):
        clear_alias(incus, full_name)
        return
    instances = incus.list_containers(fast=True)
    own_base = f"{cfg.container_prefix}-base"
    for raw in instances:
        if own_base in (raw.get("profiles") or []) and short_name(cfg, raw["name"]) == alias:
            raise AliasError(f"'{alias}' is a container of this repo; pick another alias")
    owner = find_by_alias(cfg, instances, alias)
    if owner is not None and owner != full_name:
        raise AliasError(f"'{alias}' is already the alias of '{short_name(cfg, owner)}'")
    incus.config_set(full_name, ALIAS_LABEL, alias)


def clear_alias(incus: Incus, full_name: str) -> None:
    """Remove ``full_name``'s alias (`config_unset` is a no-op when absent)."""
    incus.config_unset(full_name, ALIAS_LABEL)
