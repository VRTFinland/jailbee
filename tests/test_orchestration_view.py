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
