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
    ctx = replace(ctx, cfg=ctx.cfg.model_copy(update={"autostart": profile_cfg(make_cfg, tmp_path / "p").autostart}))
    manifest = _m().model_copy(update={"autostart_profile": "X"})
    result = actions.plan_spawn(ctx, manifest, queued_names=())
    assert result.state == "refused"
    assert result.detail == "autostart profile `X` is not defined"


def test_spawn_passes_profile_to_new_options(make_cfg, tmp_path, mocker):
    ctx = _ctx(tmp_path)
    ctx = replace(ctx, cfg=ctx.cfg.model_copy(update={"autostart": profile_cfg(make_cfg, tmp_path / "p").autostart}))
    mocker.patch("jailbee.orchestration.actions.live_create_jobs", return_value=set())
    plan = actions.plan_spawn(ctx, _m().model_copy(update={"autostart_profile": "light"}), queued_names=())
    options = actions.child_options(ctx.cfg, plan, orchestrator_full="proj-orch", fork=None)
    assert options.autostart_profile == "light"


def test_restart_stores_profile_before_boot(make_cfg, tmp_path, mocker):
    from tests.test_orchestration_lifecycle_ops import Rig, _restart
    from jailbee.autostart_profiles import PROFILE_KEY
    rig = Rig(tmp_path)
    rig.cfg = rig.cfg.model_copy(update={"autostart": profile_cfg(make_cfg, tmp_path / "p").autostart})
    mocker.patch("jailbee.orchestration.actions.live_create_jobs", return_value=set())
    def boot(*args, **kwargs):
        rig.incus.config_set.assert_any_call("proj-c1", PROFILE_KEY, "full")
    mocker.patch("jailbee.lifecycle.boot_container", side_effect=boot)
    mocker.patch("jailbee.autostart_profiles.run_profile_start", create=True)
    result = actions.handle_restart(rig.ctx(), _restart().model_copy(update={"autostart_profile": "full"}))
    assert result.state == "done", result.detail
