"""Upgrade and deprecation advisories as data, for the dashboards.

The CLI prints the same advice as hints (`upgrade.advice_lines`,
`notices.emit`); a dashboard cannot print, so the state service gathers it into
`RepoAdvisory` values on each `RepoGroup` and the front-ends render and dismiss
them. Frontend-agnostic: nothing here knows about Textual or Qt.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime as datetime_
from typing import TYPE_CHECKING

from jailbee import __version__, notices, upgrade
from jailbee.dashboard.model import RepoAdvisory
from jailbee.db import get_engine

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from datetime import datetime

    from sqlmodel import Session

    from jailbee.dashboard.model import RepoGroup


def repo_advisories(
    session: Session,
    prefix: str,
    version: str,
    *,
    now: datetime,
    raised: Sequence[notices.Notice],
    config_files: Collection[str],
) -> tuple[RepoAdvisory, ...]:
    """What one repo owes the user: owed upgrade actions, then its notices.

    `raised` are the notices the repo's config load emitted; only those naming
    one of `config_files` belong to this repo (the global file is shared by
    every repo and is not shown per repo). A dismissed notice is dropped.
    """
    out: list[RepoAdvisory] = []
    marks = upgrade.load_or_backfill(session, prefix, version, now=now)
    bounds = upgrade.dismissal_bounds(upgrade.load_dismissals(session, prefix))
    owed = upgrade.pending(version, marks, dismissals=bounds)
    for item in sorted(owed.actions, key=lambda i: upgrade.ACTIONS.index(i.action)):
        n = len(item.reasons)
        out.append(
            RepoAdvisory(
                key=upgrade.ACTION_KEYS[item.action],
                scope=prefix,
                lines=tuple(upgrade.format_advice(upgrade.Pending((item,)), footer=False)),
                fingerprint=upgrade.fingerprint(item),
                summary=(
                    f"`{upgrade.ACTION_COMMANDS[item.action]}` owed "
                    f"({n} change{'s' if n != 1 else ''})"
                ),
                action=item.action,
            )
        )
    dismissed = notices.load_all(session)
    seen: set[tuple[str, str]] = set()
    for notice in raised:
        if notice.scope not in config_files or notice.ident in dismissed or notice.ident in seen:
            continue
        seen.add(notice.ident)
        out.append(
            RepoAdvisory(
                key=notice.key,
                scope=notice.scope,
                lines=notice.lines,
                fingerprint="",
                summary=notice.lines[0],
            )
        )
    return tuple(out)


def dismiss_advisory(
    advisory: RepoAdvisory,
    *,
    version: str = __version__,
    now: datetime | None = None,
) -> None:
    """Mark `advisory` read, as `jb dismiss <key>` would.

    Stores the fingerprint the user *saw*, not one recomputed now: a release
    that added a reason since the snapshot was gathered must still speak.
    """
    from sqlmodel import Session

    with Session(get_engine()) as session:
        notices.save(
            session,
            advisory.key,
            advisory.scope,
            fingerprint=advisory.fingerprint,
            version=version,
            now=now if now is not None else datetime_.now(UTC),
        )
    notices.reset_caches()


def upgrade_counts(groups: Sequence[RepoGroup]) -> tuple[int, int]:
    """``(repos owing base build, repos owing apply)`` across `groups`."""
    base = sum(any(a.action == "base_build" for a in g.advisories) for g in groups)
    apply = sum(any(a.action == "apply" for a in g.advisories) for g in groups)
    return base, apply
