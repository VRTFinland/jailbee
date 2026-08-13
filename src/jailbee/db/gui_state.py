"""Load/save of the Qt dashboard's persisted GUI state (the gui_state row).

Kept separate from ``db/__init__.py`` (engine + schema) so the Qt app has a
small, testable surface: two functions over the single-row ``GuiState`` table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import Session

from jailbee.db.models import GuiState

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_GUI_STATE_ID = 1


def load_gui_state(engine: Engine) -> GuiState:
    """Return the persisted GUI state, or a default ``GuiState()`` if none.

    The returned object is detached from the session (its attributes are
    materialised before the session closes), so callers can read it freely.
    """
    with Session(engine) as session:
        row = session.get(GuiState, _GUI_STATE_ID)
        if row is None:
            return GuiState()
        return GuiState(
            id=row.id,
            layout=row.layout,
            table_header_state=row.table_header_state,
            refresh_interval=row.refresh_interval,
            refresh_paused=row.refresh_paused,
            card_style=row.card_style,
            collapsed_repos=row.collapsed_repos,
        )


def save_gui_state(engine: Engine, state: GuiState) -> None:
    """Upsert the single ``gui_state`` row (id=1)."""
    with Session(engine) as session:
        row = session.get(GuiState, _GUI_STATE_ID)
        if row is None:
            row = GuiState(id=_GUI_STATE_ID)
            session.add(row)
        row.layout = state.layout
        row.table_header_state = state.table_header_state
        row.refresh_interval = state.refresh_interval
        row.refresh_paused = state.refresh_paused
        row.card_style = state.card_style
        row.collapsed_repos = state.collapsed_repos
        session.commit()
