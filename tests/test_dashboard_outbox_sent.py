"""The Sent view of the terminal dashboard: rows, preview, incarnations, open-in-browser."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from jailbee.dashboard import actions as dact
from jailbee.dashboard import outbox as dob
from jailbee.dashboard.tui import session as tsession
from jailbee.dashboard.tui.outbox_box import OutboxBox
from jailbee.outbox import sent
from jailbee.outbox.sent import SentAction
from tests.dashboard_fixtures import cfg_group, ci
from tests.dashboard_pilot import bare_session
from tests.test_dashboard_session import _PLAN, _HeldJobs, _open_box, _quiet, _settle

T0 = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
CREATED = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
URL = "https://github.com/o/r/issues/5#issuecomment-1"


def _action(manifest: str, **kw: object) -> SentAction:
    base: dict[str, object] = {
        "outbox": "issue",
        "manifest": manifest,
        "index": 0,
        "kind": "comment",
        "repo": "o/r",
        "target": 5,
        "url": URL,
        "title": None,
        "content": "first line\nsecond line of the comment",
    }
    base.update(kw)
    return SentAction(**base)  # type: ignore[arg-type]


def _rec(
    manifest: str,
    *,
    minutes: int = 0,
    instance: str = "alpha-x",
    created: datetime | None = CREATED,
    state: str = "applied",
    publisher: str = "user",
    **kw: object,
) -> None:
    sent.record(
        instance=instance,
        container_prefix="alpha",
        container_created=created,
        publisher=publisher,  # type: ignore[arg-type]
        action=_action(manifest, **kw),
        state=state,  # type: ignore[arg-type]
        now=T0 + timedelta(minutes=minutes),
    )


def _group(tmp_path, *, created: datetime | None = CREATED, count: int = 2):  # type: ignore[no-untyped-def]
    info = replace(ci("alpha-x", "alpha"), created_at=created, sent_count=count)
    group = cfg_group(tmp_path, (info,))
    group.sent_count = count
    return group


def _state(session) -> dob.OutboxState:  # type: ignore[no-untyped-def]
    assert isinstance(session.overlay, dob.OutboxState)
    return session.overlay


def _texts(state: dob.OutboxState) -> list[str]:
    return [r.text.plain for r in dob.visible_rows(state, 200)]


def _open_repo_sent(session) -> None:  # type: ignore[no-untyped-def]
    session.handle_key("enter")  # the repo header's menu
    assert isinstance(session.overlay, tsession.RepoMenuState)
    session.menu_chosen(dact.REPO_SENT, None, 0)


def test_sent_rows_newest_first_with_columns_and_uncertain_mark(mocker, tmp_path):
    _rec("old", minutes=0, title="Old title")
    _rec("new", minutes=5, publisher="orchestrator", state="uncertain", target=None, repo="o/r")
    _quiet(mocker)
    session, _ = bare_session(mocker, [_group(tmp_path)])
    _open_box(session)

    state = _state(session)
    assert state.tab == "outbox"  # a proposal is pending: the Outbox tab stays first
    session.outbox_intent("tab", None)
    state = _state(session)
    assert state.tab == "sent"
    first, second = _texts(state)
    assert "11-04 10:05" in first and "orch" in first and "issue/comment" in first
    assert "first line" in first and "o/r " in first + " " and "uncertain" in first
    assert "11-04 10:00" in second and "user" in second and "o/r#5" in second
    assert "Old title" in second and "uncertain" not in second


def test_sent_preview_shows_full_content_and_url(mocker, tmp_path):
    _rec("m1")
    _rec("m2", minutes=1)
    _quiet(mocker)
    session, _ = bare_session(mocker, [_group(tmp_path)])
    _open_box(session)
    session.outbox_intent("tab", None)
    state = _state(session)

    text = "\n".join(p.plain for p in dob.preview_lines(state, state.sent_cursor, 80))

    assert "second line of the comment" in text and URL in text


def test_opens_on_sent_tab_when_nothing_is_pending(mocker, tmp_path):
    from tests.test_dashboard_outbox import _container, _listing

    _rec("m1")
    _quiet(mocker, _listing(_container()))
    session, _ = bare_session(mocker, [_group(tmp_path, count=1)])
    _open_box(session)

    assert _state(session).tab == "sent"


def test_tab_without_sent_records_is_a_status(mocker, tmp_path):
    _quiet(mocker)
    session, _ = bare_session(mocker, [_group(tmp_path, count=0)])
    _open_box(session)
    session.outbox_intent("tab", None)

    assert _state(session).tab == "outbox"
    assert "no Sent records" in _state(session).status[-1]


def test_publish_in_the_box_refreshes_the_sent_list(mocker, tmp_path):
    """The last proposal is published: the box lands on a Sent list that has the new record."""
    from jailbee.dashboard.jobs import JobResult
    from tests.test_dashboard_outbox import _container, _listing

    quiet = _quiet(mocker)
    session, _ = bare_session(mocker, [_group(tmp_path, count=0)])  # no records when opened
    jobs = _HeldJobs()
    session.jobs = jobs  # type: ignore[assignment]  # test double
    _open_box(session)
    assert _state(session).sent == () and _state(session).tab == "outbox"

    session.outbox_intent("publish", ("alpha-x", "pr/a.json", None, None))
    jobs.started[-1][2](JobResult(0, "", _PLAN))  # type: ignore[operator]  # the plan
    session.outbox_intent("answer", None, "publish")
    _rec("just-sent", content="published a moment ago")  # what the publish recorded
    quiet.return_value = tsession.da.CliResult(True, "done", _listing(_container()))
    jobs.started[-1][2](JobResult(0, "", "Published 1 action\n"))  # type: ignore[operator]

    state = _state(session)
    assert state.tab == "sent"
    assert any("published a moment ago" in t for t in _texts(state))


def test_refresh_on_the_outbox_tab_rereads_sent(mocker, tmp_path):
    _quiet(mocker)
    session, _ = bare_session(mocker, [_group(tmp_path, count=0)])
    _open_box(session)
    assert _state(session).sent == ()

    _rec("late")
    session.outbox_intent("refresh", None)
    _settle(session)

    assert len(_state(session).sent) == 1


def test_timeline_refresh_follows_the_live_containers(mocker, tmp_path):
    _rec("m1")
    _quiet(mocker)
    group = _group(tmp_path)
    session, _ = bare_session(mocker, [group])
    _open_repo_sent(session)
    assert "†" not in _texts(_state(session))[0]

    live = session.groups[0].containers  # the container was recreated since
    live[0] = replace(live[0], created_at=CREATED + timedelta(days=1))
    session.outbox_intent("refresh", None)

    assert "†" in _texts(_state(session))[0]


def _open(session, mocker, monkeypatch, *, display: str | None):  # type: ignore[no-untyped-def]
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    if display:
        monkeypatch.setenv("DISPLAY", display)
    opener = mocker.Mock()
    session.url_opener = opener
    _open_box(session)
    session.outbox_intent("tab", None)
    state = _state(session)
    session.outbox_intent("open", state.sent_cursor)
    return opener


def test_open_uses_the_opener_on_a_local_desktop(mocker, tmp_path, monkeypatch):
    _rec("m1")
    _quiet(mocker)
    session, _ = bare_session(mocker, [_group(tmp_path)])

    opener = _open(session, mocker, monkeypatch, display=":0")

    opener.assert_called_once_with(URL)


def test_open_only_shows_the_url_without_a_display(mocker, tmp_path, monkeypatch):
    _rec("m1")
    _quiet(mocker)
    session, _ = bare_session(mocker, [_group(tmp_path)])

    opener = _open(session, mocker, monkeypatch, display=None)

    opener.assert_not_called()
    assert session.notice is not None and URL in session.notice


def test_open_only_shows_the_url_over_remote_ssh(mocker, tmp_path, monkeypatch):
    from jailbee.config.models_remote import RemoteSSHConfig

    _rec("m1")
    _quiet(mocker)
    session, _ = bare_session(
        mocker,
        [_group(tmp_path)],
        over_ssh=True,
        remote=True,
        ssh_policy=RemoteSSHConfig(),
    )

    opener = _open(session, mocker, monkeypatch, display=":0")

    opener.assert_not_called()
    assert session.notice is not None and URL in session.notice


def test_incarnations_split_views(mocker, tmp_path):
    """A container destroyed and recreated under one name: tab = new, timeline = both."""
    old = CREATED
    new = CREATED + timedelta(days=2)
    _rec("m-old", minutes=0, created=old, content="from the old container")
    _rec("m-new", minutes=9, created=new, content="from the new container")
    _quiet(mocker)
    session, _ = bare_session(mocker, [_group(tmp_path, created=new, count=1)])
    _open_box(session)
    session.outbox_intent("tab", None)

    tab = _texts(_state(session))
    assert len(tab) == 1 and "from the new container" in tab[0] and "†" not in tab[0]

    session.overlay = None
    session.outbox = None
    session.outbox_parent = None
    session.overlay = session.open_sent("alpha")
    timeline = _texts(_state(session))
    assert len(timeline) == 2
    assert "from the new container" in timeline[0] and "†" not in timeline[0]
    assert "from the old container" in timeline[1] and "†" in timeline[1]
    assert all("  x  " in line for line in timeline)  # the container column
    final = _state(session)
    note = [p.plain for p in dob.preview_lines(final, dob.sent_key(final.sent[1]), 80)]
    assert any("earlier incarnation" in line for line in note)


def test_timeline_marks_destroyed_containers(mocker, tmp_path):
    _rec("m1", instance="alpha-gone", content="gone one")
    _rec("m2", minutes=1)
    _quiet(mocker)
    session, _ = bare_session(mocker, [_group(tmp_path)])

    _open_repo_sent(session)

    state = _state(session)
    assert state.sent_repo and state.tab == "sent"
    rows = _texts(state)
    assert "†" not in rows[0] and "†" in rows[1] and "  gone  " in rows[1]
    assert dob.find_sent(state, state.sent_cursor) is not None


def test_timeline_esc_returns_to_the_repo_menu(mocker, tmp_path):
    _rec("m1")
    _quiet(mocker)
    session, _ = bare_session(mocker, [_group(tmp_path)])
    _open_repo_sent(session)

    session.outbox_intent("cancel", None)

    assert isinstance(session.overlay, tsession.RepoMenuState)


def test_sent_hint_lists_the_new_keys():
    state = dob.OutboxState("alpha-x", (), tab="sent")
    assert "o open URL" in dob.outbox_hint(state, standalone=False)
    assert "Tab Outbox" in dob.outbox_hint(state, standalone=False)
    repo = replace(state, sent_repo=True)
    assert "Tab" not in dob.outbox_hint(repo, standalone=False)


def test_help_lists_sent_keys():
    from jailbee.dashboard.tui.frame import help_lines

    text = "\n".join(help_lines())
    assert "Tab switches Outbox/Sent" in text and "o opens the URL" in text


def test_box_tab_key_posts_intent_and_o_is_ignored_on_the_outbox_tab():
    from textual.app import App

    from tests.test_dashboard_outbox import _proposal, _state

    sent_state = replace(_state(_proposal("pr/a.json")), sent=(_item(),))

    posted: list[str] = []

    class Host(App[None]):
        def compose(self):  # type: ignore[no-untyped-def]
            yield OutboxBox(sent_state, mouse_enabled=lambda: True, standalone=True)

        def on_outbox_box_intent(self, message: OutboxBox.Intent) -> None:
            posted.append(message.name)

    got: list[tuple[str, object]] = []

    async def main() -> None:
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            box = app.query_one(OutboxBox)
            for name in ("o", "tab"):
                outcome = await box.handle_key(_key_event(name))
                got.append((name, getattr(outcome, "name", outcome)))
            await pilot.pause()

    asyncio.run(main())
    assert got == [("o", True), ("tab", "tab")]
    # `o` on the Outbox tab neither posted nor leaked a publish/delete/open intent.
    assert not {"open", "publish", "delete", "create-pr", "clear"} & set(posted)


def _key_event(name: str):  # type: ignore[no-untyped-def]
    from textual import events

    return events.Key(name, None)


def _item(**kw: object) -> dob.SentItem:
    base: dict[str, object] = {
        "id": 1,
        "sent_at": T0,
        "container_prefix": "alpha",
        "container": "x",
        "instance": "alpha-x",
        "container_created": CREATED,
        "publisher": "user",
        "state": "applied",
        "outbox": "issue",
        "manifest": "m",
        "index": 0,
        "kind": "comment",
        "repo": "o/r",
        "target": 5,
        "url": URL,
        "title": None,
        "content": "hello",
    }
    base.update(kw)
    gone = base.pop("gone", None)
    return dob.SentItem(sent.SentRow(**base), gone)  # type: ignore[arg-type]
