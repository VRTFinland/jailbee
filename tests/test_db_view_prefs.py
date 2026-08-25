"""Tests for load/save of a dashboard front-end's persisted view state."""

from __future__ import annotations

from sqlmodel import SQLModel, create_engine


def _engine():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return engine


def test_load_returns_empty_defaults_when_absent() -> None:
    from jailbee.db.view_prefs import FRONTEND_TUI, load_view_state

    state = load_view_state(_engine(), FRONTEND_TUI)
    assert state.columns is None  # None = built-in default set
    assert state.folded == frozenset()


def test_save_then_load_round_trips() -> None:
    from jailbee.db.view_prefs import FRONTEND_TUI, ViewState, load_view_state, save_view_state

    engine = _engine()
    save_view_state(
        engine, FRONTEND_TUI, ViewState(columns=("name", "state"), folded=frozenset({"alpha"}))
    )
    state = load_view_state(engine, FRONTEND_TUI)
    assert state.columns == ("name", "state")
    assert state.folded == frozenset({"alpha"})


def test_save_is_upsert_not_duplicate() -> None:
    from sqlmodel import Session, select

    from jailbee.db.models import ViewPrefs
    from jailbee.db.view_prefs import FRONTEND_TUI, ViewState, save_view_state

    engine = _engine()
    save_view_state(engine, FRONTEND_TUI, ViewState(columns=("name",)))
    save_view_state(engine, FRONTEND_TUI, ViewState(columns=("state",)))
    with Session(engine) as s:
        rows = s.exec(select(ViewPrefs)).all()
    assert len(rows) == 1
    assert rows[0].columns == '["state"]'


def test_the_two_frontends_do_not_share_state() -> None:
    from jailbee.db.view_prefs import (
        FRONTEND_QT,
        FRONTEND_TUI,
        ViewState,
        load_view_state,
        save_view_state,
    )

    engine = _engine()
    save_view_state(engine, FRONTEND_TUI, ViewState(columns=("name",), folded=frozenset({"a"})))
    save_view_state(engine, FRONTEND_QT, ViewState(columns=("name", "ip"), folded=frozenset()))

    assert load_view_state(engine, FRONTEND_TUI).columns == ("name",)
    assert load_view_state(engine, FRONTEND_TUI).folded == frozenset({"a"})
    assert load_view_state(engine, FRONTEND_QT).columns == ("name", "ip")
    assert load_view_state(engine, FRONTEND_QT).folded == frozenset()


def test_malformed_json_degrades_instead_of_raising() -> None:
    """View state must never be able to break the dashboard: a corrupted or
    hand-edited value reads as "nothing stored", not as an exception."""
    from sqlmodel import Session

    from jailbee.db.models import ViewPrefs
    from jailbee.db.view_prefs import FRONTEND_TUI, load_view_state

    engine = _engine()
    with Session(engine) as s:
        s.add(ViewPrefs(frontend="tui", columns="{not json", folded_repos='{"a": 1}'))
        s.commit()

    state = load_view_state(engine, FRONTEND_TUI)
    assert state.columns is None
    assert state.folded == frozenset()


def test_decode_names_drops_non_strings_and_empty_lists() -> None:
    """An empty list is not a real request for zero columns — there is no such
    thing as a table with no columns — so it reads as "use the default"."""
    from jailbee.db.view_prefs import decode_names

    assert decode_names(None) is None
    assert decode_names("") is None
    assert decode_names("[]") is None
    assert decode_names('["name", 3, "state"]') == ("name", "state")
    assert decode_names('"name"') is None


def test_decode_deeply_nested_json_degrades_instead_of_raising() -> None:
    """Deeply nested JSON raises RecursionError, which does not descend from
    ValueError. A hand-edited or corrupted row must not crash load_view_state."""
    from sqlmodel import Session

    from jailbee.db.models import ViewPrefs
    from jailbee.db.view_prefs import FRONTEND_TUI, decode_names, load_view_state

    # Direct decode: deeply nested JSON should return None, not raise
    deeply_nested = "[" * 100000 + "]" * 100000
    assert decode_names(deeply_nested) is None

    # Through load_view_state: corrupted row must not crash
    engine = _engine()
    with Session(engine) as s:
        s.add(ViewPrefs(frontend="tui", columns=deeply_nested, folded_repos="[]"))
        s.commit()

    state = load_view_state(engine, FRONTEND_TUI)
    assert state.columns is None
    assert state.folded == frozenset()
