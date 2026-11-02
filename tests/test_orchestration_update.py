from __future__ import annotations

from dataclasses import replace

import pytest

from jailbee.orchestration import actions, outbox
from jailbee.orchestration.protocol import UpdateManifest
from jailbee.sync import SyncError
from tests.test_orchestration_import import CHILD, SHA, _ctx, _result


def _manifest(**source):
    return UpdateManifest(version=1, id="u1", type="update", child="fix-1", **source)


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
    assert msg.imported_ref == "refs/jailbee/from/fix-1/fix-1"
    assert seams["deliver"].call_args.args[2] == CHILD
    ctx.incus.config_set.assert_not_called()


@pytest.mark.parametrize("ref", ["refs/heads/release/1", "refs/jailbee/from/worker/fix", "b" * 40])
def test_update_explicit_ref_uses_spawn_source_and_isolated_destination(
    tmp_path, seams, mocker, ref
):
    from jailbee.forking import ForkSource

    ctx = _ctx(tmp_path)
    seams["branch"].return_value = None
    prepare = mocker.patch(
        "jailbee.forking.prepare_fork", return_value=ForkSource("proj-orch", "main", SHA, None)
    )
    push = mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=replace(_result().push, container_ref="refs/jailbee/update/orch/u1"),
    )
    subs = mocker.patch("jailbee.submodules.transport_submodules_to_container")
    out = actions.dispatch(ctx, _manifest(ref=ref))
    assert out.state == "done"
    prepare.assert_called_once_with(
        ctx.cfg,
        ctx.incus.with_exec_timeout.return_value,
        "orch",
        ref=ref,
        timeout=actions.CHILD_TRANSPORT_TIMEOUT_S,
    )
    assert push.call_args.kwargs == {
        "source": "u1",
        "source_ref": SHA,
        "namespace": "update/orch",
        "ff_local_branch": False,
        "timeout": actions.CHILD_TRANSPORT_TIMEOUT_S,
    }
    assert subs.call_args.kwargs["commit"] == SHA
    assert subs.call_args.kwargs["pin_commits"] is True
    seams["merge"].assert_not_called()
    msg = seams["deliver"].call_args.args[3]
    assert msg.imported_ref == "refs/jailbee/update/orch/u1"
    assert msg.imported_sha == SHA


def test_update_base_resolves_with_spawn_base_policy(tmp_path, seams, mocker):
    from jailbee.lifecycle import CloneRef

    ctx = _ctx(tmp_path)
    resolver = mocker.patch(
        "jailbee.lifecycle.resolve_clone_ref", return_value=CloneRef("release/1", False, True, SHA)
    )
    push = mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=replace(_result().push, container_ref="refs/jailbee/update/orch/u1"),
    )
    mocker.patch("jailbee.submodules.transport_submodules_to_container")
    assert actions.dispatch(ctx, _manifest(base="release/1")).state == "done"
    opts = resolver.call_args.args[1]
    assert opts.container_branch == "release/1" and opts.base == "release/1"
    assert resolver.call_args.kwargs == {"autofetch": ctx.cfg.new.autofetch}
    assert push.call_args.kwargs["source_ref"] == SHA
    assert push.call_args.kwargs["namespace"] == "update/orch"
    assert seams["deliver"].call_args.args[3].imported_sha == SHA
    seams["merge"].assert_not_called()


@pytest.mark.parametrize("source", [{"ref": "refs/heads/missing"}, {"base": "missing"}])
def test_update_unknown_source_refused_without_message(tmp_path, seams, mocker, source):
    from jailbee.forking import ForkError

    mocker.patch(
        "jailbee.forking.prepare_fork",
        side_effect=ForkError("Cannot resolve source ref 'missing' to a commit"),
    )
    mocker.patch(
        "jailbee.lifecycle.resolve_clone_ref", side_effect=ValueError("branch 'missing' not found")
    )
    out = actions.dispatch(_ctx(tmp_path), _manifest(**source))
    assert out.state == "refused" and "missing" in out.detail
    seams["deliver"].assert_not_called()


@pytest.mark.parametrize(
    "source", [{"ref": "refs/heads/topic"}, {"base": "main"}, {"ref": "A" * 40}]
)
def test_update_source_selectors_validate(source):
    manifest = _manifest(**source)
    for key, value in source.items():
        assert getattr(manifest, key) == value


@pytest.mark.parametrize(
    "source",
    [
        {"ref": "main"},
        {"ref": "refs/heads/../bad"},
        {"base": "-bad"},
        {"ref": "refs/heads/main", "base": "main"},
    ],
)
def test_update_invalid_selectors_refused(source):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _manifest(**source)


@pytest.mark.parametrize(
    "ref, fetch_name",
    [("refs/heads/topic", "fetch_ref_from_container"), ("A" * 40, "fetch_sha_from_container")],
)
def test_update_transports_real_spawn_source_resolution(tmp_path, seams, mocker, ref, fetch_name):
    ctx = _ctx(tmp_path)
    bounded = ctx.incus.with_exec_timeout.return_value
    bounded.exec.return_value = SHA + "\n"
    mocker.patch("jailbee.sync.assert_container_publishable", return_value=ctx.sender.full)
    mocker.patch("jailbee.git.check_ref_format", return_value=True)
    mocker.patch("jailbee.sync._resolve_branch", return_value="main")
    fetch = mocker.patch(f"jailbee.sync.{fetch_name}")
    host_subs = mocker.patch("jailbee.submodules.transport_submodules_to_host_at_commit")
    mocker.patch("jailbee.submodules.transport_submodules_to_container")
    push = mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=replace(_result().push, container_ref="refs/jailbee/update/orch/u1"),
    )
    assert actions.dispatch(ctx, _manifest(ref=ref)).state == "done"
    bounded.exec.assert_called_once_with(
        ctx.sender.full,
        ["git", "-C", "/r", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        uid=ctx.cfg.container_user.uid,
    )
    assert fetch.call_args.args[-1] == SHA
    assert fetch.call_args.kwargs["timeout"] == actions.CHILD_TRANSPORT_TIMEOUT_S
    assert host_subs.call_args.kwargs["commit"] == SHA
    assert host_subs.call_args.kwargs["timeout"] == actions.CHILD_TRANSPORT_TIMEOUT_S
    assert push.call_args.kwargs["source_ref"] == SHA


def test_update_historical_submodules_use_path_independent_pins(tmp_path, seams, mocker):
    from jailbee.forking import ForkSource
    from jailbee.submodules import SubmoduleError

    mocker.patch(
        "jailbee.forking.prepare_fork", return_value=ForkSource("myrepo-orch", "main", SHA, None)
    )

    def transport(*args, **kwargs):
        if not kwargs.get("pin_commits"):
            raise SubmoduleError("historical path 'old c++ % path' is not a valid refname")

    mocker.patch("jailbee.submodules.transport_submodules_to_container", side_effect=transport)
    mocker.patch("jailbee.sync.push_to_container", return_value=_result().push)
    assert actions.dispatch(_ctx(tmp_path), _manifest(ref="refs/heads/history")).state == "done"


def test_update_base_uses_real_upstream_resolution(tmp_path, seams, mocker):
    ctx = _ctx(tmp_path)
    mocker.patch("jailbee.lifecycle.branch_exists_in_source", return_value=True)
    mocker.patch("jailbee.lifecycle.branch_exists_locally", return_value=False)
    fetch = mocker.patch("jailbee.lifecycle.fetch_remote_ref")
    mocker.patch("jailbee.lifecycle.rev_parse_remote", return_value=SHA)
    mocker.patch("jailbee.submodules.transport_submodules_to_container")
    mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=replace(_result().push, container_ref="refs/jailbee/update/orch/u1"),
    )
    assert actions.dispatch(ctx, _manifest(base="release/1")).state == "done"
    fetch.assert_called_once_with(ctx.cfg.repo_root, ctx.cfg.upstream_remote, "release/1")
    msg = seams["deliver"].call_args.args[3]
    assert (msg.imported_ref, msg.imported_sha) == ("refs/jailbee/update/orch/u1", SHA)


@pytest.mark.parametrize("failure", ["submodules", "push"])
def test_update_explicit_transport_failure_has_no_message(tmp_path, seams, mocker, failure):
    from jailbee.forking import ForkSource
    from jailbee.submodules import SubmoduleError

    mocker.patch(
        "jailbee.forking.prepare_fork", return_value=ForkSource("myrepo-orch", "main", SHA, None)
    )
    subs = mocker.patch("jailbee.submodules.transport_submodules_to_container")
    push = mocker.patch("jailbee.sync.push_to_container")
    if failure == "submodules":
        subs.side_effect = SubmoduleError("missing gitlink")
    else:
        push.side_effect = SyncError("cannot push")
    out = actions.dispatch(_ctx(tmp_path), _manifest(ref="refs/heads/topic"))
    assert out.state == "refused"
    assert "missing gitlink" in out.detail or "cannot push" in out.detail
    seams["deliver"].assert_not_called()


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


@pytest.fixture
def merge_seams(mocker):
    from jailbee.sync import MergeProbe

    return {
        "preflight": mocker.patch(
            "jailbee.sync._run_container_preflights", return_value="child-branch"
        ),
        "probe": mocker.patch(
            "jailbee.sync.probe_merge_in_container", return_value=MergeProbe("fast_forward", ())
        ),
        "merge": mocker.patch("jailbee.sync._merge_ref_in_container", return_value="b" * 40),
        "abort": mocker.patch("jailbee.sync.abort_merge_in_container"),
        "head": mocker.patch("jailbee.sync._container_head_oid", return_value="c" * 40),
    }


def test_update_manifest_merge_defaults_false():
    m = _manifest()
    assert m.merge is False
    assert "merge" not in m.model_dump(exclude_defaults=True)


def test_update_merge_fast_forward_merges_and_messages(tmp_path, seams, merge_seams):
    ctx = _ctx(tmp_path)
    seams["repo"].side_effect = lambda cfg, incus, name: "/child" if name == CHILD else "/parent"
    out = actions.dispatch(ctx, _manifest(merge=True))
    assert out.state == "done"
    call = merge_seams["merge"].call_args
    assert call.args[2] == "/child"
    assert call.kwargs["ff_only"] is True
    assert call.kwargs["ref"] == "refs/jailbee/from/fix-1/fix-1"
    assert call.kwargs["uid"] == ctx.cfg.container_user.uid
    msg = seams["deliver"].call_args.args[3]
    assert msg.merged_head == "b" * 40 and "fast-forward" in msg.body


def test_update_merge_creates_merge_commit(tmp_path, seams, merge_seams):
    from jailbee.sync import MergeProbe

    merge_seams["probe"].return_value = MergeProbe("merge", ())
    assert actions.dispatch(_ctx(tmp_path), _manifest(merge=True)).state == "done"
    kwargs = merge_seams["merge"].call_args.kwargs
    assert not kwargs["ff_only"]
    assert kwargs["message"] == "Merge main from orchestrator orch"


@pytest.mark.parametrize(
    "paths, expected",
    [(("a.py", "b.py"), "a.py, b.py"), (tuple(str(i) for i in range(25)), "… and 5 more")],
)
def test_update_merge_conflicts_refused(tmp_path, seams, merge_seams, paths, expected):
    from jailbee.sync import MergeProbe

    merge_seams["probe"].return_value = MergeProbe("merge", paths)
    out = actions.dispatch(_ctx(tmp_path), _manifest(merge=True))
    assert out.state == "refused" and expected in out.detail
    assert "refs/jailbee/from/fix-1/fix-1" in out.detail
    merge_seams["merge"].assert_not_called()
    seams["deliver"].assert_not_called()


def test_update_merge_conflict_without_paths_refuses(tmp_path, seams, merge_seams):
    from jailbee.sync import MergeProbe

    merge_seams["probe"].return_value = MergeProbe("merge", (), conflicted=True)
    out = actions.dispatch(_ctx(tmp_path), _manifest(merge=True))
    assert out.state == "refused" and "conflict" in out.detail
    merge_seams["merge"].assert_not_called()
    seams["deliver"].assert_not_called()


def test_update_merge_abort_failure_is_not_reported_as_aborted(
    tmp_path, seams, merge_seams, mocker
):
    from jailbee.sync import MergeConflictError

    merge_seams["merge"].side_effect = MergeConflictError("conflict", report=mocker.Mock())
    merge_seams["abort"].return_value = False
    out = actions.dispatch(_ctx(tmp_path), _manifest(merge=True))
    assert out.state == "refused" and "manual" in out.detail
    assert "merge aborted" not in out.detail
    seams["deliver"].assert_not_called()


def test_update_merge_dirty_child_refused_before_transport(tmp_path, seams, merge_seams):
    merge_seams["preflight"].side_effect = SyncError("Container working tree is dirty")
    assert actions.dispatch(_ctx(tmp_path), _manifest(merge=True)).state == "refused"
    seams["merge"].assert_not_called()


def test_update_merge_mount_mode_refused(tmp_path, seams, merge_seams):
    ctx = _ctx(tmp_path)
    ctx.incus.with_exec_timeout.return_value.config_get.return_value = "mount"
    assert actions.dispatch(ctx, _manifest(merge=True)).state == "refused"
    seams["merge"].assert_not_called()
    merge_seams["preflight"].assert_not_called()


def test_update_merge_already_contained_no_merge(tmp_path, seams, merge_seams):
    from jailbee.sync import MergeProbe

    merge_seams["probe"].return_value = MergeProbe("contained", ())
    out = actions.dispatch(_ctx(tmp_path), _manifest(merge=True))
    assert out.state == "done" and out.detail == f"fix-1 already contains {SHA[:12]}"
    merge_seams["merge"].assert_not_called()
    msg = seams["deliver"].call_args.args[3]
    assert "already contains" in msg.body and msg.merged_head == "c" * 40


def test_update_merge_race_conflict_aborts_and_refuses(tmp_path, seams, merge_seams, mocker):
    from jailbee.sync import MergeConflictError

    merge_seams["merge"].side_effect = MergeConflictError("conflict", report=mocker.Mock())
    assert actions.dispatch(_ctx(tmp_path), _manifest(merge=True)).state == "refused"
    merge_seams["abort"].assert_called_once()
    seams["deliver"].assert_not_called()


def test_update_merge_existing_operation_refused_without_abort(
    tmp_path, seams, merge_seams, mocker
):
    merge_seams["preflight"].side_effect = ["child-branch", SyncError("merge in progress")]
    out = actions.dispatch(_ctx(tmp_path), _manifest(merge=True))
    assert out.state == "refused" and "merge in progress" in out.detail
    merge_seams["merge"].assert_not_called()
    merge_seams["abort"].assert_not_called()
    seams["deliver"].assert_not_called()


def test_update_merge_git_existing_merge_refusal_does_not_abort(
    tmp_path, seams, merge_seams, mocker
):
    from jailbee import sync

    mocker.stop(merge_seams["merge"])
    mocker.patch("jailbee.sync._container_ref_oid", return_value="a" * 40)
    mocker.patch("jailbee.sync.submodules.vanishing_submodules", return_value=[])
    mocker.patch("jailbee.sync._container_has_merge_in_progress", return_value=True)
    mocker.patch(
        "jailbee.sync._exec_container_git_write",
        side_effect=sync.IncusError(
            "fatal: You have not concluded your merge (MERGE_HEAD exists)."
        ),
    )
    out = actions.dispatch(_ctx(tmp_path), _manifest(merge=True))
    assert out.state == "refused" and "MERGE_HEAD" in out.detail
    merge_seams["abort"].assert_not_called()
    seams["deliver"].assert_not_called()


@pytest.mark.parametrize("head", ["b" * 40, None])
def test_update_merge_post_merge_warning_delivers(tmp_path, seams, merge_seams, mocker, head):
    from jailbee import sync

    mocker.stop(merge_seams["merge"])
    mocker.patch("jailbee.sync._container_ref_oid", return_value="a" * 40)
    merge_seams["head"].side_effect = [head] if head else [SyncError("cannot read HEAD")]
    mocker.patch("jailbee.sync.submodules.vanishing_submodules", return_value=[])
    mocker.patch("jailbee.sync._exec_container_git_write")
    mocker.patch(
        "jailbee.sync.submodules.update_submodules_in_container",
        side_effect=sync.IncusError("submodule failed"),
    )
    out = actions.dispatch(_ctx(tmp_path), _manifest(merge=True))
    assert out.state == "done" and "warning" in out.detail
    msg = seams["deliver"].call_args.args[3]
    assert msg.merged_head == head
    assert "submodule failed" in msg.body and "re-read" in msg.body
    assert "do not merge" in msg.body


def test_update_merge_with_explicit_ref(tmp_path, seams, merge_seams, mocker):
    mocker.patch("jailbee.orchestration.importing._update_source", return_value=SHA)
    mocker.patch("jailbee.submodules.transport_submodules_to_container")
    mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=replace(_result().push, container_ref="refs/jailbee/update/orch/u1"),
    )
    assert (
        actions.dispatch(_ctx(tmp_path), _manifest(ref="refs/heads/release/1", merge=True)).state
        == "done"
    )
    assert merge_seams["probe"].call_args.kwargs["ref"] == "refs/jailbee/update/orch/u1"
    assert merge_seams["merge"].call_args.kwargs["label"] == "refs/heads/release/1"


def test_update_without_merge_never_probes(tmp_path, seams, merge_seams):
    assert actions.dispatch(_ctx(tmp_path), _manifest()).state == "done"
    merge_seams["probe"].assert_not_called()
    merge_seams["preflight"].assert_not_called()


def test_inbox_merged_head_validated():
    from pydantic import ValidationError

    from jailbee.orchestration.protocol import InboxMessage

    fields = dict(version=1, id="h-12345678", type="message", body="x", merged_head="bad")
    with pytest.raises(ValidationError):
        InboxMessage.model_validate({**fields, "from": "orch"})


@pytest.mark.parametrize("source", [{"ref": "refs/heads/release/1"}, {"base": "main"}])
def test_update_merge_explicit_dirty_before_source_or_transport(
    tmp_path, seams, merge_seams, mocker, source
):
    merge_seams["preflight"].side_effect = SyncError("dirty")
    source_resolve = mocker.patch("jailbee.orchestration.importing._update_source")
    prepare = mocker.patch("jailbee.forking.prepare_fork")
    transport = mocker.patch("jailbee.submodules.transport_submodules_to_container")
    push = mocker.patch("jailbee.sync.push_to_container")
    out = actions.dispatch(_ctx(tmp_path), _manifest(merge=True, **source))
    assert out == actions.refused("dirty")
    for call in (source_resolve, prepare, transport, push):
        call.assert_not_called()


def test_update_merge_child_repo_preflight_probe_and_abort(tmp_path, seams, merge_seams, mocker):
    from jailbee.sync import MergeConflictError

    ctx = _ctx(tmp_path)
    bounded = ctx.incus.with_exec_timeout.return_value
    seams["repo"].side_effect = lambda cfg, incus, name: "/child" if name == CHILD else "/parent"
    merge_seams["merge"].side_effect = MergeConflictError("conflict", report=mocker.Mock())
    out = actions.dispatch(ctx, _manifest(merge=True))
    uid = ctx.cfg.container_user.uid
    assert merge_seams["preflight"].call_count == 2
    merge_seams["preflight"].assert_called_with(bounded, CHILD, "/child", uid=uid)
    merge_seams["probe"].assert_called_once_with(
        bounded, CHILD, "/child", ref="refs/jailbee/from/fix-1/fix-1", uid=uid
    )
    assert merge_seams["merge"].call_args.kwargs["resolve_gitlinks"] is False
    merge_seams["abort"].assert_called_once_with(bounded, CHILD, "/child", uid=uid)
    assert out == actions.refused("child changed during update; merge aborted, nothing merged")
    seams["deliver"].assert_not_called()


def test_inbox_json_omits_only_unset_merged_head():
    import json

    from jailbee.orchestration.protocol import InboxMessage

    msg = InboxMessage.model_validate(
        {"version": 1, "id": "h-12345678", "type": "message", "from": "orch", "body": "hi"}
    )
    data = json.loads(msg.model_dump_json(by_alias=True))
    assert "merged_head" not in data
    assert data["imported_ref"] is None and data["imported_sha"] is None
    assert data["on_done"] is None
    with_head = msg.model_copy(update={"merged_head": "a" * 40})
    assert json.loads(with_head.model_dump_json(by_alias=True))["merged_head"] == "a" * 40
