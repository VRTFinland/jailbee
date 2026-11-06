from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from jailbee.git import GitError
from jailbee.global_config import GlobalConfig
from jailbee.incus import IncusError
from jailbee.orchestration import importing, labels, messages
from jailbee.orchestration.actions import (
    CHILD_EXEC_TIMEOUT_S,
    CHILD_TRANSPORT_TIMEOUT_S,
    ActionContext,
    Node,
    Outcome,
    done,
    refused,
)
from jailbee.orchestration.grant import Grant
from jailbee.orchestration.protocol import MessageManifest
from jailbee.orchestration.state import pending_dir
from jailbee.sync import FetchResult, SyncError
from tests.conftest import make_config

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

SHA = "a" * 40
CHILD = "myrepo-fix-1"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JAILBEE_ROLE", raising=False)


class Rig:
    def __init__(self, ctx: ActionContext, mocker: MockerFixture) -> None:
        from jailbee.orchestration.child_outbox import PendingGithub

        self.outbox_probe = mocker.patch(
            "jailbee.orchestration.child_outbox.probe", return_value=PendingGithub((), (), ())
        )
        self.ctx = ctx
        self.incus: MagicMock = ctx.incus  # type: ignore[assignment]  # built as a MagicMock
        self.dirty = mocker.patch(
            "jailbee.orchestration.messages.sync._container_status_dirty", return_value=False
        )
        self.branch = mocker.patch(
            "jailbee.orchestration.messages.sync._container_current_branch",
            return_value="fix-1",
        )
        self.fetch = mocker.patch(
            "jailbee.orchestration.messages.sync.fetch_from_container",
            return_value=FetchResult(
                branch="fix-1", old_oid=None, new_oid=SHA, base_oid=None, commits_added=1
            ),
        )
        self.repo_dir = mocker.patch(
            "jailbee.orchestration.messages.lifecycle.container_repo_dir",
            return_value="/home/dev/repo",
        )
        self.import_child = mocker.patch(
            "jailbee.orchestration.importing.import_child",
            return_value=importing.Imported("refs/jailbee/from/fix-1/fix-1", SHA),
        )
        self.head = mocker.patch("jailbee.orchestration.actions._child_head", return_value=SHA)
        self.insert_store = mocker.patch("jailbee.orchestration.outbox._insert_into_store", return_value=True)
        self.deliver = mocker.patch(
            "jailbee.orchestration.messages.deliver", return_value="delivered"
        )


def _make(tmp_path: Path, mocker: MockerFixture, orch_running: bool = True) -> Rig:
    orch = Node("myrepo-orch", "orch", orch_running, {labels.GRANT: Grant().to_label()})
    sender = Node(CHILD, "fix-1", True, {labels.SPAWNED_BY: "myrepo-orch"})
    ctx = ActionContext(
        cfg=make_config(tmp_path, container_prefix="myrepo"),
        gcfg=GlobalConfig(),
        incus=MagicMock(),
        root=tmp_path,
        sender=sender,
        nodes={orch.full: orch, sender.full: sender},
        now=datetime(2026, 10, 25, tzinfo=UTC),
    )
    return Rig(ctx, mocker)


def _done(**kw: Any) -> MessageManifest:
    base: dict[str, Any] = {
        "version": 1,
        "id": "d1",
        "type": "message",
        "kind": "done",
        "body": "all finished",
    }
    base.update(kw)
    return MessageManifest.model_validate(base)


@pytest.mark.parametrize("count", ["3", "0", "", "invalid"])
def test_queued_done_reports_stopped_outbox(tmp_path, mocker, count):
    from dataclasses import replace

    from jailbee.orchestration import finishing
    from jailbee.orchestration.protocol import PendingFinish

    rig = _make(tmp_path, mocker)
    child = replace(
        rig.ctx.sender,
        running=False,
        config={**rig.ctx.sender.config, labels.STOPPED_OUTBOX: count},
    )
    orch = rig.ctx.nodes["myrepo-orch"]
    item = PendingFinish(
        version=1,
        type="finish",
        id="done1",
        child_full=CHILD,
        child_short="fix-1",
        branch="fix-1",
        done_sha=SHA,
        body="done",
        in_reply_to=None,
        uid=None,
    )
    msg, _ = finishing.complete(rig.ctx, child, orch, item)
    rig.outbox_probe.assert_not_called()
    assert msg.on_done == "keep"
    if count == "0":
        assert msg.on_done_reason is None
    else:
        assert "outbox" in msg.on_done_reason
        assert msg.on_done_reason in msg.body
        assert ("3 staged GitHub" if count == "3" else "could not be checked") in msg.on_done_reason


def _untouched(rig: Rig) -> None:
    rig.incus.config_set.assert_not_called()
    rig.deliver.assert_not_called()


def _policy(rig: Rig, policy: str) -> None:
    rig.ctx.sender = Node(CHILD, "fix-1", True, {**rig.ctx.sender.config, labels.ON_DONE: policy})


@pytest.mark.parametrize("unsafe_part", ["filename", "verb"])
def test_done_with_surrogate_outbox_text_is_retained_and_serializable(
    tmp_path, mocker, monkeypatch, unsafe_part
):
    import contextlib
    import io
    import json
    import os

    from jailbee.orchestration import child_outbox
    from jailbee.orchestration.protocol import InboxMessage

    real_probe = child_outbox.probe
    rig = _make(tmp_path, mocker)
    _policy(rig, "destroy")
    issue = tmp_path / "issue-outbox"
    issue.mkdir()
    raw_name = b"bad\xff.json" if unsafe_part == "filename" else b"action.json"
    name = os.fsdecode(raw_name)
    verb = "comment" if unsafe_part == "filename" else "bad\udcffverb"
    text = json.dumps({"actions": [{"type": verb}]})
    fd = os.open(os.fsencode(issue) + b"/" + raw_name, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)

    def discover(*args, **kwargs):
        output = io.StringIO()
        with monkeypatch.context() as scoped:
            scoped.setattr("sys.argv", ["probe", str(issue), str(tmp_path / "pr-outbox"), "128"])
            with contextlib.redirect_stdout(output):
                exec(compile(child_outbox._NAMES_SCRIPT, "<child outbox probe>", "exec"), {})
        return output.getvalue()

    rig.incus.with_exec_timeout.return_value.exec_capped.side_effect = discover
    mocker.patch("jailbee.outbox_io.read_text_outbox", side_effect=[{name: text}, {}])
    pending = real_probe(
        rig.incus.with_exec_timeout.return_value, CHILD, uid=rig.ctx.cfg.container_user.uid
    )
    assert pending.count == 1 and pending.issue == (name,)
    rig.outbox_probe.return_value = pending
    messages.handle_message(rig.ctx, _done())
    msg = rig.insert_store.call_args.args[2]
    assert msg.on_done == "keep"
    assert "1 staged GitHub action(s)" in msg.on_done_reason
    assert "force: true" in msg.body
    assert labels.PENDING_DESTROY not in [c.args[1] for c in rig.incus.config_set.call_args_list]
    serialized = msg.model_dump_json(by_alias=True)
    delivered = InboxMessage.model_validate_json(serialized)
    assert "\\udcff" in delivered.body
    assert ("bad\\udcff.json" if unsafe_part == "filename" else "bad\\udcffverb") in delivered.body


@pytest.mark.parametrize("policy", ["keep", "destroy"])
@pytest.mark.parametrize("import_error", [False, True])
def test_every_done_reports_staged_github_actions(tmp_path, mocker, policy, import_error):
    from jailbee.orchestration.child_outbox import PendingGithub

    rig = _make(tmp_path, mocker)
    _policy(rig, policy)
    if import_error:
        rig.import_child.return_value = "import failed"
    pending = PendingGithub(("003-x.json",), (), ('issue/003-x.json: create "Title"',))
    probe = mocker.patch("jailbee.orchestration.child_outbox.probe", return_value=pending)
    out = messages.handle_message(rig.ctx, _done())
    msg = rig.insert_store.call_args.args[2]
    assert msg.on_done == "keep"
    assert msg.on_done_reason == (
        "child `fix-1` has 1 staged GitHub action(s) in its outbox (issue: 003-x.json); "
        "they are never published from a child — ask it to send the content, "
        "then destroy with force: true"
    )
    assert 'issue/003-x.json: create "Title"' in msg.body
    assert "staged GitHub" in out.detail
    assert labels.PENDING_DESTROY not in [c.args[1] for c in rig.incus.config_set.call_args_list]
    probe.assert_called_once_with(
        rig.incus.with_exec_timeout.return_value, CHILD, uid=rig.ctx.cfg.container_user.uid
    )


@pytest.mark.parametrize("policy", ["keep", "destroy"])
def test_done_unknown_outbox_is_reported_and_never_schedules_destroy(tmp_path, mocker, policy):
    from jailbee.orchestration.child_outbox import ChildOutboxError

    rig = _make(tmp_path, mocker)
    _policy(rig, policy)
    mocker.patch("jailbee.orchestration.child_outbox.probe", side_effect=ChildOutboxError("read"))
    messages.handle_message(rig.ctx, _done())
    msg = rig.insert_store.call_args.args[2]
    assert msg.on_done == "keep" and "outbox could not be checked" in msg.on_done_reason
    assert labels.PENDING_DESTROY not in [c.args[1] for c in rig.incus.config_set.call_args_list]


def test_done_imports_then_delivers_then_marks_destroy(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    rig = _make(tmp_path, mocker)
    _policy(rig, "destroy")
    order = MagicMock()
    order.attach_mock(rig.import_child, "import_child")
    order.attach_mock(rig.insert_store, "insert_store")
    order.attach_mock(rig.incus.config_set, "set")
    out = messages.handle_message(rig.ctx, _done())
    calls = [
        c[0]
        for c in order.mock_calls
        if c[0] in {"import_child", "insert_store"}
        or (c[0] == "set" and c.args[1] == labels.PENDING_DESTROY)
    ]
    assert calls == ["import_child", "set", "insert_store"]
    msg = rig.insert_store.call_args.args[2]
    assert msg.imported_sha == SHA and msg.on_done == "destroy"
    assert out.state == "done" and "will be destroyed" in out.detail


def test_done_durable_finish_survives_crash_before_delivery(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    rig = _make(tmp_path, mocker)
    _policy(rig, "destroy")
    rig.insert_store.side_effect = RuntimeError("broker crashed")
    with pytest.raises(RuntimeError, match="broker crashed"):
        messages.handle_message(rig.ctx, _done())
    assert list(pending_dir(tmp_path, "myrepo-orch").glob("*.finish.json"))


def test_done_keep_policy_imports_without_marker(tmp_path: Path, mocker: MockerFixture) -> None:
    rig = _make(tmp_path, mocker)
    _policy(rig, "keep")
    out = messages.handle_message(rig.ctx, _done())
    assert "is kept" in out.detail
    assert labels.PENDING_DESTROY not in [c.args[1] for c in rig.incus.config_set.call_args_list]


def test_done_import_failure_delivers_with_error_and_keeps(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    rig = _make(tmp_path, mocker)
    _policy(rig, "destroy")
    rig.import_child.return_value = "boom"
    out = messages.handle_message(rig.ctx, _done())
    msg = rig.insert_store.call_args.args[2]
    assert msg.import_error == "boom" and msg.on_done == "keep"
    assert "import failed: boom" in out.detail
    assert labels.PENDING_DESTROY not in [c.args[1] for c in rig.incus.config_set.call_args_list]


def test_done_without_destroy_grant_at_completion_is_a_kept_import_not_an_error(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    rig = _make(tmp_path, mocker)
    _policy(rig, "destroy")
    orch = rig.ctx.nodes["myrepo-orch"]
    grant = Grant(operations=("spawn", "message", "import"))
    rig.ctx.nodes = {
        CHILD: rig.ctx.sender,
        orch.full: Node(orch.full, orch.short, True, {labels.GRANT: grant.to_label()}),
    }
    out = messages.handle_message(rig.ctx, _done())
    msg = rig.insert_store.call_args.args[2]
    assert msg.import_error is None and msg.on_done == "keep"
    assert msg.imported_sha == SHA
    assert msg.on_done_reason and "destroy permission" in msg.on_done_reason
    assert "is kept (" in out.detail and "destroy permission" in out.detail
    assert "import failed" not in out.detail


def test_done_marker_failure_is_a_kept_import_not_an_error(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    rig = _make(tmp_path, mocker)
    _policy(rig, "destroy")

    def config_set(full: str, key: str, value: str) -> None:
        if key == labels.PENDING_DESTROY:
            raise IncusError("busy")

    rig.incus.config_set.side_effect = config_set
    out = messages.handle_message(rig.ctx, _done())
    msg = rig.insert_store.call_args.args[2]
    assert msg.import_error is None and msg.on_done == "keep"
    assert msg.imported_sha == SHA
    assert msg.on_done_reason and "could not schedule destruction" in msg.on_done_reason
    assert "this container is kept (" in out.detail
    assert "import failed" not in out.detail


@pytest.mark.parametrize("target_oid", [None, "b" * 40, SHA])
def test_done_checks_target_ref_before_skipping_import(
    tmp_path: Path, mocker: MockerFixture, target_oid: str | None
) -> None:
    rig = _make(tmp_path, mocker)
    rig.ctx.sender = Node(CHILD, "fix-1", True, {**rig.ctx.sender.config, labels.LAST_IMPORT: SHA})
    mocker.patch("jailbee.sync._container_ref_oid", return_value=target_oid)
    messages.handle_message(rig.ctx, _done())
    assert rig.import_child.call_count == (0 if target_oid == SHA else 1)
    assert rig.insert_store.call_args.args[2].imported_sha == SHA


@pytest.mark.parametrize("change", ["head", "branch", "during_import"])
def test_parked_done_keeps_child_changed_since_report(
    tmp_path: Path, mocker: MockerFixture, change: str
) -> None:
    from jailbee.orchestration import finishing
    from jailbee.orchestration.protocol import PendingFinish

    rig = _make(tmp_path, mocker, orch_running=False)
    _policy(rig, "destroy")
    messages.handle_message(rig.ctx, _done())
    path = next(pending_dir(tmp_path, "myrepo-orch").glob("*.finish.json"))
    item = PendingFinish.model_validate_json(path.read_text())
    orch = rig.ctx.nodes["myrepo-orch"]
    if change == "head":
        rig.head.return_value = "b" * 40
    elif change == "branch":
        rig.branch.return_value = "other"
    else:
        rig.import_child.return_value = importing.Imported(
            "refs/jailbee/from/fix-1/fix-1", "b" * 40
        )
    msg, _ = finishing.complete(rig.ctx, rig.ctx.sender, orch, item)
    assert msg.on_done == "keep"
    assert msg.import_error is not None
    assert "changed" in msg.import_error
    assert labels.PENDING_DESTROY not in [c.args[1] for c in rig.incus.config_set.call_args_list]
    if change != "during_import":
        rig.import_child.assert_not_called()


def test_done_keeps_child_when_destroy_grant_revoked(tmp_path: Path, mocker: MockerFixture) -> None:
    rig = _make(tmp_path, mocker)
    _policy(rig, "destroy")
    orch = rig.ctx.nodes["myrepo-orch"]
    grant = Grant(operations=("spawn", "message", "import"))
    rig.ctx.nodes = {
        CHILD: rig.ctx.sender,
        orch.full: Node(orch.full, orch.short, True, {labels.GRANT: grant.to_label()}),
    }
    messages.handle_message(rig.ctx, _done())
    msg = rig.insert_store.call_args.args[2]
    assert msg.on_done == "keep"
    assert labels.PENDING_DESTROY not in [c.args[1] for c in rig.incus.config_set.call_args_list]


def test_done_with_destroyed_orchestrator_refused_and_kept(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    rig = _make(tmp_path, mocker)
    rig.ctx.nodes = {CHILD: rig.ctx.sender}
    out = messages.handle_message(rig.ctx, _done())
    assert out.state == "refused" and "no longer exists" in out.detail
    rig.incus.config_set.assert_any_call(CHILD, labels.DONE_SHA, SHA)
    rig.insert_store.assert_not_called()
    rig.import_child.assert_not_called()


def test_done_parks_behind_existing_pending_items_even_when_running(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    rig = _make(tmp_path, mocker)
    directory = pending_dir(tmp_path, "myrepo-orch")
    directory.mkdir(parents=True)
    earlier = directory / "20261025T000000Z-00000000-a.json"
    earlier.write_text("{}")
    assert messages.handle_message(rig.ctx, _done()).state == "queued"
    rig.import_child.assert_not_called()
    finish = list(directory.glob("*.finish.json"))
    assert len(finish) == 1 and earlier.name < finish[0].name


def test_done_with_dirty_tree_bounced(tmp_path: Path, mocker: MockerFixture) -> None:
    rig = _make(tmp_path, mocker)
    rig.dirty.return_value = True
    out = messages.handle_message(rig.ctx, _done())
    assert out == refused("uncommitted changes: commit everything before reporting done")
    rig.fetch.assert_not_called()
    _untouched(rig)


def test_done_dirty_probe_failure_refused(tmp_path: Path, mocker: MockerFixture) -> None:
    rig = _make(tmp_path, mocker)
    rig.dirty.side_effect = SyncError("probe failed")
    assert messages.handle_message(rig.ctx, _done()) == refused("probe failed")
    rig.fetch.assert_not_called()
    _untouched(rig)


def test_done_repo_dir_failure_refused(tmp_path: Path, mocker: MockerFixture) -> None:
    rig = _make(tmp_path, mocker)
    rig.repo_dir.side_effect = IncusError("gone")
    assert messages.handle_message(rig.ctx, _done()) == refused("gone")
    _untouched(rig)


def test_done_on_detached_head_bounced(tmp_path: Path, mocker: MockerFixture) -> None:
    rig = _make(tmp_path, mocker)
    rig.branch.return_value = None
    out = messages.handle_message(rig.ctx, _done())
    assert out == refused("detached HEAD: check out your branch before reporting done")
    rig.fetch.assert_not_called()
    _untouched(rig)


def test_done_fetches_sets_labels_and_relays(tmp_path: Path, mocker: MockerFixture) -> None:
    rig = _make(tmp_path, mocker)
    out = messages.handle_message(rig.ctx, _done())
    rig.fetch.assert_called_once_with(
        rig.ctx.cfg,
        rig.incus.with_exec_timeout.return_value,
        "fix-1",
        branch="fix-1",
        tags="none",
        timeout=CHILD_TRANSPORT_TIMEOUT_S,
    )
    calls = [c.args for c in rig.incus.config_set.call_args_list]
    assert calls == [(CHILD, labels.DONE_SHA, SHA), (CHILD, labels.STATE, labels.STATE_DONE)]
    msg = rig.insert_store.call_args.args[2]
    assert (msg.kind, msg.body, msg.from_) == ("done", "all finished", "fix-1")
    assert rig.insert_store.call_args.kwargs["recipient_uuid"] is None
    assert out == done(f"imported into orch at {'a' * 12}; this container is kept")


def test_done_while_orchestrator_stopped_still_records_and_queues(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    rig = _make(tmp_path, mocker, orch_running=False)
    rig.deliver.return_value = "parked"
    out = messages.handle_message(rig.ctx, _done())
    assert list(pending_dir(tmp_path, "myrepo-orch").glob("*.finish.json"))
    rig.import_child.assert_not_called()
    rig.incus.config_set.assert_any_call(CHILD, labels.DONE_SHA, SHA)
    rig.incus.config_set.assert_any_call(CHILD, labels.STATE, labels.STATE_DONE)
    assert out == Outcome(
        "queued",
        f"orch is not running or busy; your work is on the host at {'a' * 12} "
        "and is imported when it is delivered",
    )


@pytest.mark.parametrize("exc", [SyncError("boom"), GitError("boom"), IncusError("boom")])
def test_done_fetch_failure_refused_without_labels(
    tmp_path: Path, mocker: MockerFixture, exc: Exception
) -> None:
    rig = _make(tmp_path, mocker)
    rig.fetch.side_effect = exc
    out = messages.handle_message(rig.ctx, _done())
    assert out == refused("could not fetch your branch to the host: boom")
    _untouched(rig)


@pytest.mark.parametrize("oid", ["", "abc123", "A" * 40, "a" * 41])
def test_done_refuses_an_oid_that_is_not_full_hex(
    tmp_path: Path, mocker: MockerFixture, oid: str
) -> None:
    rig = _make(tmp_path, mocker)
    rig.fetch.return_value = FetchResult(
        branch="fix-1", old_oid=None, new_oid=oid, base_oid=None, commits_added=1
    )
    out = messages.handle_message(rig.ctx, _done())
    assert out.state == "refused"
    _untouched(rig)


def test_done_label_write_failure_refused_without_relay(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    rig = _make(tmp_path, mocker)
    rig.incus.config_set.side_effect = IncusError("etag")
    out = messages.handle_message(rig.ctx, _done())
    assert out == refused("could not record the done report: etag")
    rig.deliver.assert_not_called()


def test_done_state_is_written_after_sha(tmp_path: Path, mocker: MockerFixture) -> None:
    rig = _make(tmp_path, mocker)
    rig.incus.config_set.side_effect = [None, IncusError("x")]
    messages.handle_message(rig.ctx, _done())
    keys = [c.args[1] for c in rig.incus.config_set.call_args_list]
    assert keys == [labels.DONE_SHA, labels.STATE]


def test_done_bounds_every_child_probe_and_the_fetch(tmp_path: Path, mocker: MockerFixture) -> None:
    """A hostile child must not stall the single broker loop: every exec into it
    and the ext:: fetch from it run with a timeout."""
    rig = _make(tmp_path, mocker)
    messages.handle_message(rig.ctx, _done())
    assert rig.incus.with_exec_timeout.call_count == 3
    assert all(
        c.args == (CHILD_EXEC_TIMEOUT_S,) for c in rig.incus.with_exec_timeout.call_args_list
    )
    bounded = rig.incus.with_exec_timeout.return_value
    assert rig.repo_dir.call_args.args[1] is bounded
    assert rig.dirty.call_args.args[0] is bounded
    assert rig.branch.call_args.args[0] is bounded
    assert rig.fetch.call_args.args[1] is bounded
    assert rig.fetch.call_args.kwargs["timeout"] == CHILD_TRANSPORT_TIMEOUT_S
    # Labels go through the plain wrapper.
    bounded.config_set.assert_not_called()
    assert rig.incus.config_set.call_count == 2


def test_done_fetch_timeout_refused_without_labels(tmp_path: Path, mocker: MockerFixture) -> None:
    rig = _make(tmp_path, mocker)
    rig.fetch.side_effect = GitError("git fetch timed out after 600s")
    out = messages.handle_message(rig.ctx, _done())
    assert out == refused("could not fetch your branch to the host: git fetch timed out after 600s")
    _untouched(rig)


def test_done_probe_timeout_refused_without_labels(tmp_path: Path, mocker: MockerFixture) -> None:
    rig = _make(tmp_path, mocker)
    rig.repo_dir.side_effect = IncusError("`incus exec ...` timed out after 60s")
    out = messages.handle_message(rig.ctx, _done())
    assert out.state == "refused"
    assert "timed out" in out.detail
    _untouched(rig)


def test_flush_stale_finish_after_parent_transfer_keeps_child(tmp_path, mocker):
    from jailbee.orchestration import finishing, outbox

    rig = _make(tmp_path, mocker, orch_running=False)
    _policy(rig, "destroy")
    messages.handle_message(rig.ctx, _done())
    child = Node(CHILD, "fix-1", True, {labels.SPAWNED_BY: "myrepo-new", labels.ON_DONE: "destroy"})
    old = rig.ctx.nodes["myrepo-orch"]
    rig.ctx.nodes = {CHILD: child, old.full: old}
    rig.ctx.sender = old
    from jailbee.orchestration.store import BrokerStore, store_path
    store = BrokerStore(store_path(tmp_path))
    mocker.stop(rig.insert_store)
    assert (
        outbox.flush_pending(
            rig.incus, tmp_path, old.full, uid=None, finish=finishing.flush_callback(rig.ctx), recipient_uuid="old-uuid"
        )
        == 1
    )
    message = store.unacked("old-uuid")[0].item
    assert message["import_error"] and message["on_done"] == "keep"
    assert not list(pending_dir(tmp_path, old.full).glob("*.finish.json"))
    rig.import_child.assert_not_called()
    from jailbee.orchestration.actions import destroy_guard

    assert destroy_guard(rig.ctx, child) is not None
    assert not any(
        c.args[1] in (labels.LAST_IMPORT, labels.PENDING_DESTROY)
        for c in rig.incus.config_set.call_args_list
    )
