"""Tests for load/save of the Qt dashboard's persisted GUI state."""

from __future__ import annotations

from sqlmodel import SQLModel, create_engine


def _engine():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return engine


def test_load_returns_default_when_absent() -> None:
    from jailbee.db.gui_state import load_gui_state

    state = load_gui_state(_engine())
    assert state.layout == "cards"
    assert state.table_header_state is None
    assert state.refresh_interval is None
    assert state.refresh_paused is False


def test_save_then_load_round_trips() -> None:
    from jailbee.db.gui_state import load_gui_state, save_gui_state
    from jailbee.db.models import GuiState

    engine = _engine()
    save_gui_state(
        engine,
        GuiState(
            id=1,
            layout="table",
            table_header_state="Zm9v",
            refresh_interval=5.0,
            refresh_paused=True,
        ),
    )
    state = load_gui_state(engine)
    assert state.layout == "table"
    assert state.table_header_state == "Zm9v"
    assert state.refresh_interval == 5.0
    assert state.refresh_paused is True


def test_save_is_upsert_not_duplicate() -> None:
    from sqlmodel import Session, select

    from jailbee.db.gui_state import save_gui_state
    from jailbee.db.models import GuiState

    engine = _engine()
    save_gui_state(engine, GuiState(id=1, layout="cards"))
    save_gui_state(engine, GuiState(id=1, layout="table"))
    with Session(engine) as s:
        rows = s.exec(select(GuiState)).all()
    assert len(rows) == 1
    assert rows[0].layout == "table"


def test_card_style_and_collapsed_repos_round_trip() -> None:
    from jailbee.db.gui_state import load_gui_state, save_gui_state
    from jailbee.db.models import GuiState

    engine = _engine()
    save_gui_state(
        engine,
        GuiState(id=1, card_style="grid", collapsed_repos='["gisgro","other"]'),
    )
    loaded = load_gui_state(engine)

    assert loaded.card_style == "grid"
    assert loaded.collapsed_repos == '["gisgro","other"]'


def test_defaults_when_never_saved() -> None:
    from jailbee.db.gui_state import load_gui_state

    loaded = load_gui_state(_engine())
    assert loaded.card_style == "compact"
    assert loaded.collapsed_repos is None
