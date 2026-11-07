from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from jailbee.global_config import GlobalConfig
from jailbee.orchestration import actions, labels, messages
from jailbee.orchestration.actions import ActionContext, Node, Outcome, done, refused
from jailbee.orchestration.grant import Grant
from jailbee.orchestration.protocol import MessageManifest
from tests.conftest import make_config

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from unittest.mock import MagicMock as MagicMockT


def _node(short: str, running: bool = True, **config: str) -> Node:
    return Node(
        full=f"myrepo-{short}",
        short=short,
        running=running,
        config={"volatile.uuid": f"uuid-{short}", **config},
    )


def _ctx(tmp_path: Path, sender: Node, nodes: list[Node]) -> ActionContext:
    cfg = make_config(tmp_path, container_prefix="myrepo")
    return ActionContext(
        cfg=cfg,
        gcfg=GlobalConfig(),
        incus=MagicMock(),
        root=tmp_path,
        sender=sender,
        nodes={n.full: n for n in [sender, *nodes]},
        now=datetime(2026, 10, 25, tzinfo=UTC),
    )


def _msg(**kw: object) -> MessageManifest:
    base: dict[str, object] = {"version": 1, "id": "m1", "type": "message", "body": "hi"}
    base.update(kw)
    return MessageManifest.model_validate(base)


@pytest.fixture
def deliver(mocker: MagicMockT) -> Iterator[MagicMockT]:
    yield mocker.patch("jailbee.orchestration.messages.deliver", return_value="delivered")


def _orch(tmp_path: Path, children: list[Node]) -> ActionContext:
    return _ctx(tmp_path, _node("orch", **{labels.GRANT: Grant().to_label()}), children)


def _child_of_orch() -> Node:
    return _node("fix-1", **{labels.SPAWNED_BY: "myrepo-orch"})


def test_handler_registered_by_importing_actions_only() -> None:
    assert callable(actions.HANDLERS["message"])


def test_handler_registered_without_importing_messages() -> None:
    import subprocess
    import sys

    code = "from jailbee.orchestration import actions; assert callable(actions.HANDLERS['message'])"
    subprocess.run([sys.executable, "-c", code], check=True)


def test_messages_importable_first() -> None:
    import subprocess
    import sys

    subprocess.run([sys.executable, "-c", "import jailbee.orchestration.messages"], check=True)


def test_orchestrator_message_delivered(tmp_path: Path, deliver: MagicMockT) -> None:
    ctx = _orch(tmp_path, [_child_of_orch()])
    out = messages.handle_message(ctx, _msg(to="fix-1", in_reply_to="c-7"))
    assert out == done("delivered to fix-1")
    assert deliver.call_args.args[2] == "myrepo-fix-1"
    msg = deliver.call_args.args[3]
    assert (msg.from_, msg.body, msg.kind, msg.in_reply_to) == ("orch", "hi", "note", "c-7")
    assert msg.id.startswith("h-")
    assert deliver.call_args.kwargs["running"] is True
    assert deliver.call_args.kwargs["now"] == ctx.now


def test_message_passes_emission_seq(tmp_path: Path, deliver: MagicMockT) -> None:
    ctx = _orch(tmp_path, [_child_of_orch()])
    next(ctx.seq)
    next(ctx.seq)
    messages.handle_message(ctx, _msg(to="fix-1"))
    assert deliver.call_args.kwargs["seq"] == 2
    messages.handle_message(ctx, _msg(to="fix-1"))
    assert deliver.call_args.kwargs["seq"] == 3


def test_orchestrator_kind_passes_through(tmp_path: Path, deliver: MagicMockT) -> None:
    ctx = _orch(tmp_path, [_child_of_orch()])
    messages.handle_message(ctx, _msg(to="fix-1", kind="question"))
    assert deliver.call_args.args[3].kind == "question"


def test_orchestrator_message_queued_when_child_stopped(
    tmp_path: Path, deliver: MagicMockT
) -> None:
    deliver.return_value = "parked"
    ctx = _orch(tmp_path, [_node("fix-1", False, **{labels.SPAWNED_BY: "myrepo-orch"})])
    out = messages.handle_message(ctx, _msg(to="fix-1"))
    assert out == Outcome("queued", "delivery to fix-1 is queued; the broker will retry")
    assert deliver.call_args.kwargs["running"] is False


@pytest.mark.parametrize("listed", [False, True])
def test_message_to_creating_child_is_clearly_refused(
    tmp_path: Path, deliver: MagicMockT, listed: bool
) -> None:
    from jailbee.orchestration.watch import WatchEntry, register

    ctx = _orch(tmp_path, [_child_of_orch()] if listed else [])
    register(
        tmp_path,
        ctx.sender.full,
        WatchEntry("spawn-1", "myrepo-fix-1", "spawn", ctx.now.isoformat(), supervise=True),
    )
    out = messages.handle_message(ctx, _msg(to="fix-1"))
    assert out == refused(
        "child `fix-1` is still being created; message was not queued; "
        "retry after creation finishes"
    )
    deliver.assert_not_called()


def test_message_to_ready_child_is_delivered(tmp_path: Path, deliver: MagicMockT) -> None:
    from jailbee.orchestration.watch import WatchEntry, register

    ctx = _orch(tmp_path, [_child_of_orch()])
    register(
        tmp_path,
        ctx.sender.full,
        WatchEntry(
            "spawn-1",
            "myrepo-fix-1",
            "spawn",
            ctx.now.isoformat(),
            supervise=True,
            confirmed=True,
        ),
    )
    assert messages.handle_message(ctx, _msg(to="fix-1")) == done("delivered to fix-1")
    deliver.assert_called_once()


def test_other_orchestrators_creation_is_not_a_child(tmp_path: Path, deliver: MagicMockT) -> None:
    from jailbee.orchestration.watch import WatchEntry, register

    ctx = _orch(tmp_path, [])
    register(
        tmp_path,
        "myrepo-other",
        WatchEntry("spawn-1", "myrepo-fix-1", "spawn", ctx.now.isoformat()),
    )
    assert messages.handle_message(ctx, _msg(to="fix-1")) == refused(
        "`fix-1` is not a child of this orchestrator"
    )
    deliver.assert_not_called()


def test_orchestrator_message_to_non_child_refused(tmp_path: Path, deliver: MagicMockT) -> None:
    out = messages.handle_message(_orch(tmp_path, []), _msg(to="x"))
    assert out == refused("`x` is not a child of this orchestrator")
    deliver.assert_not_called()


def test_orchestrator_message_without_to_refused(tmp_path: Path, deliver: MagicMockT) -> None:
    out = messages.handle_message(_orch(tmp_path, []), _msg())
    assert out == refused("`to` is required for a message from an orchestrator")
    deliver.assert_not_called()


def _child_ctx(tmp_path: Path, orch_listed: bool = True) -> ActionContext:
    orch = _node("orch", **{labels.GRANT: Grant().to_label()})
    return _ctx(tmp_path, _child_of_orch(), [orch] if orch_listed else [])


def test_child_question_relayed_to_orchestrator(tmp_path: Path, deliver: MagicMockT) -> None:
    out = messages.handle_message(
        _child_ctx(tmp_path), _msg(kind="question", body="which db?", id="q1")
    )
    assert deliver.call_args.args[2] == "myrepo-orch"
    msg = deliver.call_args.args[3]
    assert (msg.from_, msg.kind, msg.body) == ("fix-1", "question", "which db?")
    assert deliver.call_args.kwargs["seq"] == 0
    assert out == done("delivered to orch")


def test_child_message_refused_when_orchestrator_gone(tmp_path: Path, deliver: MagicMockT) -> None:
    out = messages.handle_message(_child_ctx(tmp_path, orch_listed=False), _msg())
    assert out == refused("your orchestrator orch no longer exists; message was not queued")
    deliver.assert_not_called()


def test_missing_recipient_identity_refuses_message_and_done(
    tmp_path: Path, deliver: MagicMockT
) -> None:
    ctx = _child_ctx(tmp_path)
    ctx.nodes["myrepo-orch"].config.pop("volatile.uuid")
    assert messages.handle_message(ctx, _msg()) == refused(
        "cannot identify orch; message was not queued"
    )
    assert messages.handle_message(ctx, _msg(kind="done")) == refused(
        "cannot identify orch; done report was not queued"
    )
    deliver.assert_not_called()


def test_child_may_name_its_orchestrator_in_to(tmp_path: Path, deliver: MagicMockT) -> None:
    out = messages.handle_message(_child_ctx(tmp_path), _msg(to="orch"))
    assert out == done("delivered to orch")


def test_child_naming_another_container_refused(tmp_path: Path, deliver: MagicMockT) -> None:
    out = messages.handle_message(_child_ctx(tmp_path), _msg(to="other"))
    assert out == refused("a child can only message its orchestrator")
    deliver.assert_not_called()


def test_child_note_default_kind(tmp_path: Path, deliver: MagicMockT) -> None:
    messages.handle_message(_child_ctx(tmp_path), _msg())
    assert deliver.call_args.args[3].kind == "note"


def test_child_done_goes_to_handle_done(
    tmp_path: Path, deliver: MagicMockT, mocker: MagicMockT
) -> None:
    handler = mocker.patch(
        "jailbee.orchestration.messages.handle_done", return_value=done("checked")
    )
    ctx = _child_ctx(tmp_path)
    m = _msg(kind="done")
    assert messages.handle_message(ctx, m) == done("checked")
    handler.assert_called_once_with(ctx, m)
    deliver.assert_not_called()


def test_child_done_naming_another_container_refused(
    tmp_path: Path, deliver: MagicMockT, mocker: MagicMockT
) -> None:
    handler = mocker.patch("jailbee.orchestration.messages.handle_done")
    out = messages.handle_message(_child_ctx(tmp_path), _msg(kind="done", to="other"))
    assert out == refused("a child can only message its orchestrator")
    handler.assert_not_called()


# -- reply correlation across the relay ------------------------------------


def _inbox(tmp_path: Path, short: str) -> list[dict[str, object]]:
    from jailbee.orchestration.store import read_items

    return [s.item for s in read_items(tmp_path, f"myrepo-{short}")]


def _pair(tmp_path: Path) -> tuple[ActionContext, ActionContext]:
    """(child ctx, orchestrator ctx) sharing one real broker store."""
    orch = _node("orch", **{labels.GRANT: Grant().to_label()})
    child = _child_of_orch()
    return _ctx(tmp_path, child, [orch]), _ctx(tmp_path, orch, [child])


def test_relayed_item_carries_the_senders_manifest_id(tmp_path: Path) -> None:
    child, _ = _pair(tmp_path)
    messages.handle_message(child, _msg(kind="question", id="message-q-37b9"))
    [item] = _inbox(tmp_path, "orch")
    assert str(item["id"]).startswith("h-")
    assert item["origin_id"] == "message-q-37b9"


def test_orchestrator_reply_by_host_id_reaches_child_as_its_own_id(tmp_path: Path) -> None:
    child, orch = _pair(tmp_path)
    messages.handle_message(child, _msg(kind="question", id="message-q-37b9"))
    [question] = _inbox(tmp_path, "orch")
    messages.handle_message(orch, _msg(to="fix-1", id="a1", in_reply_to=str(question["id"])))
    [answer] = _inbox(tmp_path, "fix-1")
    assert answer["in_reply_to"] == "message-q-37b9"


def test_child_reply_by_host_id_reaches_orchestrator_as_its_own_id(tmp_path: Path) -> None:
    child, orch = _pair(tmp_path)
    messages.handle_message(orch, _msg(to="fix-1", kind="question", id="orch-q-1"))
    [question] = _inbox(tmp_path, "fix-1")
    assert question["origin_id"] == "orch-q-1"
    messages.handle_message(child, _msg(id="c-answer", in_reply_to=str(question["id"])))
    [answer] = _inbox(tmp_path, "orch")
    assert answer["in_reply_to"] == "orch-q-1"


def test_reply_by_original_id_passes_through(tmp_path: Path) -> None:
    child, orch = _pair(tmp_path)
    messages.handle_message(child, _msg(kind="question", id="message-q-37b9"))
    messages.handle_message(orch, _msg(to="fix-1", id="a1", in_reply_to="message-q-37b9"))
    [answer] = _inbox(tmp_path, "fix-1")
    assert answer["in_reply_to"] == "message-q-37b9"


def test_unknown_reply_ref_passes_through(tmp_path: Path) -> None:
    _, orch = _pair(tmp_path)
    messages.handle_message(orch, _msg(to="fix-1", id="a1", in_reply_to="h-deadbeef"))
    [answer] = _inbox(tmp_path, "fix-1")
    assert answer["in_reply_to"] == "h-deadbeef"


def test_reply_ref_naming_an_item_from_someone_else_passes_through(tmp_path: Path) -> None:
    """Only an item the recipient itself sent is mapped back to its author's id."""
    from jailbee.orchestration.store import BrokerStore, store_path

    _, orch = _pair(tmp_path)
    BrokerStore(store_path(tmp_path)).insert_item(
        "uuid-orch",
        "myrepo-orch",
        {
            "version": 1,
            "id": "h-other",
            "type": "message",
            "from": "fix-2",
            "body": "x",
            "origin_id": "fix2-q",
        },
        now=1.0,
    )
    messages.handle_message(orch, _msg(to="fix-1", id="a1", in_reply_to="h-other"))
    [answer] = _inbox(tmp_path, "fix-1")
    assert answer["in_reply_to"] == "h-other"


def test_message_without_reply_ref_still_carries_its_origin(tmp_path: Path) -> None:
    _, orch = _pair(tmp_path)
    messages.handle_message(orch, _msg(to="fix-1", id="a1"))
    [answer] = _inbox(tmp_path, "fix-1")
    assert answer["in_reply_to"] is None and answer["origin_id"] == "a1"


def test_host_items_without_origin_omit_the_key() -> None:
    from jailbee.orchestration.protocol import InboxMessage

    msg = InboxMessage.model_validate(
        {"version": 1, "id": "h-1", "type": "message", "from": "broker", "body": "x"}
    )
    assert "origin_id" not in msg.model_dump(by_alias=True)


def test_parked_relay_keeps_origin_and_mapped_reply_through_flush(tmp_path: Path) -> None:
    """A stopped-recipient delivery parks the item; the flush keeps both ids."""
    from jailbee.orchestration import outbox
    from jailbee.orchestration.state import pending_dir

    child, orch = _pair(tmp_path)
    messages.handle_message(child, _msg(kind="question", id="message-q-37b9"))
    [question] = _inbox(tmp_path, "orch")
    # An older parked item makes the next delivery queue behind it.
    outbox.save_pending(
        pending_dir(tmp_path, "myrepo-fix-1") / "00000000T000000Z-00000000-h-older.json",
        {"version": 1, "id": "h-older", "type": "message", "from": "orch", "body": "older"},
        recipient_uuid="uuid-fix-1",
    )
    out = messages.handle_message(orch, _msg(to="fix-1", id="a1", in_reply_to=str(question["id"])))
    assert out.state == "queued"
    assert _inbox(tmp_path, "fix-1") == []
    outbox.flush_pending(
        MagicMock(), tmp_path, "myrepo-fix-1", uid=None, recipient_uuid="uuid-fix-1"
    )
    [_, answer] = _inbox(tmp_path, "fix-1")
    assert (answer["in_reply_to"], answer["origin_id"]) == ("message-q-37b9", "a1")
