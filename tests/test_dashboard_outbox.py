"""Tests for the dashboard's Outbox entry (pure; no terminal)."""

from __future__ import annotations

import json

import pytest

from jailbee import dashboard_outbox as dob
from jailbee.dashboard import prompt_target_kind


def _proposal(pid: str = "pr/a.json", **over: object) -> dict[str, object]:
    return {
        "id": pid,
        "state": "pending",
        "revision": "r1",
        "actions": [{"index": 0}, {"index": 1}],
        "error": None,
        "edit_block": None,
        **over,
    }


def _listing(*proposals: dict[str, object], name: str = "alpha-x", **over: object) -> str:
    container = {
        "name": name,
        "available": True,
        "error": None,
        "stores": [],
        "proposals": list(proposals),
        **over,
    }
    return json.dumps({"schema": 1, "containers": [container]})


def _row(**over: object) -> dob.ProposalRow:
    base: dict[str, object] = {
        "id": "pr/a.json",
        "state": "pending",
        "revision": "r1",
        "actions": 2,
        "error": None,
        "edit_block": None,
    }
    return dob.ProposalRow(**{**base, **over})  # type: ignore[arg-type]  # test-only overrides


def test_parse_reads_the_named_containers_proposals():
    stdout = _listing(_proposal(), _proposal("issue/b.json", state="partial", actions=[]))
    listing = dob.parse_outbox_listing(stdout, "alpha-x")
    assert listing == dob.OutboxListing(
        (_row(), _row(id="issue/b.json", state="partial", actions=0)), None, ()
    )


def test_parse_of_an_unlisted_container_is_an_empty_outbox():
    assert dob.parse_outbox_listing(_listing(_proposal()), "other") == dob.OutboxListing(
        (), None, ()
    )


def test_parse_carries_an_unavailable_containers_reason():
    listing = dob.parse_outbox_listing(_listing(available=False, error="not running"), "alpha-x")
    assert listing.error == "not running"
    assert dob.parse_outbox_listing(_listing(available=False), "alpha-x").error == (
        "container unavailable"
    )


def test_parse_collects_store_warnings_and_rejections():
    stores = [{"kind": "pr", "warnings": ["w1"], "rejected": ["bad.json"]}]
    assert dob.parse_outbox_listing(_listing(stores=stores), "alpha-x").warnings == (
        "w1",
        "bad.json",
    )


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "Container  Proposal",
        "[]",
        '{"containers": {}}',
        _listing("not-a-proposal"),  # type: ignore[arg-type]  # deliberately malformed
        _listing({"state": "pending", "revision": "r", "actions": []}),
        _listing(_proposal(actions=3)),
        _listing(_proposal(revision=None)),
    ],
    ids=["empty", "table", "list", "containers-object", "row", "no-id", "actions", "revision"],
)
def test_parse_refuses_anything_else(stdout):
    with pytest.raises(dob.OutboxLoadError, match="unexpected output"):
        dob.parse_outbox_listing(stdout, "alpha-x")


def test_outbox_picker_lists_the_proposals_then_the_browser():
    rows = (_row(), _row(id="issue/b.json", actions=1, error="bad manifest"))
    picker = dob.outbox_picker("alpha-x", rows, can_browse=True)
    assert picker.title == "Outbox — alpha-x"
    assert [(e.label, e.value) for e in picker.entries] == [
        ("pr/a.json  pending  (2 actions)", "proposal:pr/a.json"),
        ("issue/b.json  pending  (bad manifest)", "proposal:issue/b.json"),
        ("Browse actions & comments…", dob.BROWSE),
    ]
    assert [e.value for e in dob.outbox_picker("alpha-x", rows, can_browse=False).entries] == [
        "proposal:pr/a.json",
        "proposal:issue/b.json",
    ]


def test_a_proposal_value_never_reads_as_the_browse_entry():
    assert dob.proposal_id(dob.proposal_value(dob.BROWSE)) == dob.BROWSE
    assert dob.proposal_id(dob.BROWSE) is None


@pytest.mark.parametrize(
    ("row", "values"),
    [
        (_row(), [dob.SHOW, dob.PUBLISH, dob.DELETE]),
        (_row(state="awaiting-pr"), [dob.SHOW, dob.PUBLISH, dob.DELETE]),
        (_row(state="applied"), [dob.SHOW, dob.DELETE]),
        (_row(error="bad"), [dob.SHOW, dob.DELETE]),
        (_row(state="partial", edit_block="settle first"), [dob.SHOW, dob.PUBLISH]),
    ],
    ids=["pending", "awaiting-pr", "applied", "invalid", "edit-block"],
)
def test_proposal_picker_offers_what_the_proposal_allows(row, values):
    picker = dob.proposal_picker("alpha-x", row, can_show=True, can_publish=True, can_delete=True)
    assert [e.value for e in picker.entries] == values
    assert picker.carry == (row.id, row.revision, str(row.actions))


def test_proposal_picker_offers_only_permitted_entries():
    picker = dob.proposal_picker(
        "alpha-x", _row(), can_show=False, can_publish=False, can_delete=True
    )
    assert [e.value for e in picker.entries] == [dob.DELETE]


@pytest.mark.parametrize("action", [dob.PUBLISH, dob.DELETE])
def test_outbox_confirm_puts_no_first(action):
    picker = dob.outbox_confirm_picker("alpha-x", action, "pr/a.json", "r1", 2)
    assert [e.value for e in picker.entries] == ["no", "yes"]
    assert picker.carry == (action, "pr/a.json", "r1")


def test_every_outbox_question_is_about_the_container():
    questions = (
        dob.outbox_picker("alpha-x", (_row(),), can_browse=True),
        dob.proposal_picker("alpha-x", _row(), can_show=True, can_publish=True, can_delete=True),
        dob.outbox_confirm_picker("alpha-x", dob.DELETE, "pr/a.json", "r1", 2),
    )
    for question in questions:
        assert prompt_target_kind(question.purpose) == "container"
        assert question.target == "alpha-x"


def _parsed(argv: list[str]) -> dict[str, object]:
    """``argv`` parsed by its real Click command, as the child will parse it."""
    from jailbee.remote_ssh import router

    typed, command = router.command_leaf(argv)
    words = typed.split()
    with command.make_context(words[-1], argv[len(words) :], resilient_parsing=True) as ctx:
        return dict(ctx.params)


def test_change_argv_pins_the_listed_revision_and_confirms():
    for argv in (
        dob.outbox_apply_argv("alpha-x", "pr/a.json", "r1"),
        dob.outbox_drop_argv("alpha-x", "pr/a.json", "r1"),
    ):
        params = _parsed(argv)
        assert (params["container"], params["proposal"]) == ("alpha-x", "pr/a.json")
        assert (params["revision"], params["yes"]) == ("r1", True)


def test_listing_argv_asks_for_json():
    params = _parsed(dob.outbox_ls_argv("alpha-x"))
    assert params["container"] == "alpha-x"
    assert str(params["output"]) == "json"
