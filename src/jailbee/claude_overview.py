"""Every Claude login on this host, and who reads it.

`jailbee claude ls` used to be a *holder* view: one live row — the calling
repo's — plus the host-wide parked store. Which account a credential group
held was answerable only by naming that group with ``-g``, and a group
reachable only as one container's temporary override (`claude_groups`) was
not discoverable at all. This module answers the whole question at once.

**A row is a login file, not a group.** Three shapes, told apart by
``Row.state``:

- ``live`` — a holder's ``.credentials.json``. ``group`` names the credential
  group, or is ``None`` for a repo's own config home, whose repo ``prefix``
  names. Those are separate holders per repo, never one shared "no group".
- ``empty`` — a credential group with no login in it. Worth a row: someone
  created it on purpose, and before this nothing said it was empty.
- ``parked`` — a file in the host-wide store, belonging to no holder and
  activatable into any of them.

Reads only. Nothing here writes a credential, and one ``incus list`` serves
every group — an unreachable daemon costs the container column, not the
listing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from jailbee import claude_groups, claude_pool

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.claude_pool import Slot
    from jailbee.config import Config
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus


@dataclass(frozen=True)
class Row:
    """One login on this host, and everyone that reads it.

    ``slot`` is the login itself and carries its name and path; it is ``None``
    only for an ``empty`` group. ``repos`` are the repos resolving to this
    holder from ``global.yaml``, ``containers`` the containers reading it —
    including ones that got there through a temporary override, which is the
    only evidence a group no repo resolves to exists at all.
    """

    slot: Slot | None
    group: str | None
    prefix: str | None
    holder: Path | None
    repos: tuple[str, ...] = ()
    containers: tuple[str, ...] = ()
    mine: bool = False

    @property
    def state(self) -> str:
        """``live``, ``parked`` or ``empty``."""
        if self.slot is None:
            return "empty"
        return "live" if self.slot.live else "parked"

    @property
    def name(self) -> str | None:
        """The slot name — the reference `claude use` and `claude rm` take."""
        return None if self.slot is None else self.slot.name

    @property
    def account(self) -> str | None:
        """The account as it reads in a table, or None for an empty group."""
        return None if self.slot is None else self.slot.display_name

    @property
    def org_hint(self) -> str | None:
        return None if self.slot is None else self.slot.org_hint


@dataclass(frozen=True)
class Overview:
    """Every row, plus what could not be read while building them."""

    rows: tuple[Row, ...]
    unreachable: tuple[str, ...]
    """Registered repos whose config would not load — their holders may be missing."""
    containers_known: bool
    """False when `incus list` failed; every `Row.containers` is then empty."""


def _config_homes(cfg: Config) -> tuple[dict[str, Path], list[str]]:
    """The Claude config home of every registered repo, and the unreadable ones.

    The calling repo is included whether or not it is registered: it is the
    one repo whose config we already hold, and a table that omitted the
    caller's own holder would answer the wrong question.
    """
    from jailbee.config import load_repo_config

    homes: dict[str, Path] = {}
    unreachable: list[str] = []
    for prefix, repo_root in claude_pool.registered_repos():
        if prefix == cfg.container_prefix:
            continue
        if not repo_root.is_dir():
            # A registration whose directory is gone, treated as `members`
            # treats it: named rather than skipped, because its holder is
            # unknowable and silence would read as "no such repo".
            unreachable.append(prefix)
            continue
        try:
            homes[prefix] = claude_pool.config_home(load_repo_config(repo_root))
        except Exception:  # ConfigError, OSError, YAML/Pydantic — all mean "unreadable"
            unreachable.append(prefix)
    homes[cfg.container_prefix] = claude_pool.config_home(cfg)
    return homes, sorted(unreachable)


def _resolved_group(gcfg: GlobalConfig, prefix: str) -> str | None:
    resolved = gcfg.claude_credentials.dir_for(prefix)
    return None if resolved is None else resolved.name


def _existing_group_dirs() -> list[str]:
    """Group directories in the credential store, `_parked` and friends aside."""
    root = claude_groups.group_dir("x").parent
    try:
        return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("_"))
    except OSError:
        return []


def _account_at(
    holder: Path,
    homes: dict[str, Path],
    by_prefix: dict[str, set[str | None]],
    *,
    group: str | None,
    member_prefixes: list[str],
    prefer: str,
) -> claude_pool.Identity | None:
    """Which account `holder`'s login belongs to, or None when nothing says.

    Authoritativeness is `claude_groups.authoritative_in`'s rule and only its
    rule, asked of the one `groups_by_prefix_from` mapping `build` computes
    for the whole host — including for an ungrouped holder, where the answer
    being sought is "no group".
    """
    members = [claude_pool.Member(p, homes[p]) for p in member_prefixes]
    # Not intersected with `member_prefixes`: `_member_account` already keeps
    # only the members it was handed, and a second filter here would look like
    # it guarded a case that one does not.
    authoritative = claude_groups.authoritative_in(by_prefix, group)
    account = claude_pool.account_of(holder, members, prefer=prefer, authoritative=authoritative)
    return None if account is None else account.identity


def build(cfg: Config, gcfg: GlobalConfig, incus: Incus) -> Overview:
    """Every login on this host, from the point of view of `cfg`'s repo.

    `cfg` must be the repo's own config, never a `-g` holder view: the view
    keeps the calling repo's config home while pointing elsewhere, and this
    reads both. Filtering to one group is the caller's job.
    """
    from jailbee.incus import IncusError

    homes, unreachable = _config_homes(cfg)
    prefixes = sorted(homes)
    try:
        raw = incus.list_containers()
        containers_known = True
    except (IncusError, OSError):
        # Best-effort: a listing must still list. Every container column goes
        # empty, and `containers_known` tells the caller to say so.
        raw, containers_known = [], False
    triples = claude_groups.container_groups(gcfg, raw, prefixes)
    by_prefix = claude_groups.groups_by_prefix_from(gcfg, raw, prefixes)

    mine = claude_pool.holder_dir(cfg)
    parked = claude_pool.parked_slots()
    taken = {s.name for s in parked}
    rows: list[Row] = []

    group_names = sorted(
        {*_existing_group_dirs()}
        | {g for _, _, g in triples if g is not None}
        | {g for p in prefixes if (g := _resolved_group(gcfg, p)) is not None}
    )
    for group in group_names:
        holder = claude_groups.group_dir(group)
        member_prefixes = [p for p in prefixes if _resolved_group(gcfg, p) == group]
        identity = _account_at(
            holder,
            homes,
            by_prefix,
            group=group,
            member_prefixes=member_prefixes,
            prefer=cfg.container_prefix,
        )
        rows.append(
            Row(
                slot=_named(claude_pool.live_slot_at(holder, identity), taken),
                group=group,
                prefix=None,
                holder=holder,
                repos=tuple(member_prefixes),
                containers=tuple(c for c, _, g in triples if g == group),
                mine=holder == mine,
            )
        )

    for prefix in prefixes:
        if _resolved_group(gcfg, prefix) is not None:
            continue
        holder = homes[prefix]
        slot = _named(
            claude_pool.live_slot_at(
                holder,
                _account_at(
                    holder,
                    homes,
                    by_prefix,
                    group=None,
                    member_prefixes=[prefix],
                    prefer=prefix,
                ),
            ),
            taken,
        )
        # An empty row per registered repo would bury the table, so an
        # ungrouped holder earns one only by holding a login — except the
        # caller's, which is what the reader came to see.
        if slot is None and holder != mine:
            continue
        rows.append(
            Row(
                slot=slot,
                group=None,
                prefix=prefix,
                holder=holder,
                repos=(prefix,),
                containers=tuple(c for c, p, g in triples if g is None and p == prefix),
                mine=holder == mine,
            )
        )

    rows.extend(Row(slot=s, group=None, prefix=None, holder=None) for s in parked)
    return Overview(
        rows=tuple(sorted(rows, key=_order)),
        unreachable=tuple(unreachable),
        containers_known=containers_known,
    )


def _named(slot: Slot | None, taken: set[str]) -> Slot | None:
    """`slot`, renamed `~live` when the store already holds that name.

    The rule `claude_pool._slots_for` applies inside one holder, applied here
    across the host for the same reason: one account can legitimately hold two
    grants, and the name in this table is the reference `claude use` takes.
    """
    from dataclasses import replace

    from jailbee.claude_pool import DISAMBIGUATOR

    if slot is None or slot.name not in taken:
        return slot
    return replace(slot, name=f"{slot.name}{DISAMBIGUATOR}live")


_STATE_ORDER = {"live": 0, "empty": 1, "parked": 2}


def _order(row: Row) -> tuple[int, int, str]:
    """Live holders first, then empty groups, then the parked store.

    Within the live block, named groups come before the ungrouped holders:
    a group is shared and a config home is one repo's, so the shared ones
    read first.
    """
    return (
        _STATE_ORDER[row.state],
        0 if row.group is not None else 1,
        row.group or row.prefix or (row.name or ""),
    )
