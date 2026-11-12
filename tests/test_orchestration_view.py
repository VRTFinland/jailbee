"""Tests for the orchestration view model."""

from dataclasses import replace

import pytest

from jailbee.orchestration.view import (
    ChildView,
    GrantView,
    annotate,
    parse_child_view,
    parse_grant_view,
    queued_counts,
)
from tests.dashboard_fixtures import ci

GRANT = (
    '{"version":1,"max_running_children":4,'
    '"operations":["import","message","restart","spawn","stop"],'
    '"grantable":{"mounts":["data"],"loose":false,"egress":["pypi.org"]}}'
)


def test_parse_grant_view_reads_all_fields():
    v = parse_grant_view(GRANT)
    assert v == GrantView(
        4, ("import", "message", "restart", "spawn", "stop"), ("data",), False, ("pypi.org",)
    )


def test_parse_grant_view_without_publish_has_no_publish_rights():
    v = parse_grant_view(GRANT)
    assert v is not None
    assert (v.publish_issues, v.publish_foreign_repos, v.publish_foreign_actions) == ((), (), ())
    assert (v.publish_prs, v.publish_pr_reviews, v.publish_rate) == (False, False, 30)


def test_parse_grant_view_keeps_immutable_project_rights_and_old_defaults():
    from jailbee.orchestration.grant import Grant, PublishRights

    grant = Grant(publish=PublishRights.model_validate({"projects": [
        {"board": "Org/3", "actions": ["set", "remove"]}
    ]}))
    view = parse_grant_view(grant.to_label())
    assert view is not None
    assert view.publish_projects == (("org/3", ("remove", "set")),)
    assert parse_grant_view(GRANT).publish_projects == ()


def test_parse_grant_view_reads_publish_rights():
    raw = GRANT[:-1] + (
        ',"publish":{"issues":["state","comment"],'
        '"foreign_issues":{"repos":["Org/Lib"],"actions":["comment"]},'
        '"pull_requests":true,"pr_reviews":true,"max_actions_per_hour":12}}'
    )
    v = parse_grant_view(raw)
    assert v is not None
    assert v.publish_issues == ("comment", "state")
    assert (v.publish_foreign_repos, v.publish_foreign_actions) == (("org/lib",), ("comment",))
    assert (v.publish_prs, v.publish_pr_reviews, v.publish_rate) == (True, True, 12)


@pytest.mark.parametrize("raw", [None, "", "{", "[]", '{"version": 2}', 42])
def test_parse_grant_view_corrupt_is_none(raw):
    assert parse_grant_view(raw) is None


def test_parse_child_view_defaults_state_to_working():
    v = parse_child_view("alpha-orch", None, '{"version":1,"mounts":[],"loose":true,"egress":[]}')
    assert v == ChildView("alpha-orch", "working", (), True, ())


def test_parse_child_view_without_spawned_by_is_none():
    assert parse_child_view(None, "done", None) is None


def test_parse_child_view_corrupt_rights_keeps_child_with_no_rights():
    assert parse_child_view("alpha-orch", "done", "{") == ChildView(
        "alpha-orch", "done", (), False, ()
    )


def test_parse_child_view_unknown_state_reads_as_working():
    v = parse_child_view("alpha-orch", "bogus", None)
    assert v is not None
    assert v.state == "working"


def test_queued_counts(tmp_path):
    p = tmp_path / "queue.json"
    p.write_text('{"version":1,"queues":{"alpha-orch":[{},{}],"beta-o":[]}}')
    assert queued_counts(p) == {"alpha-orch": 2, "beta-o": 0}


@pytest.mark.parametrize(
    "text", [None, "", "{", '{"version":1,"queues":[]}', '{"version":1,"queues":{"a":7}}']
)
def test_queued_counts_missing_or_garbage_is_empty(tmp_path, text):
    p = tmp_path / "queue.json"
    if text is not None:
        p.write_text(text)
    assert queued_counts(p) == {}


def test_queued_counts_reads_saved_db_and_ignores_stale_file(tmp_path):
    from jailbee.orchestration.state import QueuedRequest, save_queue

    request = QueuedRequest("m", "child", "fork", None, "t", "b", None, "", {})
    save_queue(tmp_path, {"orch": [request]})
    path = tmp_path / "queue.json"
    path.write_text('{"queues":{"stale":[{},{}]}}')
    assert queued_counts(path) == {"orch": 1}
    assert path.exists()


def test_queued_counts_legacy_is_read_only_and_marker_blocks_fallback(tmp_path):
    from jailbee.orchestration.state import load_queue

    path = tmp_path / "queue.json"
    path.write_text('{"queues":{"orch":[]}}')
    assert queued_counts(path) == {"orch": 0}
    assert not (tmp_path / "broker.db").exists()
    assert load_queue(tmp_path) == {}
    path.write_text('{"queues":{"stale":[{}]}}')
    assert queued_counts(path) == {"orch": 0}
    assert path.exists()


def test_annotate_counts_running_children_and_orphans():
    orch = replace(ci("alpha-orch", "alpha"), grant=parse_grant_view(GRANT))
    kid1 = replace(ci("alpha-k1", "alpha"), child=ChildView("alpha-orch", "working", (), False, ()))
    kid2 = replace(
        ci("alpha-k2", "alpha", state="Stopped"),
        child=ChildView("alpha-orch", "done", (), False, ()),
    )
    orphan = replace(
        ci("alpha-k3", "alpha"), child=ChildView("alpha-gone", "working", (), False, ())
    )
    annotate([orch, kid1, kid2, orphan], {"alpha-orch": 3})
    assert (orch.running_children, orch.queued_children) == (1, 3)
    assert kid1.orchestrator_running is True
    assert orphan.orchestrator_running is False


def test_annotate_counts_child_still_being_created():
    orch = replace(ci("alpha-orch", "alpha"), grant=parse_grant_view(GRANT))
    kid = replace(
        ci("alpha-k1", "alpha", state="—", job_phase="cloning"),
        child=ChildView("alpha-orch", "working", (), False, ()),
        job_kind="create",
    )
    annotate([orch, kid], {})
    assert orch.running_children == 1


def test_annotate_stopped_orchestrator_marks_children():
    orch = replace(ci("alpha-orch", "alpha", state="Stopped"), grant=parse_grant_view(GRANT))
    kid = replace(ci("alpha-k1", "alpha"), child=ChildView("alpha-orch", "working", (), False, ()))
    annotate([orch, kid], {})
    assert kid.orchestrator_running is False


def test_child_view_parses_runner():
    v = parse_child_view(
        "alpha-orch", "done", None, '{"agent":"claude-jb","model":"opus","profile":"p1"}'
    )
    assert v is not None
    assert v.runner == "claude-jb p1 opus"


@pytest.mark.parametrize("raw", [None, "", "{", '{"agent":"nope"}', 42])
def test_child_view_corrupt_runner_is_none(raw):
    v = parse_child_view("alpha-orch", "done", None, raw)
    assert v is not None
    assert v.runner is None


def test_parse_grant_view_default_runner():
    import json

    data = json.loads(GRANT)
    data["default_runner"] = {"agent": "claude", "model": "opus"}
    assert parse_grant_view(json.dumps(data)).default_runner == "claude opus"


def _populated(tmp_path):
    from jailbee.orchestration.store import BrokerStore, store_path

    st = BrokerStore(store_path(tmp_path))
    m = {"type": "message", "kind": "note", "ref": "r", "from": "bob"}
    st.insert_item("u1", "app-a", {**m, "id": "m1"}, now=100.0)
    st.insert_item("u1", "app-a", {**m, "id": "m2"}, now=110.0)
    st.insert_item("u2", "app-b", {**m, "id": "m3"}, now=120.0)
    st.ack("u1", "m2", now=200.0)
    st.ack("u1", "m1", now=210.0)
    return st


def test_messages_view_default_is_unread_only(tmp_path):
    from jailbee.orchestration.view import messages_view

    _populated(tmp_path)
    rows = messages_view(tmp_path, full=None, include_acked=False)
    assert [r["id"] for r in rows] == ["m3"]
    assert rows[0]["acked_at"] is None and rows[0]["from"] == "bob"
    assert str(rows[0]["created_at"]).startswith("1970-01-01T00:02:00")


def test_messages_view_all_puts_acked_last_newest_last(tmp_path):
    from jailbee.orchestration.view import messages_view

    _populated(tmp_path)
    rows = messages_view(tmp_path, full=None, include_acked=True)
    assert [r["id"] for r in rows] == ["m3", "m2", "m1"]
    assert rows[-1]["acked_at"] is not None


def test_messages_view_filters_by_container_and_shortens(tmp_path):
    from jailbee.orchestration.view import messages_view

    _populated(tmp_path)
    rows = messages_view(
        tmp_path, full="app-a", include_acked=True, short=lambda f: f.removeprefix("app-")
    )
    assert {r["container"] for r in rows} == {"a"}
    assert len(rows) == 2


def test_messages_view_missing_db_is_empty_and_creates_nothing(tmp_path):
    from jailbee.orchestration.view import messages_view

    assert messages_view(tmp_path, full=None, include_acked=True) == []
    assert list(tmp_path.iterdir()) == []


def test_messages_view_does_not_migrate_an_empty_db(tmp_path):
    import sqlite3

    from jailbee.orchestration.store import store_path
    from jailbee.orchestration.view import messages_view

    sqlite3.connect(store_path(tmp_path)).close()
    assert messages_view(tmp_path, full=None, include_acked=True) == []
    con = sqlite3.connect(store_path(tmp_path))
    assert con.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    con.close()


def test_messages_target_resolves_or_takes_the_name_as_written(tmp_path, mocker) -> None:
    from unittest.mock import MagicMock

    from jailbee.orchestration.view import messages_target
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    prefix = cfg.container_prefix
    factory = MagicMock()
    assert messages_target(cfg, factory, None) is None
    factory.assert_not_called()
    resolve = mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=f"{prefix}-a")
    assert messages_target(cfg, factory, "a") == f"{prefix}-a"
    resolve.side_effect = ValueError("gone")
    assert messages_target(cfg, factory, "gone") == f"{prefix}-gone"
    assert messages_target(cfg, factory, f"{prefix}-gone") == f"{prefix}-gone"


def test_messages_table_shows_missing_values_as_dashes() -> None:
    from jailbee.orchestration.view import messages_table

    row = {k: None for k in ("container", "id", "type", "kind", "from", "ref", "acked_at")}
    table = messages_table([{**row, "container": "a", "id": "m1", "created_at": "t"}])
    cells = [c._cells[0] for c in table.columns]
    assert cells == ["a", "m1", "-", "-", "-", "-", "t", "-"]
