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
        ),
    )
    state = load_gui_state(engine)
    assert state.layout == "table"
    assert state.table_header_state == "Zm9v"


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


def test_card_style_round_trips() -> None:
    from jailbee.db.gui_state import load_gui_state, save_gui_state
    from jailbee.db.models import GuiState

    engine = _engine()
    save_gui_state(engine, GuiState(id=1, card_style="grid"))
    loaded = load_gui_state(engine)

    assert loaded.card_style == "grid"


def test_defaults_when_never_saved() -> None:
    from jailbee.db.gui_state import load_gui_state

    loaded = load_gui_state(_engine())
    assert loaded.card_style == "compact"


def test_saving_leaves_a_cadence_an_older_jailbee_persisted() -> None:
    """The cadence columns are unused now but kept for an older jailbee
    sharing the database: a save must not reset what it wrote."""
    from sqlmodel import Session

    from jailbee.db.gui_state import save_gui_state
    from jailbee.db.models import GuiState

    engine = _engine()
    with Session(engine) as s:
        s.add(GuiState(id=1, refresh_interval=7.0, refresh_paused=True))
        s.commit()
    save_gui_state(engine, GuiState(id=1, layout="table"))
    with Session(engine) as s:
        row = s.get(GuiState, 1)
        assert row is not None
        assert (row.layout, row.refresh_interval, row.refresh_paused) == ("table", 7.0, True)
