"""Every Claude login on this host, and who reads it.

**This module is a binding, not an implementation.** The generic half —
one `Row` per (agent, holder), built from `engine.*` and `groups.*` — lives
in `accounts/overview.py`. `Row` and `Overview` are re-exported from there,
and `build` binds it to `CLAUDE`, so callers and tests keep the module they
have always used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jailbee.accounts import overview
from jailbee.accounts.adapters.claude import CLAUDE
from jailbee.accounts.overview import Overview as Overview
from jailbee.accounts.overview import Row as Row

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus


def build(cfg: Config, gcfg: GlobalConfig, incus: Incus) -> Overview:
    """Every login on this host, from the point of view of `cfg`'s repo.

    `cfg` must be the repo's own config, never a `-g` holder view — see
    `accounts.overview.build`.
    """
    return overview.build(CLAUDE, cfg, gcfg, incus)
