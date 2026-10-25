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
    return Node(full=f"myrepo-{short}", short=short, running=running, config=config)


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
    assert out == Outcome("queued", "fix-1 is not running; the message is delivered when it starts")
    assert deliver.call_args.kwargs["running"] is False


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


def test_child_message_queued_when_orchestrator_gone(tmp_path: Path, deliver: MagicMockT) -> None:
    deliver.return_value = "parked"
    out = messages.handle_message(_child_ctx(tmp_path, orch_listed=False), _msg())
    assert deliver.call_args.args[2] == "myrepo-orch"
    assert deliver.call_args.kwargs["running"] is False
    assert out == Outcome("queued", "orch is not running; the message is delivered when it starts")


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
