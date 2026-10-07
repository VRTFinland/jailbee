"""Native outbox inspection uses real views/plans and short-lived Qt workers."""

import json
from dataclasses import replace
from threading import Event

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QThread, QTimer
from PySide6.QtWidgets import QMessageBox

from jailbee.dashboard.model import RepoTarget
from jailbee.outbox.inspect import build_views
from jailbee.outbox.models import ContainerView
from jailbee.outbox_io import JournalStore
from tests.outbox_support import IDENTITY, issue_files, pr_files, store


def views(tmp_path, *, files=None):
    snapshots = (
        store("pr", pr_files()),
        store("issue", issue_files() if files is None else files),
    )
    return ContainerView(
        IDENTITY,
        IDENTITY.full_name,
        True,
        None,
        snapshots,
        build_views(IDENTITY, snapshots, journal_store=JournalStore(tmp_path / "journal")),
    )


@pytest.fixture
def env(qtbot, mocker, make_cfg, tmp_path):
    from jailbee.qtui import outbox

    cfg = make_cfg(tmp_path)
    target = RepoTarget(cfg.repo_root, None)
    incus = mocker.Mock()
    mocker.patch.object(outbox, "Incus", return_value=incus)
    mocker.patch.object(outbox, "JournalStore", return_value=JournalStore(tmp_path / "journal"))
    loader = mocker.patch.object(outbox.config_api, "load_repo_config", return_value=cfg)
    resolver = mocker.patch.object(
        outbox.commands, "resolve_target", return_value=(cfg, IDENTITY.full_name)
    )
    view = views(tmp_path)
    read = mocker.patch.object(outbox.service, "load_container", return_value=view)
    mutate = mocker.patch.object(outbox.service, "execute_delete", return_value=())
    dialog = outbox.OutboxDialog(target, IDENTITY.full_name)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: not dialog.busy)
    yield dialog, view, read, mutate, loader, resolver, cfg, incus
    dialog.close()
    qtbot.waitUntil(lambda: not dialog.busy)


def select(dialog, proposal=0, action=None, comment=None):
    item = dialog.tree.topLevelItem(proposal)
    if action is not None:
        item = item.child(action)
    if comment is not None:
        item = item.child(comment)
    dialog.tree.setCurrentItem(item)


def test_plain_full_text_and_zero_based_children(env):
    dialog = env[0]
    select(dialog)
    assert env[1].proposals[0].raw_text in dialog.details.toPlainText()
    assert dialog.details.isReadOnly()
    select(dialog, action=0, comment=1)
    assert dialog.details.toPlainText() == "Second"
    assert "Action 0" in dialog.tree.topLevelItem(0).child(0).text(0)
    assert "Comment 0" in dialog.tree.topLevelItem(0).child(0).child(0).text(0)
    assert "all pending" in dialog.publish_button.text().lower()


def test_long_html_like_body_is_literal_and_not_truncated(env, qtbot):
    dialog, view, read, *_ = env
    text = "<b>literal & [red]</b>\n" + "full text\n" * 6000
    action = replace(view.proposals[0].actions[0], title=None, body=text)
    proposal = replace(view.proposals[0], actions=(action,))
    read.return_value = replace(view, proposals=(proposal,))
    dialog.refresh()
    qtbot.waitUntil(lambda: not dialog.busy)
    select(dialog, action=0)
    assert dialog.details.toPlainText() == text


@pytest.mark.parametrize("state", ["empty", "unavailable", "invalid", "progress"])
def test_empty_invalid_and_progress_states(env, qtbot, tmp_path, state):
    dialog, view, read, *_ = env
    if state == "empty":
        new = replace(view, proposals=())
    elif state == "unavailable":
        new = ContainerView(None, view.name, False, "unavailable", (), ())
    elif state == "invalid":
        new = views(tmp_path, files={"001.json": "{bad"})
    else:
        proposal = replace(view.proposals[0], edit_block="publication progress", state="partial")
        new = replace(view, proposals=(proposal,))
    read.return_value = new
    dialog.refresh()
    qtbot.waitUntil(lambda: not dialog.busy)
    if state in ("empty", "unavailable"):
        assert not dialog.delete_button.isEnabled()
        assert not dialog.publish_button.isEnabled()
        assert (
            state in dialog.status.text().lower() or "no proposals" in dialog.status.text().lower()
        )
    elif state == "progress":
        select(dialog, action=0)
        assert not dialog.delete_button.isEnabled()
        assert (
            "publication progress" in dialog.details.toPlainText()
            or "publication progress" in dialog.status.text()
        )
    else:
        select(dialog, proposal=1)
        assert not dialog.publish_button.isEnabled()
        assert dialog.delete_button.isEnabled()  # Safe whole-manifest deletion.


def test_child_delete_uses_shared_plan_frozen_revision_and_reload(env, qtbot, mocker):
    dialog, view, read, mutate, *_ = env
    question = mocker.patch.object(
        QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
    )
    select(dialog, action=0, comment=0)
    with qtbot.waitSignal(dialog.changed):
        dialog.delete_selected()
    qtbot.waitUntil(lambda: not dialog.busy)
    plan = mutate.call_args.args[3]
    assert plan.selection.action == 0 and plan.selection.comment == 0
    assert plan.removed_comments == ((0, 0),)
    assert plan.expected_revision == view.proposals[0].revision
    assert mutate.call_args.kwargs["lock_timeout"] == 2.0
    assert json.loads(plan.new_text)["actions"][0]["comments"][0]["body"] == "Second"
    assert question.call_args.args[4] == QMessageBox.StandardButton.No
    assert read.call_count >= 2


@pytest.mark.parametrize("accept", [False, True])
def test_cascade_preview_and_cancel(env, qtbot, mocker, accept):
    dialog, _, _, mutate, *_ = env
    question = mocker.patch.object(
        QMessageBox,
        "question",
        return_value=QMessageBox.StandardButton.Yes if accept else QMessageBox.StandardButton.No,
    )
    select(dialog, proposal=1, action=0)
    dialog.delete_selected()
    qtbot.waitUntil(lambda: not dialog.busy)
    assert "(0, 1)" in question.call_args.args[2]
    if accept:
        assert mutate.call_args.args[3].removed_actions == (0, 1)
    else:
        mutate.assert_not_called()


def test_confirmation_freezes_snapshot_and_blocks_reentrant_edits(env, qtbot, mocker):
    dialog, view, read, mutate, *_ = env
    select(dialog, action=0, comment=0)

    def confirming(*args):
        assert not dialog.delete_button.isEnabled()
        assert not dialog.publish_button.isEnabled()
        read.return_value = replace(view, proposals=())
        dialog.refresh()
        dialog.delete_selected()
        assert dialog.tree.topLevelItemCount() == 2
        return QMessageBox.StandardButton.Yes

    mocker.patch.object(QMessageBox, "question", side_effect=confirming)
    dialog.delete_selected()
    qtbot.waitUntil(lambda: not dialog.busy)
    assert mutate.call_count == 1
    assert mutate.call_args.args[3].expected_revision == view.proposals[0].revision
    assert dialog.tree.topLevelItemCount() == 0


@pytest.mark.parametrize("changed", [False, True])
def test_refresh_retains_child_only_when_revision_matches(env, qtbot, changed):
    dialog, view, read, *_ = env
    select(dialog, action=0, comment=1)
    if changed:
        proposal = replace(view.proposals[0], revision="a" * 64)
        read.return_value = replace(view, proposals=(proposal, *view.proposals[1:]))
    dialog.refresh()
    qtbot.waitUntil(lambda: not dialog.busy)
    current = dialog.tree.currentItem()
    assert current.parent() is None if changed else current.text(0).startswith("Comment 1")


def test_publish_child_requests_whole_manifest_and_reloads_on_return(env, qtbot):
    dialog, view, read, *_ = env
    select(dialog, action=0, comment=0)
    with qtbot.waitSignal(dialog.publishRequested) as emitted:
        dialog.publish_selected()
    assert emitted.args == ["pr/001.json", view.proposals[0].revision]
    before = read.call_count
    dialog.publication_started()
    dialog.event(QEvent(QEvent.Type.WindowActivate))
    qtbot.waitUntil(lambda: read.call_count > before and not dialog.busy)


@pytest.mark.parametrize("operation", ["load", "delete"])
def test_blocked_worker_close_is_responsive_and_preserves_delete_completion(
    env, qtbot, mocker, operation
):
    dialog, _, read, mutate, *_ = env
    entered, release = Event(), Event()
    changed, retired, ticks, threads = [], [], [], []
    dialog.changed.connect(lambda: changed.append(QThread.currentThread()))
    dialog.retired.connect(lambda: retired.append(True))
    old = dialog.details.toPlainText()

    def blocked(*args, **kwargs):
        threads.append(QThread.currentThread())
        entered.set()
        assert release.wait(3)
        return read.return_value if operation == "load" else ()

    (read if operation == "load" else mutate).side_effect = blocked
    mocker.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes)
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start(5)
    try:
        if operation == "load":
            dialog.refresh()
        else:
            select(dialog, action=0, comment=0)
            old = dialog.details.toPlainText()
            dialog.delete_selected()
        qtbot.waitUntil(entered.is_set)
        dialog.close()
        assert dialog.closing and dialog.busy
        assert not retired
        qtbot.waitUntil(lambda: bool(ticks))
        assert not dialog.isVisible()
        release.set()
        qtbot.waitUntil(lambda: bool(retired))
        assert dialog.details.toPlainText() == old
        assert len(changed) == (1 if operation == "delete" else 0)
        assert all(thread is QThread.currentThread() for thread in changed)
        assert threads[0] is not QThread.currentThread()
    finally:
        release.set()
        timer.stop()
        qtbot.waitUntil(lambda: not dialog.busy)


def test_obsolete_load_is_discarded_and_refreshes_are_coalesced(env, qtbot):
    dialog, view, read, *_ = env
    entered, release = Event(), Event()
    calls = []

    def loading(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
            return replace(view, proposals=())
        return view

    read.side_effect = loading
    try:
        dialog.refresh()
        qtbot.waitUntil(entered.is_set)
        for _ in range(10):
            dialog.refresh()
        release.set()
        qtbot.waitUntil(lambda: not dialog.busy)
        assert len(calls) == 2
        assert dialog.tree.topLevelItemCount() == 2
    finally:
        release.set()
        qtbot.waitUntil(lambda: not dialog.busy)


def test_worker_loads_target_config_off_ui_and_rechecks_for_delete(env, qtbot, mocker, tmp_path):
    dialog, _, _read, mutate, loader, resolver, cfg, incus = env
    seen = []
    loader.side_effect = lambda root: seen.append((root, QThread.currentThread())) or cfg
    foreign = cfg.model_copy(
        update={"container_user": cfg.container_user.model_copy(update={"uid": 1234})}
    )
    resolver.return_value = (foreign, IDENTITY.full_name)
    select(dialog, action=0, comment=0)
    mocker.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes)
    dialog.delete_selected()
    qtbot.waitUntil(lambda: not dialog.busy)
    assert seen and all(
        root == cfg.repo_root and thread is not QThread.currentThread() for root, thread in seen
    )
    assert mutate.call_args.args[:2] == (foreign, incus)
    assert resolver.call_count >= 3


def test_delete_config_drift_and_mutation_error_remain_visible(env, qtbot, mocker):
    dialog, _, _, mutate, _, resolver, cfg, _ = env
    select(dialog, action=0, comment=0)
    resolver.side_effect = [
        (cfg, IDENTITY.full_name),
        (cfg.model_copy(update={"container_prefix": "changed"}), IDENTITY.full_name),
    ]
    mocker.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes)
    dialog.delete_selected()
    qtbot.waitUntil(lambda: not dialog.busy)
    mutate.assert_not_called()
    assert "changed" in dialog.status.text().lower()


def test_load_failure_clears_stale_selection(env, qtbot):
    dialog, _, read, *_ = env
    select(dialog, action=0)
    read.side_effect = OSError("unavailable")
    dialog.refresh()
    qtbot.waitUntil(lambda: not dialog.busy)
    assert not dialog.delete_button.isEnabled()
    assert not dialog.publish_button.isEnabled()
    assert dialog.tree.topLevelItemCount() == 0
    assert "unavailable" in dialog.status.text()


def test_close_during_confirmation_cancels_mutation(env, qtbot, mocker):
    dialog, _, _, mutate, *_ = env
    select(dialog, action=0, comment=0)

    def closing(*args):
        dialog.close()
        return QMessageBox.StandardButton.Yes

    mocker.patch.object(QMessageBox, "question", side_effect=closing)
    dialog.delete_selected()
    mutate.assert_not_called()
    assert dialog.closing and not dialog.busy


def test_real_shared_deletion_changes_only_selected_comment(env, qtbot, mocker):
    from jailbee.qtui import outbox

    dialog, view, read, mutate, _, _, cfg, incus = env
    mocker.stop(read)
    mocker.stop(mutate)
    snapshots = {s.kind: s for s in view.stores}
    incus.list_containers.return_value = [
        {"name": IDENTITY.full_name, "created_at": IDENTITY.created_at}
    ]
    mocker.patch.object(
        outbox.service, "read_store", side_effect=lambda i, c, kind, **kw: snapshots[kind]
    )
    management = mocker.patch.object(outbox.service, "PrManagement")
    from jailbee.outbox.io import PrManagement

    management.return_value = PrManagement(cfg.repo_root / "pr-locks")
    updated = []

    def mutation(i, c, kind, **kwargs):
        updated.append(kwargs["new_manifest"])
        files = snapshots[kind].as_dict()
        name, text = kwargs["new_manifest"]
        files[name] = text
        snapshots[kind] = store(kind, files)
        return kwargs["delete_names"]

    mocker.patch.object(outbox.service, "mutate_store", side_effect=mutation)
    select(dialog, action=0, comment=0)
    mocker.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes)
    with qtbot.waitSignal(dialog.changed):
        dialog.delete_selected()
    qtbot.waitUntil(lambda: not dialog.busy)
    assert len(updated) == 1
    assert json.loads(updated[0][1])["actions"][0]["comments"] == [
        {"path": "a.py", "line": 2, "body": "Second"}
    ]
    assert dialog.tree.topLevelItem(0).child(0).childCount() == 1


def test_ambiguous_mutation_failure_still_notifies_controller(env, qtbot):
    from jailbee.outbox.models import OutboxExecutionError

    dialog, _, _, mutate, *_ = env
    select(dialog, action=0, comment=0)
    mutate.side_effect = OutboxExecutionError("incomplete deletion; journal retained")
    # Avoid a modal confirmation while preserving the real deletion path.
    dialog._confirming = False
    from unittest.mock import patch

    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
        with qtbot.waitSignal(dialog.changed):
            dialog.delete_selected()
    qtbot.waitUntil(lambda: not dialog.busy)
    assert "journal retained" in dialog.status.text()


def test_direct_native_window_refuses_any_ssh(qtbot, monkeypatch, tmp_path):
    from jailbee.qtui.outbox import OutboxDialog

    monkeypatch.setenv("JAILBEE_SSH_SESSION", "1")
    with pytest.raises(ValueError, match="SSH"):
        OutboxDialog(RepoTarget(tmp_path, None), IDENTITY.full_name)
