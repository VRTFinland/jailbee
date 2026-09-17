"""The dismissal store and the deprecation-notice family."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlmodel import Session

NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_caches():
    """Every test in this file drives the process caches directly."""
    from jailbee import notices

    notices.reset_caches()
    yield
    notices.reset_caches()


def _notice(key: str = "legacy-chrome-block", scope: str = "/etc/x/config.yaml"):
    from jailbee.notices import Notice

    return Notice(key=key, scope=scope, lines=("`chrome:` is deprecated.",))


def _dismissal(key: str, scope: str, *, fingerprint: str = "", version: str = "1.3.2"):
    from jailbee.notices import Dismissal

    return Dismissal(
        key=key,
        scope=scope,
        fingerprint=fingerprint,
        version=version,
        dismissed_at=NOW,
    )


def test_save_then_load_all_round_trips(db_engine) -> None:
    from jailbee import notices

    with Session(db_engine) as s:
        notices.save(s, "apply", "myrepo", fingerprint="1.3.2", version="1.3.4", now=NOW)
    with Session(db_engine) as s:
        stored = notices.load_all(s)
    assert set(stored) == {("apply", "myrepo")}
    entry = stored[("apply", "myrepo")]
    assert (entry.fingerprint, entry.version, entry.dismissed_at) == ("1.3.2", "1.3.4", NOW)


def test_save_is_idempotent_and_overwrites(db_engine) -> None:
    """Dismissing the same key twice updates the row rather than raising on the
    primary key — a user who dismisses `apply` again after a new reason
    appeared is acknowledging the newer fingerprint."""
    from jailbee import notices

    with Session(db_engine) as s:
        notices.save(s, "apply", "myrepo", fingerprint="1.2.0", version="1.2.0", now=NOW)
        notices.save(s, "apply", "myrepo", fingerprint="1.3.2", version="1.3.4", now=NOW)
    with Session(db_engine) as s:
        stored = notices.load_all(s)
    assert len(stored) == 1
    assert stored[("apply", "myrepo")].fingerprint == "1.3.2"


def test_drop_removes_the_row_and_reports_whether_it_existed(db_engine) -> None:
    from jailbee import notices

    with Session(db_engine) as s:
        notices.save(s, "apply", "myrepo", fingerprint="1.3.2", version="1.3.2", now=NOW)
        assert notices.drop(s, "apply", "myrepo") is True
        assert notices.drop(s, "apply", "myrepo") is False
        assert notices.load_all(s) == {}


def test_emit_prints_the_notice_and_the_dismiss_footer(capsys, monkeypatch) -> None:
    """The footer is what makes the feature discoverable; without it nobody
    learns the key."""
    from jailbee import notices

    monkeypatch.setattr(notices, "dismissals", lambda: {})
    notices.emit(_notice())
    err = capsys.readouterr().err
    assert "`chrome:` is deprecated." in err
    assert "jb dismiss legacy-chrome-block" in err


def test_emit_is_silent_when_dismissed(capsys, monkeypatch) -> None:
    from jailbee import notices

    ident = ("legacy-chrome-block", "/etc/x/config.yaml")
    monkeypatch.setattr(notices, "dismissals", lambda: {ident: _dismissal(*ident)})
    notices.emit(_notice())
    assert capsys.readouterr().err == ""


def test_emit_registers_the_notice_even_when_it_is_suppressed(monkeypatch) -> None:
    """`jailbee dismiss` and `jailbee doctor` both read `active()` to learn
    which notices apply to this process. A suppressed notice still applies —
    leaving it out would make a dismissal invisible to the very command that
    must still report it."""
    from jailbee import notices

    ident = ("legacy-chrome-block", "/etc/x/config.yaml")
    monkeypatch.setattr(notices, "dismissals", lambda: {ident: _dismissal(*ident)})
    notices.emit(_notice())
    assert [n.ident for n in notices.active()] == [ident]


def test_dismissals_survives_an_unreadable_state_db(monkeypatch) -> None:
    """Config loading calls this on every command. A locked or corrupt state DB
    must degrade to "nothing is dismissed", never to a traceback."""
    from jailbee import notices

    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr("jailbee.db.get_engine", boom)
    notices.reset_caches()
    assert notices.dismissals() == {}


def test_dismissals_reads_the_database_once_per_process(monkeypatch, db_engine) -> None:
    """`jailbee dashboard` reloads the config on every refresh tick; without the
    cache each tick would open the state DB again."""
    from jailbee import notices

    calls: list[int] = []

    def counted_engine():
        calls.append(1)
        return db_engine

    monkeypatch.setattr("jailbee.db.get_engine", counted_engine)
    notices.reset_caches()
    notices.dismissals()
    notices.dismissals()
    assert len(calls) == 1
