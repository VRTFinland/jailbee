"""TTL-driven auto-revert for `jailbee net loose` containers.

Called once per repo per ``jailbee-net-refresh.timer`` tick from
``egress_pool.refresh_all``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from jailbee.lifecycle import current_network_mode, switch_network
from jailbee.profiles import profile_names

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RevertResult:
    """Outcome of a single container's TTL check.

    ``reverted_to`` is ``None`` when no mode change was performed — either
    the labels were orphaned (cleaned only) or the switch failed (labels
    preserved for retry).
    """

    container: str
    reverted_to: str | None
    error: str | None = None


def check_and_revert_loose(
    cfg: Config,
    incus: Incus,
    *,
    now: datetime,
    mirror_endpoint: tuple[str, int] | None = None,
) -> list[RevertResult]:
    """Return containers whose loose TTL fired this cycle.

    Iterates every container carrying this repo's ``<prefix>-base`` profile
    and inspects its ``user.jailbee.loose_until`` label. See spec for the full
    decision matrix.

    The ``loose_auto_revert`` policy is deliberately *not* consulted here. It
    governs whether jailbee *schedules* a TTL of its own (``_switch`` writes no
    label when it is disabled), not whether an already-written label is
    honoured: a user who said ``jailbee net loose x --for 2h`` stated an intent
    that beats the config switch. Ignoring the label under a disabled policy
    would leave ``jailbee ls``/``jailbee net status`` counting down to ``0s`` on a
    container that never leaves loose — a lie in the unsafe direction.
    Containers with no label cost one ``config_get`` and are skipped.
    """
    prefix = cfg.container_prefix
    out: list[RevertResult] = []

    for raw in incus.list_containers():
        name = raw["name"]
        # A container mid-destroy can be reported with "profiles": null, which
        # bypasses the `.get(..., [])` default (key present, value None).
        profiles = raw.get("profiles") or []
        if f"{prefix}-base" not in profiles:
            continue

        try:
            if incus.config_get(name, "user.jailbee.autostart_in_progress"):
                continue

            loose_until = incus.config_get(name, "user.jailbee.loose_until")
            if not loose_until:
                continue

            try:
                until_ts = datetime.fromisoformat(loose_until)
            except ValueError:
                log.warning(
                    "loose_revert: %s — unparseable loose_until %r, clearing labels",
                    name,
                    loose_until,
                )
                incus.config_unset(name, "user.jailbee.loose_until")
                incus.config_unset(name, "user.jailbee.loose_revert_to")
                continue

            current = current_network_mode(cfg, incus, name)
            if current != "loose":
                # Orphan labels — user switched manually but cleanup
                # didn't run. Clean up the labels without touching the
                # network.
                incus.config_unset(name, "user.jailbee.loose_until")
                incus.config_unset(name, "user.jailbee.loose_revert_to")
                out.append(RevertResult(container=name, reverted_to=None))
                continue

            if until_ts > now:
                continue

            # Only a mode jailbee still supports may be reverted to. Anything
            # else — an unset label, or one written before a mode was
            # removed — falls back to strict rather than making
            # switch_network raise and the container retry every tick.
            recorded = incus.config_get(name, "user.jailbee.loose_revert_to")
            valid_modes = profile_names(cfg).net_by_mode
            revert_to = recorded if recorded in valid_modes else "strict"
            try:
                switch_network(
                    cfg,
                    incus,
                    name,
                    revert_to,
                    mirror_endpoint=mirror_endpoint,
                )
            except Exception as e:  # log + retry next cycle
                log.warning(
                    "loose_revert: failed to switch %s: %s",
                    name,
                    e,
                )
                out.append(
                    RevertResult(
                        container=name,
                        reverted_to=None,
                        error=str(e),
                    ),
                )
                continue

            incus.config_unset(name, "user.jailbee.loose_until")
            incus.config_unset(name, "user.jailbee.loose_revert_to")
            log.info("loose_revert: %s → %s (TTL expired)", name, revert_to)
            out.append(RevertResult(container=name, reverted_to=revert_to))
        except Exception as e:  # never let one container break the loop
            log.warning("loose_revert: %s raised: %s", name, e)
            out.append(
                RevertResult(
                    container=name,
                    reverted_to=None,
                    error=str(e),
                ),
            )

    return out
