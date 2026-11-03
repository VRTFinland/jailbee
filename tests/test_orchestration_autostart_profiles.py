"""Spawn and restart preserve and validate named autostart selections."""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from jailbee.orchestration import actions
from jailbee.orchestration.protocol import SpawnManifest
from tests.test_autostart_profiles import profile_cfg
from tests.test_orchestration_spawn import _ctx, _m


def test_raw_legacy_queue_without_services_loads(tmp_path):
    import json

    from jailbee.orchestration.state import load_queue

    raw = {
        "version": 1,
        "queues": {
            "parent": [
                {
                    "manifest_id": "m",
                    "child_full": "c",
                    "kind": "spawn",
                    "queued_at": "now",
                    "prompt": "go",
                    "rights": {},
                    "commit": None,
                    "branch": None,
                    "base": None,
                }
            ]
        },
    }
    (tmp_path / "queue.json").write_text(json.dumps(raw))
    assert load_queue(tmp_path)["parent"][0].services is None


def test_spawn_manifest_accepts_autostart_profile():
    raw = _m().model_dump(exclude_unset=True) | {"autostart_profile": "light"}
    assert SpawnManifest.model_validate(raw).autostart_profile == "light"


def test_spawn_unknown_profile_refused_with_detail(make_cfg, tmp_path):
    ctx = _ctx(tmp_path)
    ctx = replace(
        ctx,
        cfg=ctx.cfg.model_copy(
            update={"autostart": profile_cfg(make_cfg, tmp_path / "p").autostart}
        ),
    )
    manifest = _m().model_copy(update={"autostart_profile": "X"})
    result = actions.plan_spawn(ctx, manifest, queued_names=())
    assert result.state == "refused"
    assert result.detail == "autostart profile `X` is not defined"


def test_spawn_passes_profile_to_new_options(make_cfg, tmp_path, mocker):
    ctx = _ctx(tmp_path)
    ctx = replace(
        ctx,
        cfg=ctx.cfg.model_copy(
            update={"autostart": profile_cfg(make_cfg, tmp_path / "p").autostart}
        ),
    )
    mocker.patch("jailbee.orchestration.actions.live_create_jobs", return_value=set())
    plan = actions.plan_spawn(
        ctx, _m().model_copy(update={"autostart_profile": "light"}), queued_names=()
    )
    options = actions.child_options(ctx.cfg, plan, orchestrator_full="proj-orch", fork=None)
    assert options.autostart_profile == "light"


def test_restart_stores_profile_before_boot(make_cfg, tmp_path, mocker):
    from jailbee.autostart_profiles import PROFILE_KEY
    from tests.test_orchestration_lifecycle_ops import Rig, _restart

    rig = Rig(tmp_path)
    rig.cfg = rig.cfg.model_copy(
        update={"autostart": profile_cfg(make_cfg, tmp_path / "p").autostart}
    )
    mocker.patch("jailbee.orchestration.actions.live_create_jobs", return_value=set())

    from jailbee.create_flow import CreateStarted
    from jailbee.orchestration.state import load_reservations

    def launch(*args, **kwargs):
        rig.incus.config_set.assert_any_call("proj-c1", PROFILE_KEY, "full")
        assert kwargs["initial_prompt"].endswith("go on")
        return CreateStarted("proj-c1", 12345, tmp_path / "boot.log")

    launch_mock = mocker.patch(
        "jailbee.create_flow.launch_boot_background", create=True, side_effect=launch
    )
    boot = mocker.patch("jailbee.lifecycle.boot_container")
    result = actions.handle_restart(
        rig.ctx(), _restart().model_copy(update={"autostart_profile": "full"})
    )
    assert result.state == "started", result.detail
    launch_mock.assert_called_once()
    boot.assert_not_called()
    assert load_reservations(tmp_path)["proj-orch"][0].pid == 12345
    assert actions.running_children(rig.ctx()) == 1


def test_profile_restart_reserves_queue_slot_before_setup_can_fail(make_cfg, tmp_path, mocker):
    from jailbee.create_flow import CreateStarted
    from jailbee.orchestration.state import QueuedRequest, load_queue, save_queue
    from tests.test_orchestration_lifecycle_ops import Rig

    rig = Rig(tmp_path)
    rig.cfg = rig.cfg.model_copy(
        update={"autostart": profile_cfg(make_cfg, tmp_path / "p").autostart}
    )
    ctx = rig.ctx()
    second = replace(rig.child, full="proj-c2", short="c2")
    ctx.nodes[second.full] = second
    mocker.patch("jailbee.orchestration.actions.live_create_jobs", return_value=set())
    mocker.patch("jailbee.lifecycle.boot_container")
    mocker.patch(
        "jailbee.autostart_profiles.run_profile_start",
        create=True,
        side_effect=RuntimeError("setup failed after boot"),
    )
    mocker.patch(
        "jailbee.create_flow.launch_boot_background",
        create=True,
        return_value=CreateStarted("proj-c1", 12345, tmp_path / "boot.log"),
    )
    save_queue(
        tmp_path,
        {
            "proj-orch": [
                QueuedRequest(
                    manifest_id=f"r{i}",
                    child_full=f"proj-c{i}",
                    kind="restart",
                    queued_at=ctx.now.isoformat(),
                    prompt="go",
                    rights={},
                    commit=None,
                    branch=None,
                    base=None,
                )
                for i in (1, 2)
            ]
        },
    )
    results = actions.drain_queue(ctx)
    assert [(q.child_full, out.state) for q, out in results] == [("proj-c1", "started")]
    assert [q.child_full for q in load_queue(tmp_path)["proj-orch"]] == ["proj-c2"]


@pytest.mark.parametrize("kind", ["spawn", "restart"])
@pytest.mark.parametrize(
    "services,reason",
    [
        (["Bad"], "Value error, invalid service name: 'Bad'"),
        (["a\n"], "Value error, invalid service name: 'a\\n'"),
        (["svc"] * 33, "Value error, at most 32 services are allowed"),
    ],
)
def test_manifest_rejects_invalid_service_selection(kind, services, reason):
    from tests.test_orchestration_lifecycle_ops import _restart

    raw = (_m() if kind == "spawn" else _restart()).model_dump(exclude_unset=True) | {
        "services": services
    }
    with pytest.raises(ValidationError) as exc:
        (SpawnManifest if kind == "spawn" else type(_restart())).model_validate(raw)
    assert any(
        error["loc"] == ("services",) and error["msg"] == reason for error in exc.value.errors()
    )


@pytest.mark.parametrize("services", [None, [], ["db", "web"]])
def test_spawn_services_reach_options_and_keep_manifest_id(tmp_path, mocker, services):
    ctx = _ctx(tmp_path)
    mocker.patch("jailbee.orchestration.actions.live_create_jobs", return_value=set())
    manifest = SpawnManifest.model_validate(
        _m().model_dump(exclude_unset=True) | {"services": services}
    )
    plan = actions.plan_spawn(ctx, manifest, queued_names=())
    assert plan.manifest_id == manifest.id
    assert (
        actions.child_options(
            ctx.cfg, plan, orchestrator_full="proj-orch", fork=None
        ).autostart_services
        == services
    )


@pytest.mark.parametrize("services,encoded", [(None, None), ([], ","), (["db", "web"], "db,web")])
def test_restart_services_label_before_launch(tmp_path, mocker, services, encoded):
    from jailbee.autostart_profiles import SERVICES_KEY
    from jailbee.create_flow import CreateStarted
    from tests.test_orchestration_lifecycle_ops import Rig, _restart

    rig = Rig(tmp_path)
    mocker.patch("jailbee.orchestration.actions.live_create_jobs", return_value=set())

    def launch(*args, **kwargs):
        calls = [c for c in rig.incus.config_set.call_args_list if c.args[1] == SERVICES_KEY]
        assert [c.args[2] for c in calls] == ([] if encoded is None else [encoded])
        return CreateStarted("proj-c1", 12345, tmp_path / "boot.log")

    mocker.patch("jailbee.create_flow.launch_boot_background", side_effect=launch)
    manifest = type(_restart()).model_validate(
        _restart().model_dump(exclude_unset=True) | {"services": services}
    )
    assert actions.handle_restart(rig.ctx(), manifest).state == "started"


def test_agent_only_refuses_explicit_empty_services(tmp_path):
    from tests.test_orchestration_lifecycle_ops import Rig, _restart

    manifest = type(_restart()).model_validate(
        _restart().model_dump() | {"services": [], "agent_only": True}
    )
    result = actions.handle_restart(Rig(tmp_path).ctx(), manifest)
    assert result.detail == "services cannot be combined with agent_only (no autostart runs)"


@pytest.mark.parametrize("services", [None, [], ["db", "web"]])
@pytest.mark.parametrize("kind", ["spawn", "restart"])
def test_queued_services_persist_and_reach_launch(tmp_path, mocker, services, kind):
    from jailbee.autostart_profiles import SERVICES_KEY
    from jailbee.create_flow import CreateStarted
    from jailbee.orchestration import labels
    from jailbee.orchestration.actions import Node
    from jailbee.orchestration.state import load_queue
    from tests.test_orchestration_lifecycle_ops import _restart
    from tests.test_orchestration_queue import Rig
    from tests.test_orchestration_queue import _m as queue_manifest

    rig = Rig(tmp_path, limit=1, running=1)
    mocker.patch("jailbee.orchestration.actions.live_create_jobs", return_value=set())
    if kind == "spawn":
        manifest = SpawnManifest.model_validate(
            queue_manifest("new", kind="new").model_dump(exclude_unset=True)
            | {"services": services}
        )
        assert actions.handle_spawn(rig.ctx(), manifest).state == "queued"
    else:
        rig.children.append(Node("proj-c2", "c2", False, {labels.SPAWNED_BY: "proj-orch"}))
        manifest = type(_restart()).model_validate(
            _restart("c2").model_dump(exclude_unset=True) | {"services": services}
        )
        assert actions.handle_restart(rig.ctx(), manifest).state == "queued"
    assert load_queue(tmp_path)["proj-orch"][0].services == services
    rig.children = [c for c in rig.children if not c.running]
    create = mocker.patch(
        "jailbee.orchestration.actions.start_create",
        return_value=CreateStarted("proj-new", 123, tmp_path / "log"),
    )
    boot = mocker.patch(
        "jailbee.create_flow.launch_boot_background",
        return_value=CreateStarted("proj-c2", 123, tmp_path / "log"),
    )
    assert [out.state for _, out in actions.drain_queue(rig.ctx())] == ["started"]
    if kind == "spawn":
        assert create.call_args.args[3].autostart_services == services
    else:
        boot.assert_called_once()
        calls = [
            c.args[2] for c in rig.incus.config_set.call_args_list if c.args[1] == SERVICES_KEY
        ]
        assert calls == ([] if services is None else [",".join(services) if services else ","])


@pytest.mark.parametrize("kind", ["spawn", "restart"])
def test_manifest_accepts_32_services_and_omission(kind):
    from tests.test_orchestration_lifecycle_ops import _restart

    original = _m() if kind == "spawn" else _restart()
    raw = original.model_dump(exclude_unset=True)
    assert type(original).model_validate(raw).services is None
    assert type(original).model_validate(raw | {"services": ["db"] * 32}).services == ["db"] * 32


@pytest.mark.parametrize("services", [None, [], ["db"]])
def test_inbox_services_and_legacy_message_roundtrip(services):
    from jailbee.orchestration.protocol import InboxMessage

    raw = {"version": 1, "id": "x", "type": "message", "from": "host", "body": "ok"}
    assert InboxMessage.model_validate(raw).services is None
    msg = InboxMessage.model_validate(raw | {"services": services})
    assert InboxMessage.model_validate_json(msg.model_dump_json(by_alias=True)).services == services
