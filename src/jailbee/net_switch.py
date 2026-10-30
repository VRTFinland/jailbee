"""Non-interactive network switching shared by the CLI and orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus


@dataclass(frozen=True)
class SwitchResult:
    mode: str
    mirror_available: bool


def mirror_endpoint_or_none(cfg: Config, incus: Incus) -> tuple[str, int] | None:
    from jailbee.docker_daemon import compute_mirror_endpoint, mirror_wanted
    from jailbee.global_config import default_global_config_path, load_global_config

    gcfg, _ = load_global_config(default_global_config_path())
    if not mirror_wanted(cfg, gcfg):
        return None
    try:
        return compute_mirror_endpoint(incus, gcfg)
    except ValueError:
        return None


def switch_mode(
    cfg: Config, incus: Incus, name: str, mode: str, *, until: datetime | None
) -> SwitchResult:
    from jailbee.lifecycle import current_network_mode, switch_network

    endpoint = mirror_endpoint_or_none(cfg, incus)
    pre_mode = current_network_mode(cfg, incus, name)
    switch_network(cfg, incus, name, mode, mirror_endpoint=endpoint)
    if mode == "loose" and until is not None:
        revert_to = (
            incus.config_get(name, "user.jailbee.loose_revert_to") or "strict"
            if pre_mode == "loose" else "strict"
        )
        incus.config_set(name, "user.jailbee.loose_revert_to", revert_to)
        incus.config_set(name, "user.jailbee.loose_until", until.isoformat())
    else:
        incus.config_unset(name, "user.jailbee.loose_until")
        incus.config_unset(name, "user.jailbee.loose_revert_to")
    return SwitchResult(mode=mode, mirror_available=endpoint is not None)
