"""Load/save of one dashboard front-end's persisted view state.

Kept separate from ``db/__init__.py`` (engine + schema) and from
``db/gui_state.py`` (Qt widget state) so both front-ends have one small,
testable surface for the state they actually share a *shape* with but never
a *value*: the TUI and the Qt dashboard keep independent rows.

Every decode here degrades rather than raises. View state is a personal
display preference; a corrupted or hand-edited value must never be able to
break the dashboard that reads it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlmodel import Session

from jailbee.db.models import ViewPrefs

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

FRONTEND_TUI = "tui"
FRONTEND_QT = "qt"


@dataclass(frozen=True)
class ViewState:
    """One front-end's view state, decoded.

    ``columns`` is ``None`` when nothing is stored, meaning "use the built-in
    default set" — distinct from an empty selection, which is not a
    representable request (see :func:`decode_names`).
    """

    columns: tuple[str, ...] | None = None
    folded: frozenset[str] = field(default_factory=frozenset)


def decode_names(raw: str | None) -> tuple[str, ...] | None:
    """A stored JSON name list, or ``None`` if there is nothing usable.

    ``None`` covers absent, unparseable, not-a-list, and emptied-by-filtering:
    there is no such thing as a table with zero columns, so an empty list is
    presumed to be a mistake rather than a request.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        # RecursionError is raised by json.loads on deeply nested input and does
        # not descend from ValueError, so it must be named explicitly.
        return None
    if not isinstance(data, list):
        return None
    names = tuple(x for x in data if isinstance(x, str))
    return names or None


def _decode_folded(raw: str | None) -> frozenset[str]:
    """A stored JSON prefix list as a set; empty for anything unusable.

    Unlike :func:`decode_names`, an empty set is a perfectly ordinary value
    ("nothing folded"), so there is no None case to distinguish.
    """
    names = decode_names(raw)
    return frozenset(names) if names is not None else frozenset()


def load_view_state(engine: Engine, frontend: str) -> ViewState:
    """Return ``frontend``'s stored view state, or empty defaults if none."""
    with Session(engine) as session:
        row = session.get(ViewPrefs, frontend)
        if row is None:
            return ViewState()
        return ViewState(columns=decode_names(row.columns), folded=_decode_folded(row.folded_repos))


def save_view_state(engine: Engine, frontend: str, state: ViewState) -> None:
    """Upsert ``frontend``'s row.

    ``folded`` is written sorted so the stored text is stable across saves
    that did not change anything — a set has no order, and churning the
    column on every write would make the row's history useless.
    """
    with Session(engine) as session:
        row = session.get(ViewPrefs, frontend)
        if row is None:
            row = ViewPrefs(frontend=frontend)
            session.add(row)
        row.columns = None if state.columns is None else json.dumps(list(state.columns))
        row.folded_repos = json.dumps(sorted(state.folded))
        session.commit()
