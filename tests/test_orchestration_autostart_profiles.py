"""Spawn and restart preserve and validate named autostart selections."""

from dataclasses import replace

from jailbee.orchestration import actions
from jailbee.orchestration.protocol import SpawnManifest
from tests.test_autostart_profiles import profile_cfg
from tests.test_orchestration_spawn import _ctx, _m


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
