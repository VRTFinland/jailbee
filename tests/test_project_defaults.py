"""Default projects are filled at the host staging boundary."""
# ruff: noqa: F811  (fixture imported from the sibling test module)

import json

import pytest

from jailbee import issue_projects
from jailbee.orchestration import wire
from jailbee.outbox import broker_ops, store_db
from tests.test_orchestration_query import Env
from tests.test_outbox_broker_ops import PLAIN, env  # noqa: F401


@pytest.mark.parametrize("op", [wire.OUTBOX_PUT, wire.OUTBOX_DRAFT_APPEND])
def test_staging_uses_default_project(env: Env, op: str) -> None:
    store = store_db.OutboxStore(env.store)
    action = {"type": "project_add", "repo": ".", "issue": 7}
    calls = []

    def reader(board, repo, number, field):
        calls.append((board, repo, number, field))
        return issue_projects.ProjectExpectation(present=True)

    args = (
        {
            "kind": "issue",
            "slug": "test",
            "manifest": {"version": 1, "actions": [action]},
            "bodies": {},
        }
        if op == wire.OUTBOX_PUT
        else {"kind": "issue", "name": "test", "action": action, "bodies": {}}
    )
    broker_ops.handle(
        store, PLAIN, op, args, now=1, project_reader=reader, default_project="acme/42"
    )
    if op == wire.OUTBOX_PUT:
        (row,) = store_db.read_proposals(env.rig.root, PLAIN, "issue")
    else:
        row = store.get_draft(PLAIN, "issue", "test")
        assert row is not None
    stored = json.loads(row.manifest)["actions"][0]
    assert stored["project"] == "acme/42"
    assert stored["expected"] == {"present": True}
    assert calls == [("acme/42", ".", 7, None)]


@pytest.mark.parametrize("op", [wire.OUTBOX_PUT, wire.OUTBOX_DRAFT_APPEND])
def test_staging_without_project_names_configuration_key(env: Env, op: str) -> None:
    store = store_db.OutboxStore(env.store)
    action = {"type": "project_add", "repo": ".", "issue": 7}
    args = (
        {
            "kind": "issue",
            "slug": "test",
            "manifest": {"version": 1, "actions": [action]},
            "bodies": {},
        }
        if op == wire.OUTBOX_PUT
        else {"kind": "issue", "name": "test", "action": action, "bodies": {}}
    )
    with pytest.raises(wire.WireError, match=r"github\.project"):
        broker_ops.handle(store, PLAIN, op, args, now=1)
