from __future__ import annotations

import pytest

from jailbee.orchestration import actions, outbox
from jailbee.orchestration.protocol import UpdateManifest
from jailbee.sync import SyncError
from tests.test_orchestration_import import CHILD, SHA, _ctx, _result


def _manifest():
    return UpdateManifest(version=1, id="u1", type="update", child="fix-1")


@pytest.fixture
def seams(mocker):
    return {
        "branch": mocker.patch("jailbee.sync._container_current_branch", return_value="main"),
        "repo": mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/r"),
        "merge": mocker.patch(
            "jailbee.sync.merge_container_into_container", return_value=_result()
        ),
        "deliver": mocker.patch.object(outbox, "deliver", return_value="delivered"),
    }


def test_update_transports_and_messages_child(tmp_path, seams):
    ctx = _ctx(tmp_path)
    out = actions.dispatch(ctx, _manifest())
    assert out.state == "done"
    seams["merge"].assert_called_once_with(
        ctx.cfg,
        ctx.incus.with_exec_timeout.return_value,
        "orch",
        "fix-1",
        branch="main",
        plain=True,
        ff_local_branch=False,
        require_clean_target=False,
        timeout=actions.CHILD_TRANSPORT_TIMEOUT_S,
    )
    msg = seams["deliver"].call_args.args[3]
    assert msg.kind == "update" and msg.imported_sha == SHA
    assert seams["deliver"].call_args.args[2] == CHILD
    ctx.incus.config_set.assert_not_called()


def test_update_of_stopped_child_refused(tmp_path, seams):
    out = actions.dispatch(_ctx(tmp_path, child_running=False), _manifest())
    assert out.state == "refused" and "restart it first" in out.detail
    seams["merge"].assert_not_called()


def test_update_with_detached_orchestrator_refused(tmp_path, seams):
    seams["branch"].return_value = None
    out = actions.dispatch(_ctx(tmp_path), _manifest())
    assert out.state == "refused" and "detached HEAD" in out.detail
    seams["merge"].assert_not_called()


def test_update_transport_failure_refused_no_message(tmp_path, seams):
    seams["merge"].side_effect = SyncError("failed")
    out = actions.dispatch(_ctx(tmp_path), _manifest())
    assert out == actions.refused("failed")
    seams["deliver"].assert_not_called()


def test_update_gated_by_import_operation(tmp_path, seams):
    from jailbee.orchestration import labels
    from jailbee.orchestration.grant import Grant

    ctx = _ctx(tmp_path)
    ctx.sender = actions.Node(
        ctx.sender.full,
        "orch",
        True,
        {labels.GRANT: Grant(operations=("spawn", "message")).to_label()},
    )
    out = actions.dispatch(ctx, _manifest())
    assert out.state == "refused" and "operation `import`" in out.detail
    seams["merge"].assert_not_called()


def test_child_cannot_send_update(tmp_path, seams):
    ctx = _ctx(tmp_path)
    ctx.sender = ctx.nodes[CHILD]
    assert actions.dispatch(ctx, _manifest()).state == "refused"
    seams["merge"].assert_not_called()


@pytest.mark.parametrize("oid", ["", "a", "A" * 40])
def test_update_rejects_invalid_import_oid(tmp_path, seams, oid):
    seams["merge"].return_value = _result(oid)
    assert actions.dispatch(_ctx(tmp_path), _manifest()).state == "refused"
    seams["deliver"].assert_not_called()
