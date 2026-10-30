import json
from datetime import UTC, datetime, timedelta

import pytest

from jailbee.global_config import GlobalConfig
from jailbee.orchestration import actions, labels
from jailbee.orchestration.actions import ActionContext, Node
from jailbee.orchestration.grant import ChildRights, Grant, Grantable
from jailbee.orchestration.protocol import RightsManifest
from tests.conftest import make_cfg


@pytest.fixture
def rig(tmp_path, mocker):
    cfg = make_cfg(tmp_path)
    grant = Grant(grantable=Grantable(loose=True, mounts=("aws",)))
    store = {
        ("orch", labels.GRANT): grant.to_label(),
        ("c", labels.RIGHTS): ChildRights().to_label(),
    }
    incus = mocker.Mock()
    incus.config_get.side_effect = lambda name, key: store.get((name, key))
    incus.config_set.side_effect = lambda name, key, value: store.__setitem__((name, key), value)
    incus.config_unset.side_effect = lambda name, key: store.pop((name, key), None)
    incus.config_unset_checked.side_effect = incus.config_unset.side_effect
    incus.config_device_get.return_value = None
    sender = Node("orch", "orch", True, {labels.GRANT: grant.to_label()})
    child = Node("c", "c", True, {labels.SPAWNED_BY: "orch"})
    cfg = cfg.model_copy(update={"optional_mounts": {"aws": object()}})
    ctx = ActionContext(
        cfg,
        GlobalConfig(),
        incus,
        tmp_path,
        sender,
        {"orch": sender, "c": child},
        datetime(2026, 10, 30, tzinfo=UTC),
    )
    switch = mocker.patch("jailbee.net_switch.switch_mode")
    attach = mocker.patch("jailbee.mounts.attach")
    remove = mocker.patch("jailbee.mounts.remove_optional_mount")
    add_egress = mocker.patch("jailbee.egress_scope.add_container_entry")
    rm_egress = mocker.patch("jailbee.egress_scope.remove_container_entry")
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.egress_scope.container_extras", return_value=[])
    mocker.patch("jailbee.egress_scope.resolve_entries", return_value=[])
    return ctx, store, switch, attach, remove, add_egress, rm_egress


def manifest(**blocks):
    return RightsManifest.model_validate(
        {"version": 1, "type": "rights", "id": "r", "child": "c", **blocks}
    )


def test_grant_loose_timed(rig):
    ctx, store, switch, *_ = rig
    result = actions.dispatch(ctx, manifest(grant={"loose": True, "for": "5m"}))
    assert result.state == "done"
    until = ctx.now + timedelta(minutes=5)
    switch.assert_called_once_with(ctx.cfg, ctx.incus, "c", "loose", until=until)
    assert json.loads(store[("c", labels.TIMED)]) == {"loose": until.isoformat()}
    assert not ChildRights.from_label(store[("c", labels.RIGHTS)]).loose


def test_grant_never_mount(rig):
    ctx, store, _, attach, *_ = rig
    result = actions.dispatch(ctx, manifest(grant={"mounts": ["aws"], "for": "never"}))
    assert result.state == "done"
    attach.assert_called_once_with(ctx.cfg, ctx.incus, "c", "aws", until=None)
    assert ChildRights.from_label(store[("c", labels.RIGHTS)]).mounts == ("aws",)


@pytest.mark.parametrize("revoke", [False, True])
def test_rights_operation_gate_and_revoke_exception(rig, revoke):
    ctx, store, switch, *_ = rig
    grant = Grant(operations=("spawn", "message"))
    store[("orch", labels.GRANT)] = grant.to_label()
    ctx.sender = Node("orch", "orch", True, {labels.GRANT: grant.to_label()})
    blocks = {"revoke": {"loose": True}} if revoke else {"grant": {"loose": True, "for": "5m"}}
    result = actions.dispatch(ctx, manifest(**blocks))
    assert result.state == ("done" if revoke else "refused")
    if not revoke:
        assert result.detail == "operation `rights` is not in this container's grant"
    switch.assert_not_called()


def test_ungrantable_refused_before_any_change(rig):
    ctx, store, switch, *_ = rig
    store[("orch", labels.GRANT)] = Grant().to_label()
    result = actions.dispatch(ctx, manifest(grant={"loose": True, "for": "5m"}))
    assert result.state == "refused" and result.detail == "loose network mode is not grantable"
    switch.assert_not_called()


def test_loose_implies_egress_runtime(rig):
    ctx, store, _, _, _, add, _ = rig
    result = actions.dispatch(ctx, manifest(grant={"egress": ["pypi.org"], "for": "5m"}))
    assert result.state == "done"
    add.assert_called_once_with(
        ctx.cfg, ctx.incus, "c", "pypi.org", until=ctx.now + timedelta(minutes=5)
    )
    assert "egress:pypi.org" in json.loads(store[("c", labels.TIMED)])


def test_timed_grant_does_not_narrow_spawn_loose(rig):
    ctx, store, switch, *_ = rig
    store[("c", labels.RIGHTS)] = ChildRights(loose=True).to_label()
    result = actions.dispatch(ctx, manifest(grant={"loose": True, "for": "5m"}))
    assert result.state == "done" and "already held" in result.detail
    switch.assert_not_called()
    assert ("c", labels.TIMED) not in store


def test_revoke_human_mount_leaves_it_alone(rig):
    ctx, _, _, _, remove, *_ = rig
    result = actions.dispatch(ctx, manifest(revoke={"mounts": ["aws"]}))
    assert (
        result.state == "done" and "aws was not given by orchestration; left as is" in result.detail
    )
    remove.assert_not_called()


def test_revoke_owned_clears_live_label_and_timed(rig, mocker):
    ctx, store, switch, _, _remove, *_ = rig
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="loose")
    ctx.incus.config_device_get.return_value = "/host/aws"
    store[("c", labels.RIGHTS)] = ChildRights(loose=True, mounts=("aws",)).to_label()
    store[("c", labels.TIMED)] = json.dumps({"loose": (ctx.now + timedelta(minutes=5)).isoformat()})
    result = actions.dispatch(ctx, manifest(revoke={"loose": True, "mounts": ["aws"]}))
    assert result.state == "done"
    switch.assert_called_once_with(ctx.cfg, ctx.incus, "c", "strict", until=None)
    assert ChildRights.from_label(store[("c", labels.RIGHTS)]) == ChildRights()
    assert ("c", labels.TIMED) not in store


@pytest.mark.parametrize(
    "blocks",
    [{"grant": {"loose": True, "for": "5m"}}, {"child": "sibling", "revoke": {"loose": True}}],
)
def test_child_cannot_grant_or_name_sibling(rig, blocks):
    ctx, *_ = rig
    ctx.sender = ctx.nodes["c"]
    result = actions.dispatch(ctx, manifest(**blocks))
    assert (
        result.state == "refused" and result.detail == "children may only revoke their own rights"
    )


def test_child_revoke_notifies_parent(rig, mocker):
    ctx, store, switch, *_ = rig
    ctx.sender = ctx.nodes["c"]
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="loose")
    store[("c", labels.RIGHTS)] = ChildRights(loose=True).to_label()
    deliver = mocker.patch("jailbee.orchestration.messages.deliver", return_value="delivered")
    result = actions.dispatch(ctx, manifest(child=None, revoke={"loose": True}))
    assert result.state == "done"
    switch.assert_called_once()
    msg = deliver.call_args.args[3]
    assert msg.kind == "note" and msg.body == "c gave up: loose"
    assert deliver.call_args.args[2] == "orch"


def test_second_grant_failure_undoes_first(rig):
    ctx, store, switch, attach, *_ = rig
    attach.side_effect = ValueError("mount failed")
    result = actions.dispatch(ctx, manifest(grant={"loose": True, "mounts": ["aws"], "for": "5m"}))
    assert result.state == "failed" and "mount failed" in result.detail
    assert switch.call_args_list[0].args[3] == "loose"
    assert switch.call_args_list[-1].args[3] == "strict"
    assert ("c", labels.TIMED) not in store
    assert ChildRights.from_label(store[("c", labels.RIGHTS)]) == ChildRights()


def test_later_expiry_wins_and_never_promotes(rig):
    ctx, store, switch, *_ = rig
    future = ctx.now + timedelta(hours=2)
    store[("c", "user.jailbee.loose_until")] = future.isoformat()
    store[("c", labels.TIMED)] = json.dumps({"loose": future.isoformat()})
    result = actions.dispatch(ctx, manifest(grant={"loose": True, "for": "5m"}))
    assert result.state == "done"
    assert json.loads(store[("c", labels.TIMED)])["loose"] == future.isoformat()
    assert not switch.called or switch.call_args.kwargs["until"] == future
    result = actions.dispatch(ctx, manifest(grant={"loose": True, "for": "never"}))
    assert result.state == "done"
    switch.assert_called_with(ctx.cfg, ctx.incus, "c", "loose", until=None)
    assert ChildRights.from_label(store[("c", labels.RIGHTS)]).loose
    assert ("c", labels.TIMED) not in store


@pytest.mark.parametrize("right", ["loose", "mount", "egress"])
def test_timed_grant_does_not_narrow_human_permanent_right(rig, mocker, right):
    ctx, store, switch, attach, _, add, _ = rig
    if right == "loose":
        mocker.patch("jailbee.lifecycle.current_network_mode", return_value="loose")
        block = {"loose": True}
    elif right == "mount":
        ctx.incus.config_device_get.return_value = "/host/aws"
        block = {"mounts": ["aws"]}
    else:
        mocker.patch("jailbee.egress_scope.container_extras", return_value=["pypi.org"])
        block = {"egress": ["pypi.org"]}
    result = actions.dispatch(ctx, manifest(grant={**block, "for": "5m"}))
    assert result.state == "done" and "already held" in result.detail
    switch.assert_not_called()
    attach.assert_not_called()
    add.assert_not_called()
    assert ChildRights.from_label(store[("c", labels.RIGHTS)]) == ChildRights()
    assert ("c", labels.TIMED) not in store


def test_explicit_null_grant_from_child_is_refused(rig):
    ctx, *_ = rig
    ctx.sender = ctx.nodes["c"]
    result = actions.dispatch(ctx, manifest(grant=None, revoke={"loose": True}))
    assert (
        result.state == "refused" and result.detail == "children may only revoke their own rights"
    )


def test_refusal_preflights_unknown_mount_before_revoke(rig):
    ctx, store, switch, *_ = rig
    store[("c", labels.RIGHTS)] = ChildRights(loose=True).to_label()
    result = actions.dispatch(
        ctx, manifest(revoke={"loose": True}, grant={"mounts": ["unknown"], "for": "never"})
    )
    assert result.state == "refused" and "not grantable" in result.detail
    switch.assert_not_called()


def test_revoke_only_ignores_removed_live_grant(rig, mocker):
    ctx, store, switch, *_ = rig
    store.pop(("orch", labels.GRANT))
    store[("c", labels.RIGHTS)] = ChildRights(loose=True).to_label()
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="loose")
    assert actions.dispatch(ctx, manifest(revoke={"loose": True})) is None
    switch.assert_not_called()


def test_expired_provenance_does_not_own_human_readded_mount(rig):
    ctx, store, _, _, remove, *_ = rig
    store[("c", labels.TIMED)] = json.dumps(
        {"mount:aws": (ctx.now - timedelta(minutes=1)).isoformat()}
    )
    ctx.incus.config_device_get.return_value = "/human/aws"
    result = actions.dispatch(ctx, manifest(revoke={"mounts": ["aws"]}))
    assert result.state == "done" and "was not given by orchestration" in result.detail
    remove.assert_not_called()
