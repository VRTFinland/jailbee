"""`jailbee dismiss` — marking repeating advisories read.

Surveys the two families that repeat on every command (the upgrade advice in
`jailbee.upgrade` and the deprecation notices in `jailbee.notices`), records a
dismissal for the ones named, and renders the status view. Writes go through
`notices.save` / `notices.drop`, which own the single shared table; nothing
here talks to SQLite directly.

Unlike its two read paths, this module does **not** swallow a state-DB error.
The advisory paths are a courtesy and must never fail the command the user
actually ran; here the command *is* writing that state, and a silent failure
would leave the user believing a warning was dismissed when it was not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from sqlmodel import Session

    from jailbee.config import Config
    from jailbee.notices import Dismissal


@dataclass(frozen=True)
class Row:
    """One advisory, and what is known about its dismissal.

    `applies` is False for a dismissal whose advisory has stopped applying —
    the config was fixed, or the action was finally run. Such a row is still
    listed so `--clear` can reach it and the status view can say why it is
    there; it is never a dismissal *target*, because recording a dismissal for
    an advisory the user has not seen would hide its first appearance.
    """

    key: str
    scope: str
    lines: tuple[str, ...]
    fingerprint: str
    dismissal: Dismissal | None
    applies: bool

    @property
    def qualified(self) -> str:
        """`key@scope` — how a key is named when it applies in several scopes."""
        return f"{self.key}@{self.scope}"


def survey(cfg: Config, session: Session, version: str, *, now: datetime) -> list[Row]:
    """Every advisory this process can speak about, dismissed or not.

    The deprecation half comes from `notices.active()`, which the caller's own
    config load populated — so `jailbee dismiss` must load the config before
    calling this (the CLI does anyway, for `container_prefix`). Upgrade advice
    is computed *unfiltered*: the status view's job is to show what would be
    hidden as much as what is.
    """
    from jailbee import notices, upgrade

    stored = notices.load_all(session)
    rows: list[Row] = []
    seen: set[tuple[str, str]] = set()

    marks = upgrade.load_or_backfill(session, cfg.container_prefix, version, now=now)
    for item in upgrade.pending(version, marks).actions:
        key = upgrade.ACTION_KEYS[item.action]
        ident = (key, cfg.container_prefix)
        seen.add(ident)
        rows.append(
            Row(
                key=key,
                scope=cfg.container_prefix,
                lines=tuple(upgrade.format_advice(upgrade.Pending((item,)))),
                # The highest firing note version, not the running jailbee
                # version: dismissing acknowledges the reasons shown *now*, so
                # a later release that adds one above this brings the advice
                # back. Storing the running version instead would also swallow
                # a reason added to that same release later.
                fingerprint=".".join(str(part) for part in item.releases[-1]),
                dismissal=stored.get(ident),
                applies=True,
            )
        )

    for notice in notices.active():
        seen.add(notice.ident)
        rows.append(
            Row(
                key=notice.key,
                scope=notice.scope,
                lines=notice.lines,
                fingerprint="",
                dismissal=stored.get(notice.ident),
                applies=True,
            )
        )

    for ident, entry in stored.items():
        if ident in seen:
            continue
        rows.append(
            Row(
                key=entry.key,
                scope=entry.scope,
                lines=(),
                fingerprint=entry.fingerprint,
                dismissal=entry,
                applies=False,
            )
        )
    return rows


def resolve(
    rows: list[Row], keys: list[str], *, include_stale: bool = False
) -> tuple[list[Row], list[str]]:
    """Match `keys` against `rows`, returning `(matched, unknown)`.

    A key is either bare — which takes every scope it applies in, since two
    files raising the same notice are usually one decision — or `key@scope`,
    which picks one. Only rows that apply are dismissal targets; `--clear`
    passes `include_stale` so a dismissal left behind by a fixed config can
    still be removed.
    """
    matched: list[Row] = []
    unknown: list[str] = []
    for raw in keys:
        key, _, scope = raw.partition("@")
        hits = [
            row
            for row in rows
            if row.key == key
            and (not scope or row.scope == scope)
            and (include_stale or row.applies)
        ]
        if not hits:
            unknown.append(raw)
            continue
        matched.extend(hit for hit in hits if hit not in matched)
    return matched, unknown


def apply_dismissals(session: Session, rows: list[Row], version: str, *, now: datetime) -> None:
    """Record a dismissal for each row."""
    from jailbee import notices

    for row in rows:
        notices.save(
            session,
            row.key,
            row.scope,
            fingerprint=row.fingerprint,
            version=version,
            now=now,
        )


def clear(session: Session, rows: list[Row]) -> int:
    """Remove the dismissals for `rows`. Returns how many were actually there."""
    from jailbee import notices

    return sum(1 for row in rows if notices.drop(session, row.key, row.scope))


def _display_scope(scope: str) -> str:
    """A scope as the status view shows it.

    A file scope goes through `display_path` — an absolute config path is most
    of a terminal line, and `~` is both shorter and the form the user would
    type back. A `container_prefix` is not a path and is shown verbatim.
    """
    from pathlib import Path

    from jailbee.paths import display_path

    return display_path(Path(scope)) if scope.startswith("/") else scope


def render(rows: list[Row]) -> list[str]:
    """The status view, as lines.

    Lines rather than a printed table so the wording is testable without
    capturing output — the same choice `upgrade.format_advice` makes.
    """
    if not rows:
        return ["Nothing to dismiss — no advisory warnings apply here."]
    scopes = {row.scope: _display_scope(row.scope) for row in rows}
    key_width = max(len(row.key) for row in rows)
    scope_width = max(len(scope) for scope in scopes.values())
    lines = ["Advisory warnings in this repo (`jailbee doctor` reports them either way):", ""]
    for row in sorted(rows, key=lambda r: (not r.applies, r.key, r.scope)):
        if row.dismissal is None:
            status = "showing"
        elif row.applies:
            status = f"dismissed at {row.dismissal.version}"
        else:
            status = f"dismissed at {row.dismissal.version} (no longer applies)"
        lines.append(
            f"  {row.key.ljust(key_width)}  {scopes[row.scope].ljust(scope_width)}  {status}"
        )
    return lines
