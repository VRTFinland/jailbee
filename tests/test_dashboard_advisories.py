"""RepoAdvisory gathering: owed upgrade actions and repo-scoped notices."""

from __future__ import annotations

from datetime import UTC, datetime

from jailbee import notices, upgrade
from jailbee.dashboard import advisories as adv
from jailbee.dashboard.model import RepoAdvisory, RepoGroup
from jailbee.upgrade import UpgradeNote

NOW = datetime(2026, 5, 19, 17, 25, tzinfo=UTC)
PREFIX = "alpha"


def _notes(mocker, *versions, actions=("apply",)):
    mocker.patch(
        "jailbee.upgrade.UPGRADE_NOTES",
        tuple(
            UpgradeNote(v, frozenset(actions), "reason " + ".".join(map(str, v))) for v in versions
        ),
    )


def _observe(session, version="1.7.0"):
    for action in upgrade.ACTIONS:
        upgrade.record(session, PREFIX, action, version, now=NOW)


def _advise(session, *, raised=(), files=(), version="1.7.5"):
    return adv.repo_advisories(
        session, PREFIX, version, now=NOW, raised=raised, config_files=set(files)
    )


def test_owed_apply_becomes_advisory(db_session, mocker):
    _notes(mocker, (1, 7, 3))
    _observe(db_session)

    (a,) = _advise(db_session)

    assert (a.key, a.scope, a.action, a.fingerprint) == ("apply", PREFIX, "apply", "1.7.3")
    assert a.summary == "`jb apply` owed (1 change)"
    marks = upgrade.load_or_backfill(db_session, PREFIX, "1.7.5", now=NOW)
    (item,) = upgrade.pending("1.7.5", marks).actions
    assert a.lines == tuple(upgrade.format_advice(upgrade.Pending((item,)), footer=False))


def test_dismissed_action_hidden_until_new_reason(db_session, mocker):
    _notes(mocker, (1, 7, 3))
    _observe(db_session)
    notices.save(db_session, "apply", PREFIX, fingerprint="1.7.3", version="1.7.5", now=NOW)
    assert _advise(db_session) == ()

    _notes(mocker, (1, 7, 3), (1, 7, 5))
    (a,) = _advise(db_session)
    assert a.fingerprint == "1.7.5"


def test_notice_scoped_to_repo_file_kept_other_dropped(db_session, mocker):
    _notes(mocker)
    repo_file, other = "/r/.jailbee/config.yaml", "/home/x/global.yaml"
    raised = [
        notices.Notice("legacy-chrome-block", repo_file, ("chrome in repo",)),
        notices.Notice("legacy-chrome-block", other, ("chrome in global",)),
        notices.Notice("legacy-chrome-block", repo_file, ("chrome in repo",)),
    ]

    result = _advise(db_session, raised=raised, files={repo_file})

    assert result == (
        RepoAdvisory("legacy-chrome-block", repo_file, ("chrome in repo",), "", "chrome in repo"),
    )


def test_dismissed_notice_hidden(db_session, mocker):
    _notes(mocker)
    notices.save(
        db_session, "legacy-chrome-block", "/r/c.yaml", fingerprint="", version="1", now=NOW
    )
    raised = [notices.Notice("legacy-chrome-block", "/r/c.yaml", ("x",))]

    assert _advise(db_session, raised=raised, files={"/r/c.yaml"}) == ()


def test_dismiss_advisory_stores_displayed_fingerprint(db_session, db_engine, mocker):
    _notes(mocker, (1, 7, 3), (1, 7, 5))
    _observe(db_session)
    mocker.patch.object(adv, "get_engine", return_value=db_engine)
    shown = RepoAdvisory("apply", PREFIX, ("x",), "1.7.3", "s", "apply")

    adv.dismiss_advisory(shown, version="1.7.5", now=NOW)

    db_session.expire_all()
    (a,) = _advise(db_session)
    assert a.key == "apply" and a.fingerprint == "1.7.5"


def test_upgrade_counts():
    def g(*actions):
        return RepoGroup(
            "p",
            None,
            None,
            [],
            advisories=tuple(RepoAdvisory(a or "n", "s", (), "", "s", a) for a in actions),
        )

    groups = [g("apply", "base_build"), g("apply"), g(None), g()]
    assert adv.upgrade_counts(groups) == (1, 2)
