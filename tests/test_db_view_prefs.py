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
    assert state.show_empty_repos is True
    assert state.hidden_repos == frozenset()


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
    save_view_state(
        engine,
        FRONTEND_TUI,
        ViewState(
            columns=("name",),
            folded=frozenset({"a"}),
            show_empty_repos=False,
            hidden_repos=frozenset({"alpha"}),
        ),
    )
    save_view_state(
        engine,
        FRONTEND_QT,
        ViewState(
            columns=("name", "ip"),
            folded=frozenset(),
            show_empty_repos=False,
            hidden_repos=frozenset({"beta"}),
        ),
    )

    assert load_view_state(engine, FRONTEND_TUI).columns == ("name",)
    assert load_view_state(engine, FRONTEND_TUI).folded == frozenset({"a"})
    assert load_view_state(engine, FRONTEND_QT).columns == ("name", "ip")
    assert load_view_state(engine, FRONTEND_QT).folded == frozenset()
    assert load_view_state(engine, FRONTEND_TUI).show_empty_repos is False
    assert load_view_state(engine, FRONTEND_TUI).hidden_repos == frozenset({"alpha"})
    assert load_view_state(engine, FRONTEND_QT).show_empty_repos is False
    assert load_view_state(engine, FRONTEND_QT).hidden_repos == frozenset({"beta"})


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


def test_malformed_hidden_repos_degrades_without_losing_other_preferences() -> None:
    from sqlmodel import Session

    from jailbee.db.models import ViewPrefs
    from jailbee.db.view_prefs import FRONTEND_TUI, load_view_state

    engine = _engine()
    with Session(engine) as session:
        session.add(
            ViewPrefs(
                frontend=FRONTEND_TUI,
                columns='["name"]',
                folded_repos='["folded"]',
                show_empty_repos=False,
                hidden_repos='[1, {"bad": true}]',
            )
        )
        session.commit()

    state = load_view_state(engine, FRONTEND_TUI)
    assert state.hidden_repos == frozenset()
    assert state.columns == ("name",)
    assert state.folded == frozenset({"folded"})
    assert state.show_empty_repos is False


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


def test_show_details_defaults_true_and_round_trips() -> None:
    from jailbee.db.view_prefs import FRONTEND_TUI, ViewState, load_view_state, save_view_state

    engine = _engine()
    assert load_view_state(engine, FRONTEND_TUI).show_details is True
    save_view_state(engine, FRONTEND_TUI, ViewState(show_details=False))
    assert load_view_state(engine, FRONTEND_TUI).show_details is False
    save_view_state(engine, FRONTEND_TUI, ViewState(show_details=True))
    assert load_view_state(engine, FRONTEND_TUI).show_details is True


def test_columns_version_defaults_to_zero_and_round_trips() -> None:
    from jailbee.db.view_prefs import FRONTEND_TUI, ViewState, load_view_state, save_view_state

    engine = _engine()
    assert load_view_state(engine, FRONTEND_TUI).columns_version == 0
    save_view_state(engine, FRONTEND_TUI, ViewState(columns=("name",), columns_version=1))
    assert load_view_state(engine, FRONTEND_TUI).columns_version == 1


def test_a_save_without_a_version_never_lowers_the_stored_one() -> None:
    """Both front-ends build the `ViewState` they save from their own fields
    (version 0); folding a repo must not undo a column migration."""
    from jailbee.db.view_prefs import FRONTEND_QT, ViewState, load_view_state, save_view_state

    engine = _engine()
    save_view_state(engine, FRONTEND_QT, ViewState(columns=("name",), columns_version=1))
    save_view_state(engine, FRONTEND_QT, ViewState(columns=("name",), folded=frozenset({"a"})))

    state = load_view_state(engine, FRONTEND_QT)
    assert state.columns_version == 1
    assert state.folded == frozenset({"a"})


def test_sort_defaults_to_none_and_round_trips() -> None:
    from jailbee.db.view_prefs import FRONTEND_TUI, ViewState, load_view_state, save_view_state

    engine = _engine()
    state = load_view_state(engine, FRONTEND_TUI)
    assert (state.sort_field, state.sort_desc) == (None, False)
    save_view_state(engine, FRONTEND_TUI, ViewState(sort_field="cpu", sort_desc=True))
    state = load_view_state(engine, FRONTEND_TUI)
    assert (state.sort_field, state.sort_desc) == ("cpu", True)
    save_view_state(engine, FRONTEND_TUI, ViewState())
    state = load_view_state(engine, FRONTEND_TUI)
    assert (state.sort_field, state.sort_desc) == (None, False)


def test_stored_column_order_round_trips() -> None:
    from jailbee.db.view_prefs import FRONTEND_QT, ViewState, load_view_state, save_view_state

    engine = _engine()
    save_view_state(engine, FRONTEND_QT, ViewState(columns=("name", "cpu", "state")))
    assert load_view_state(engine, FRONTEND_QT).columns == ("name", "cpu", "state")
