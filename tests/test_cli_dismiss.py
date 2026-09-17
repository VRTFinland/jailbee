"""`jailbee dismiss` — marking advisory warnings read."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlmodel import Session
from typer.testing import CliRunner

from jailbee.cli import app

NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_caches():
    from jailbee import notices

    notices.reset_caches()
    yield
    notices.reset_caches()


def _rows(cfg, session, version="1.3.2"):
    from jailbee.dismiss_command import survey

    return survey(cfg, session, version, now=NOW)


def test_survey_reports_a_deprecation_notice_this_process_found(db_engine, tmp_path) -> None:
    """The deprecation half of the survey comes from `notices.active()`, which
    the caller's own config load fills — so a notice emitted before the survey
    is what `jailbee dismiss` can offer."""
    from jailbee import notices
    from jailbee.notices import Notice
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    notices.emit(Notice(key="legacy-chrome-block", scope="/g/global.yaml", lines=("x",)))
    with Session(db_engine) as session:
        rows = _rows(cfg, session)
    hit = [r for r in rows if r.key == "legacy-chrome-block"]
    assert len(hit) == 1
    assert (hit[0].scope, hit[0].applies, hit[0].dismissal) == ("/g/global.yaml", True, None)


def test_survey_marks_a_stored_dismissal_that_no_longer_applies(db_engine, tmp_path) -> None:
    """A dismissal whose notice stopped applying — the config was fixed — is
    still listed, so `--clear` can reach it and the status view can say why it
    is there."""
    from jailbee import notices
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    with Session(db_engine) as session:
        notices.save(
            session, "legacy-chrome-block", "/gone.yaml", fingerprint="", version="1.3.2", now=NOW
        )
        rows = _rows(cfg, session)
    stale = [r for r in rows if r.scope == "/gone.yaml"]
    assert len(stale) == 1
    assert stale[0].applies is False
    assert stale[0].lines == ()


def test_apply_dismissals_records_the_fingerprint_of_what_was_shown(db_engine) -> None:
    from jailbee import notices
    from jailbee.dismiss_command import Row, apply_dismissals

    row = Row(
        key="apply",
        scope="myrepo",
        lines=("...",),
        fingerprint="1.3.2",
        dismissal=None,
        applies=True,
    )
    with Session(db_engine) as session:
        apply_dismissals(session, [row], "1.4.0", now=NOW)
        stored = notices.load_all(session)
    entry = stored[("apply", "myrepo")]
    # The fingerprint is the highest reason shown; `version` is merely when.
    assert (entry.fingerprint, entry.version) == ("1.3.2", "1.4.0")


def test_resolve_rejects_a_key_that_is_not_showing() -> None:
    from jailbee.dismiss_command import Row, resolve

    rows = [
        Row(
            key="apply", scope="myrepo", lines=(), fingerprint="1.3.2", dismissal=None, applies=True
        )
    ]
    matched, unknown = resolve(rows, ["base-build"])
    assert matched == []
    assert unknown == ["base-build"]


def test_resolve_accepts_key_at_scope_for_disambiguation() -> None:
    """Two files can raise the same notice; `key@scope` picks one."""
    from jailbee.dismiss_command import Row, resolve

    a = Row(
        key="legacy-chrome-block",
        scope="/g/global.yaml",
        lines=(),
        fingerprint="",
        dismissal=None,
        applies=True,
    )
    b = Row(
        key="legacy-chrome-block",
        scope="/r/config.yaml",
        lines=(),
        fingerprint="",
        dismissal=None,
        applies=True,
    )
    matched, unknown = resolve([a, b], ["legacy-chrome-block@/g/global.yaml"])
    assert matched == [a]
    assert unknown == []


def test_resolve_takes_every_scope_for_a_bare_key() -> None:
    from jailbee.dismiss_command import Row, resolve

    a = Row(
        key="legacy-chrome-block",
        scope="/g/global.yaml",
        lines=(),
        fingerprint="",
        dismissal=None,
        applies=True,
    )
    b = Row(
        key="legacy-chrome-block",
        scope="/r/config.yaml",
        lines=(),
        fingerprint="",
        dismissal=None,
        applies=True,
    )
    matched, _ = resolve([a, b], ["legacy-chrome-block"])
    assert matched == [a, b]


def test_resolve_ignores_a_stale_row_unless_clearing() -> None:
    """Dismissing targets only what applies — recording a dismissal for an
    advisory the user has not seen would hide its first appearance. `--clear`
    passes `include_stale` so a leftover row can still be removed."""
    from jailbee.dismiss_command import Row, resolve
    from jailbee.notices import Dismissal

    dismissal = Dismissal(
        key="legacy-chrome-block",
        scope="/g/global.yaml",
        fingerprint="",
        version="1.3.2",
        dismissed_at=NOW,
    )
    stale = Row(
        key="legacy-chrome-block",
        scope="/g/global.yaml",
        lines=(),
        fingerprint="",
        dismissal=dismissal,
        applies=False,
    )

    assert resolve([stale], ["legacy-chrome-block"]) == ([], ["legacy-chrome-block"])
    assert resolve([stale], ["legacy-chrome-block"], include_stale=True) == ([stale], [])


def test_render_says_doctor_still_reports_them() -> None:
    from jailbee.dismiss_command import Row, render

    rows = [
        Row(
            key="apply",
            scope="myrepo",
            lines=(),
            fingerprint="1.3.2",
            dismissal=None,
            applies=True,
        )
    ]
    assert any("doctor" in line for line in render(rows))


def test_render_distinguishes_a_stale_dismissal() -> None:
    from jailbee.dismiss_command import Row, render
    from jailbee.notices import Dismissal

    rows = [
        Row(
            key="legacy-chrome-block",
            scope="/g/global.yaml",
            lines=(),
            fingerprint="",
            dismissal=Dismissal(
                key="legacy-chrome-block",
                scope="/g/global.yaml",
                fingerprint="",
                version="1.3.2",
                dismissed_at=NOW,
            ),
            applies=False,
        )
    ]
    assert any("no longer applies" in line for line in render(rows))


def test_cli_rejects_an_unknown_key(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["dismiss", "not-a-key"])
    combined = (result.output or "") + (result.stderr or "")
    assert result.exit_code == 2
    assert "not-a-key" in combined


def test_cli_dismiss_says_doctor_still_reports_it(tmp_path, monkeypatch) -> None:
    """The user learns the model from the command itself: it must say where the
    warning went, not just that it went."""
    from jailbee import notices
    from jailbee.notices import Notice

    monkeypatch.chdir(tmp_path)
    scope = str(tmp_path / "c.yaml")
    notices.emit(Notice(key="legacy-chrome-block", scope=scope, lines=("x",)))
    result = CliRunner().invoke(app, ["dismiss", "legacy-chrome-block"])
    combined = (result.output or "") + (result.stderr or "")
    assert result.exit_code == 0, combined
    assert "doctor" in combined
    assert "new reason" in combined


def test_cli_dismiss_then_clear_round_trips(tmp_path, monkeypatch) -> None:
    """The recorded state, not the rendered table: Rich wraps the status view
    to the terminal width, and a `tmp_path` scope is long enough to split a
    status across two lines. `render`'s wording is asserted directly above."""
    from sqlmodel import Session as _Session

    from jailbee import notices
    from jailbee.db import get_engine
    from jailbee.notices import Notice

    monkeypatch.chdir(tmp_path)
    scope = str(tmp_path / "c.yaml")

    def stored():
        with _Session(get_engine()) as session:
            return notices.load_all(session)

    def emit_again():
        notices.reset_caches()
        notices.emit(Notice(key="legacy-chrome-block", scope=scope, lines=("x",)))

    emit_again()
    assert CliRunner().invoke(app, ["dismiss", "legacy-chrome-block"]).exit_code == 0
    assert set(stored()) == {("legacy-chrome-block", scope)}

    emit_again()
    cleared = CliRunner().invoke(app, ["dismiss", "--clear", "legacy-chrome-block"])
    assert cleared.exit_code == 0
    assert stored() == {}


def test_cli_rejects_keys_and_all_together(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["dismiss", "apply", "--all"])
    assert result.exit_code == 2
