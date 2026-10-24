"""Tests for the pure Outbox manager core (no terminal)."""

from __future__ import annotations

import json

import pytest
from rich.cells import cell_len

from jailbee.dashboard import outbox as dob


def _action(index: int = 0, **over: object) -> dict[str, object]:
    return {
        "index": index,
        "kind": "comment",
        "repo": "o/r",
        "target": "42",
        "title": None,
        "body": "hello",
        "markdown": True,
        "reply_to": None,
        "state": "pending",
        "receipt": None,
        "delete_scope": [index],
        "delete_block": None,
        "comments": [],
        **over,
    }


def _proposal(pid: str = "pr/a.json", **over: object) -> dict[str, object]:
    return {
        "id": pid,
        "kind": pid.split("/")[0],
        "state": "pending",
        "revision": "r1",
        "raw_text": '{"raw": 1}\n{"more": 2}',
        "error": None,
        "edit_block": None,
        "draftable": False,
        "deletable": True,
        "delete_block": None,
        "delete_archive": False,
        "actions": [_action(0), _action(1)],
        **over,
    }


def _container(*proposals: dict[str, object], name: str = "alpha-x", **over: object):
    return {
        "name": name,
        "available": True,
        "error": None,
        "stores": [],
        "proposals": list(proposals),
        **over,
    }


def _listing(*containers: dict[str, object]) -> str:
    return json.dumps({"schema": 1, "containers": list(containers)})


def _state(*proposals: dict[str, object], **over: object) -> dob.OutboxState:
    containers = dob.parse_outbox_listing(_listing(_container(*proposals)))
    return dob.OutboxState("Outbox", containers, **over)  # type: ignore[arg-type]  # test overrides


def _key(pid: str = "pr/a.json", action: int | None = None, comment: int | None = None):
    return ("alpha-x", pid, action, comment)


def test_parse_listing_all_containers_and_new_fields():
    issue = _proposal(
        "issue/b.json",
        edit_block="blocked",
        deletable=False,
        delete_block="why",
        delete_archive=True,
        draftable=True,
        actions=[
            _action(
                0,
                kind="create",
                repo="github:x/y",
                target="x/y",
                title="T",
                delete_scope=[0, 1],
                comments=[],
            )
        ],
    )
    pr = _proposal(
        actions=[
            _action(
                0,
                kind="review",
                comments=[{"index": 0, "label": "a:1", "text": "t", "path": "a", "line": 1}],
            )
        ]
    )
    got = dob.parse_outbox_listing(
        _listing(_container(pr), _container(issue, name="beta-y", stores=[{"warnings": ["w"]}]))
    )
    assert [c.name for c in got] == ["alpha-x", "beta-y"]
    item = got[1].proposals[0]
    assert (item.deletable, item.delete_block, item.delete_archive, item.draftable) == (
        False,
        "why",
        True,
        True,
    )
    assert item.actions[0].delete_scope == (0, 1) and item.actions[0].title == "T"
    assert got[1].warnings == ("w",)
    assert item.foreign_repos == ("x/y",)
    comment = got[0].proposals[0].actions[0].comments[0]
    assert (comment.path, comment.line) == ("a", 1)


@pytest.mark.parametrize("stdout", ["[]", "{", json.dumps({"schema": 1}), '{"containers": {}}'])
def test_parse_rejects_non_listing(stdout):
    with pytest.raises(dob.OutboxLoadError):
        dob.parse_outbox_listing(stdout)


def test_unavailable_container_rows():
    containers = dob.parse_outbox_listing(
        _listing(_container(name="gone", available=False, error="boom"))
    )
    rows = dob.visible_rows(dob.OutboxState("t", containers), 80)
    assert len(rows) == 1 and rows[0].role == "header" and rows[0].key is None
    assert rows[0].text.plain == "gone  ⚠ boom"
    assert rows[0].text.style == "yellow"


def test_proposal_row_text_pr_and_issue():
    pr = _proposal(actions=[_action(i, title="Fix it" if i == 0 else None) for i in range(3)])
    issue = _proposal(
        "issue/b.json",
        state="partial",
        actions=[
            _action(0, kind="create", repo="o/r", target="o/r", title="Add x", state="applied"),
            _action(1, kind="create", repo="o/r", target="o/r", title="Y"),
        ],
    )
    rows = dob.visible_rows(_state(pr, issue), 120)
    assert rows[0].text.plain == "▸   PR  o/r#42  Fix it  · 3 actions · pending"
    assert rows[1].text.plain == "▸   ISS o/r  Add x  · 2 actions · applied 1/2"
    marked = dob.visible_rows(_state(pr, marks=frozenset({_key()})), 120)
    assert marked[0].text.plain.startswith("▸ ● PR  ")


def test_error_and_foreign_proposal_rows():
    bad = _proposal(error="bad json")
    foreign = _proposal(
        "issue/f.json", actions=[_action(0, kind="create", repo="github:a/b", title="T")]
    )
    rows = dob.visible_rows(_state(bad, foreign), 120)
    assert "bad json" in rows[0].text.plain
    assert any(s.style == "yellow" for s in rows[0].text.spans)
    assert rows[1].text.plain.endswith("  ⚠ foreign")


def test_summary_prefers_title_then_first_body_line():
    item = dob.parse_outbox_listing(
        _listing(
            _container(
                _proposal(actions=[_action(0, title="T", body="b")]),
                _proposal("pr/b.json", actions=[_action(0, title=None, body="\n\n second \nx")]),
                _proposal("pr/c.json", actions=[_action(0, title=None, body="")]),
            )
        )
    )[0].proposals
    assert [dob.proposal_summary(p) for p in item] == ["T", "second", "(no text)"]


def test_summary_truncates_to_width():
    long = _proposal(actions=[_action(0, title="x" * 200)])
    row = dob.visible_rows(_state(long), 40)[0]
    assert cell_len(row.text.plain) <= 40
    assert row.text.plain.endswith("…")


def test_expand_shows_actions_then_comments():
    comments = [
        {"index": i, "label": "l", "text": f"c{i}\nmore", "path": "src/a.py", "line": 5 + i}
        for i in range(2)
    ]
    pr = _proposal(
        actions=[
            _action(0, kind="review", comments=comments),
            _action(1, kind="reply", reply_to=123),
        ]
    )
    state = _state(pr, expanded=frozenset({_key(), _key(action=0)}))
    rows = dob.visible_rows(state, 100)
    assert [r.role for r in rows] == ["proposal", "action", "comment", "comment", "action"]
    assert [r.key for r in rows[1:]] == [
        _key(action=0),
        _key(action=0, comment=0),
        _key(action=0, comment=1),
        _key(action=1),
    ]
    assert rows[1].text.plain == "  ▾   review · 2 comments  [pending]"
    assert rows[2].text.plain == "        src/a.py:5  c0"
    assert rows[4].text.plain == "      reply to #123  [pending]"
    assert [r.depth for r in rows] == [0, 1, 2, 2, 1]


def test_notes_close_their_container_group():
    containers = dob.parse_outbox_listing(
        _listing(
            _container(_proposal(), stores=[{"warnings": ["w1"], "rejected": ["r1"]}]),
            _container(name="beta-y"),
        )
    )
    rows = dob.visible_rows(dob.OutboxState("t", containers), 100)
    assert [(r.role, r.markable) for r in rows] == [
        ("header", False),
        ("proposal", True),
        ("note", False),
        ("note", False),
        ("header", False),
    ]


def test_rows_and_preview_strip_controls_and_keep_markup_literal():
    evil = "[red]x[/red]\x1b]0;pwn\x07​"
    pr = _proposal(actions=[_action(0, title=evil, body=evil)])
    state = _state(pr)
    row = dob.visible_rows(state, 100)[0]
    assert "[red]x[/red]" in row.text.plain
    assert "\x1b" not in row.text.plain and "​" not in row.text.plain
    assert not any(s.style == "red" for s in row.text.spans)
    for line in dob.preview_lines(state, _key(), 80):
        assert "\x1b" not in line.plain and "​" not in line.plain
        assert not any(s.style == "red" for s in line.spans)
    assert any("[red]x[/red]" in line.plain for line in dob.preview_lines(state, _key(), 80))


def test_preview_markdown_rendered_and_raw_toggle():
    pr = _proposal(actions=[_action(0, body="# H\n\n*e*")])
    state = _state(pr)
    plain = [t.plain for t in dob.preview_lines(state, _key(), 60)]
    assert plain[0] == "Action 0: comment o/r 42 [pending]"
    assert "# H" not in plain
    assert any("H" in line for line in plain)
    raw = dob.preview_lines(_state(pr, raw=True), _key(), 60)
    assert [t.plain for t in raw] == ['{"raw": 1}', '{"more": 2}']


def test_preview_action_comment_question_and_status():
    comments = [{"index": 0, "label": "a.py:3", "text": "note", "path": "a.py", "line": 3}]
    pr = _proposal(
        actions=[
            _action(0, kind="review", comments=comments, receipt="https://x"),
            _action(1, title="Tt", body=None),
        ]
    )
    state = _state(pr)
    whole = [t.plain for t in dob.preview_lines(state, _key(), 60)]
    assert whole[0].startswith("Action 0: review") and "Receipt: https://x" in whole
    assert "Comment 0: a.py:3" in whole and "Title: Tt" in whole
    one = [t.plain for t in dob.preview_lines(state, _key(action=1), 60)]
    assert one == ["Action 1: comment o/r 42 [pending]", "Title: Tt"]
    cm = [t.plain for t in dob.preview_lines(state, _key(action=0, comment=0), 60)]
    assert cm[0] == "Comment 0: a.py:3"
    q = dob.Question("p", "T", ("sure?", "really"), (("y", "Yes"),), "y")
    import dataclasses

    shown = dataclasses.replace(state, question=q, status=("done",))
    assert [t.plain for t in dob.preview_lines(shown, _key(), 60)] == [
        "sure?",
        "really",
        "",
        "done",
    ]


def test_delete_scope_lines_include_dependents_and_archive():
    issue_a = _proposal("issue/a.json", delete_archive=True)
    issue_b = _proposal(
        "issue/b.json",
        actions=[_action(0, kind="create", delete_scope=[0, 1]), _action(1, kind="comment")],
    )
    comments = [{"index": 2, "label": "x", "text": "t", "path": "src/a.py", "line": 5}]
    pr_c = _proposal("pr/c.json", actions=[_action(0, kind="review", comments=comments)])
    state = _state(issue_a, issue_b, pr_c)
    keys = [
        ("alpha-x", "pr/c.json", 0, 2),
        ("alpha-x", "issue/b.json", 0, None),
        ("alpha-x", "issue/a.json", None, None),
    ]
    assert dob.delete_scope_lines(state, keys) == (
        "issue/a.json (whole, journal archived)",
        "issue/b.json action 0 (create) + dependent 1",
        "pr/c.json action 0 comment 2 (src/a.py:5)",
    )


def test_not_markable_when_blocked():
    pr = _proposal(
        deletable=False,
        actions=[_action(0, delete_scope=None, delete_block="no"), _action(1)],
    )
    rows = dob.visible_rows(_state(pr, expanded=frozenset({_key()})), 100)
    assert [r.markable for r in rows] == [False, False, True]


def test_standalone_hint_fits_100_columns_with_every_ability():
    every = dob.Abilities(True, True, True, True, True, True)
    hint = dob.outbox_hint(_state(_proposal(), abilities=every), standalone=True)
    # 96: the box's content width at 100 terminal columns (border and padding).
    assert len(hint) <= 96
    assert hint.startswith("q quit")
    for part in ("d delete", "p publish", "c PR", "X clear", "a all repos", "r refresh"):
        assert part in hint


def test_hint_drops_unavailable_abilities():
    full = dob.Abilities(True, True, False, True, True, True)
    state = _state(_proposal(), abilities=full)
    hint = dob.outbox_hint(state, standalone=False)
    assert hint == (
        "↑/↓ move · Enter open · Space mark · d delete · p publish · c create PR · X clear"
        " · v preview · m manifest · r refresh · Esc close"
    )
    standalone = dob.outbox_hint(state, standalone=True)
    assert "a all repos" in standalone and standalone.startswith("q quit")
    none = dob.outbox_hint(_state(_proposal()), standalone=False)
    assert "d delete" not in none and "p publish" not in none and "a all" not in none
    q = dob.Question("p", "T", (), (), "")
    import dataclasses

    assert (
        dob.outbox_hint(dataclasses.replace(state, question=q), standalone=False)
        == "↑/↓ choose · Enter confirm · Esc cancel"
    )


def test_find_proposal():
    state = _state(_proposal())
    found = dob.find_proposal(state, _key(action=1))
    assert found is not None and found[1].id == "pr/a.json"
    assert dob.find_proposal(state, _key("pr/zz.json")) is None
    assert dob.find_proposal(state, None) is None


def test_proposal_flags():
    state = _state(
        _proposal(state="awaiting-pr", create_scope={"kind": "repo"}),
        _proposal("pr/b.json", error="e"),
        _proposal("pr/c.json", state="partial"),
    )
    a, b, c = state.containers[0].proposals
    assert (a.creatable, a.publishable) == (True, False)
    assert b.publishable is False and c.publishable is True


def test_argv_builders():
    assert dob.outbox_ls_argv(None) == ["outbox", "ls", "-o", "json"]
    assert dob.outbox_ls_argv("c", all_repos=True) == [
        "outbox", "ls", "c", "--all-repos", "-o", "json",
    ]  # fmt: skip
    assert dob.outbox_plan_argv("c", "pr/x.json", "r") == [
        "outbox", "apply", "c", "pr/x.json", "--dry-run", "--foreign",
        "--revision", "r", "-o", "json",
    ]  # fmt: skip
    assert dob.outbox_apply_argv("c", "pr/x.json", "r", yes=False) == [
        "outbox", "apply", "c", "pr/x.json", "--revision", "r",
    ]  # fmt: skip
    assert dob.outbox_apply_argv("c", "pr/x.json", "r", yes=True, draft=True, foreign=True) == [
        "outbox", "apply", "c", "pr/x.json", "--revision", "r", "-y", "--draft", "--foreign",
    ]  # fmt: skip
    assert dob.outbox_drop_argv("c", "pr/x.json", "r", action=1, comment=0) == [
        "outbox", "drop", "c", "pr/x.json", "--action", "1", "--comment", "0",
        "--yes", "--revision", "r",
    ]  # fmt: skip
    assert dob.outbox_drop_argv(
        "c", "issue/x.json", "r", with_dependents=True, archive_journal=True
    ) == [
        "outbox", "drop", "c", "issue/x.json", "--with-dependents", "--archive-journal",
        "--yes", "--revision", "r",
    ]  # fmt: skip
    assert dob.create_pr_argv("c", {"kind": "repo"}) == ["pr", "c"]
    assert dob.clear_prepare_argv("c") == ["outbox", "clear", "c", "--prepare-snapshot"]
    assert dob.clear_approved_argv("c", "tok") == [
        "outbox", "clear", "c", "--approved-snapshot", "tok", "--yes",
    ]  # fmt: skip


def test_every_container_field_is_sanitised_and_literal():
    import dataclasses

    evil = "[red]x[/red]\x1b]0;pwn\x07\u200b"
    comments = [{"index": 0, "label": evil, "text": evil, "path": evil, "line": 1}]
    pr = _proposal(
        error=evil,
        edit_block=evil,
        actions=[_action(0, repo=evil, target=evil, receipt=evil, comments=comments)],
    )
    containers = dob.parse_outbox_listing(
        _listing(
            _container(pr, name=evil, stores=[{"warnings": [evil]}]),
            _container(name=evil + "2", available=False, error=evil),
        )
    )
    q = dob.Question("p", evil, (evil,), (), "")
    state = dob.OutboxState(
        "t",
        containers,
        expanded=frozenset({(evil, "pr/a.json", None, None), (evil, "pr/a.json", 0, None)}),
        question=q,
        status=(evil,),
    )

    def check(text) -> None:
        assert "\x1b" not in text.plain and "\x07" not in text.plain and "\u200b" not in text.plain
        assert not any("red" in str(s.style) for s in text.spans)
        assert "red" not in str(text.style)

    rows = dob.visible_rows(state, 400)
    assert len(rows) == 6
    for row in rows:
        check(row.text)
        if row.role != "action":
            assert "[red]x[/red]" in row.text.plain
    key = (evil, "pr/a.json", None, None)
    for st in (
        state,
        dataclasses.replace(state, question=None),
        dataclasses.replace(state, question=None, raw=True),
    ):
        for k in (key, (evil, "pr/a.json", 0, None), (evil, "pr/a.json", 0, 0)):
            for line in dob.preview_lines(st, k, 80):
                check(line)
    plain = [t.plain for t in dob.preview_lines(dataclasses.replace(state, question=None), key, 80)]
    assert any("Receipt: [red]x[/red]" in line for line in plain)
    assert any("Action 0: comment [red]x[/red]" in line for line in plain)
    shown = [t.plain for t in dob.preview_lines(state, key, 80)]
    assert shown == ["[red]x[/red]]0;pwn", "", "[red]x[/red]]0;pwn"]


def test_hint_publish_with_hand_off_only():
    state = _state(_proposal(), abilities=dob.Abilities(False, False, True, False, False, False))
    assert "p publish" in dob.outbox_hint(state, standalone=False)


def test_comment_markability_follows_parent_action():
    comments = [{"index": 0, "label": "l", "text": "t", "path": "a", "line": 1}]
    ok = _action(0, kind="review", comments=comments)
    blocked = _action(1, kind="review", comments=comments, delete_scope=None, delete_block="no")
    state = _state(
        _proposal(actions=[ok, blocked]),
        expanded=frozenset({_key(), _key(action=0), _key(action=1)}),
    )
    rows = dob.visible_rows(state, 100)
    assert [(r.role, r.markable) for r in rows] == [
        ("proposal", True),
        ("action", True),
        ("comment", True),
        ("action", False),
        ("comment", False),
    ]


def test_delete_scope_lines_prefix_container_when_several():
    containers = dob.parse_outbox_listing(
        _listing(
            _container(_proposal("pr/a.json")),
            _container(_proposal("pr/b.json"), name="beta-y"),
        )
    )
    state = dob.OutboxState("t", containers)
    keys = [("beta-y", "pr/b.json", None, None), ("alpha-x", "pr/a.json", 1, None)]
    assert dob.delete_scope_lines(state, keys) == (
        "alpha-x: pr/a.json action 1 (comment)",
        "beta-y: pr/b.json (whole)",
    )


def test_has_children():
    reviewed = _action(1, kind="review", comments=[{"index": 0, "label": "c", "text": "t"}])
    state = _state(
        _proposal("pr/a.json", actions=[_action(0), reviewed]),
        _proposal("pr/empty.json", actions=[]),
    )
    assert dob.has_children(state, _key())
    assert not dob.has_children(state, _key("pr/empty.json"))
    assert not dob.has_children(state, _key(action=0))
    assert dob.has_children(state, _key(action=1))
    assert not dob.has_children(state, _key(action=1, comment=0))
    assert not dob.has_children(state, _key("pr/gone.json"))
    assert not dob.has_children(state, None)
