"""Container profile state and attempted create stages."""

import pytest

from jailbee.autostart_plan import AutostartTrigger
from jailbee.config import Autostart


def profile_cfg(make_cfg, tmp_path):
    return make_cfg(tmp_path, autostart={
        "on_create": [{"stage": "deps", "steps": []}, {"stage": "images", "steps": []}],
        "on_start": [{"stage": "frontend", "steps": []}, {"stage": "backend", "steps": []}],
        "profiles": {"full": "all", "light": {"stages": ["deps", "frontend"]}},
        "default_profile": "light",
    })


def state_incus(mocker, state=None):
    state = dict(state or {})
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda c, k: state.get(k)
    incus.config_set.side_effect = lambda c, k, v: state.update({k: v})
    return incus, state


@pytest.mark.parametrize("stored,expected", [("full", "full"), (None, "light"), ("gone", "light")])
def test_resolve_profile_stored_absent_unknown(make_cfg, tmp_path, mocker, stored, expected):
    from jailbee.autostart_profiles import resolve_profile, PROFILE_KEY
    incus, _ = state_incus(mocker, {PROFILE_KEY: stored})
    warning = mocker.patch("jailbee.autostart_profiles.warn")
    assert resolve_profile(profile_cfg(make_cfg, tmp_path), incus, "c") == expected
    assert warning.call_count == (1 if stored == "gone" else 0)


def test_resolve_profile_no_profiles(make_cfg, tmp_path, mocker):
    from jailbee.autostart_profiles import resolve_profile
    incus, _ = state_incus(mocker)
    assert resolve_profile(make_cfg(tmp_path), incus, "c") is None
    incus.config_get.assert_not_called()


def test_profiled_config_carries_filtered_block(make_cfg, tmp_path):
    from jailbee.autostart_profiles import profiled_config
    cfg = profile_cfg(make_cfg, tmp_path)
    filtered = profiled_config(cfg, "light")
    assert [s.stage for s in filtered.autostart.on_create] == ["deps"]
    assert [s.stage for s in cfg.autostart.on_create] == ["deps", "images"]


def test_created_empty_key_is_not_a_legacy_container(mocker):
    from jailbee.autostart_profiles import created_stages
    incus = mocker.MagicMock()
    incus.config_get.return_value = None  # Incus wrapper collapses empty stdout
    incus.config_show.return_value = 'config:\n  user.jailbee.autostart_created: ""\n'
    assert created_stages(incus, "c") == frozenset()


def test_record_created_idempotent_order_preserved(mocker):
    from jailbee.autostart_profiles import record_created, CREATED_KEY
    incus, state = state_incus(mocker, {CREATED_KEY: "deps"})
    record_created(incus, "c", "images")
    record_created(incus, "c", "deps")
    assert state[CREATED_KEY] == "deps,images"


def test_missing_on_create_legacy_container_is_empty(make_cfg, tmp_path, mocker):
    from jailbee.autostart_profiles import missing_on_create
    incus, _ = state_incus(mocker)
    block = missing_on_create(profile_cfg(make_cfg, tmp_path), incus, "c", "full")
    assert block.on_create == []
    assert block.on_start == []


def test_failed_on_create_stage_is_recorded(make_cfg, tmp_path, mocker):
    from jailbee.autostart import run_stages
    from jailbee.autostart_profiles import CREATED_KEY
    cfg = profile_cfg(make_cfg, tmp_path)
    incus, state = state_incus(mocker, {CREATED_KEY: ""})
    mocker.patch("jailbee.autostart.run_stage", side_effect=RuntimeError("failed"))
    with pytest.raises(RuntimeError, match="failed"):
        run_stages(cfg, incus, "c", cfg.autostart.on_create, "/r", trigger=AutostartTrigger.ON_CREATE)
    assert state[CREATED_KEY] == "deps"


def test_no_profiles_repo_writes_no_keys(make_cfg, tmp_path, mocker):
    from jailbee.autostart import run_stages
    cfg = make_cfg(tmp_path)
    incus, state = state_incus(mocker)
    mocker.patch("jailbee.autostart.run_stage")
    run_stages(cfg, incus, "c", Autostart.model_validate({"on_create": [{"stage": "x"}]}).on_create,
               "/r", trigger=AutostartTrigger.ON_CREATE)
    assert not state


def test_background_new_job_roundtrips_autostart_profile():
    from jailbee.background import op_to_job, job_to_opts
    from tests.test_lifecycle_autostart_detach import _opts
    opts = _opts(autostart_profile="light")
    decoded, _, _ = job_to_opts(op_to_job(opts, container_name="c", log_path="/l"))
    assert decoded.autostart_profile == "light"


def test_new_stores_profile_and_seeds_created_empty_first(make_cfg, tmp_path, mocker):
    from jailbee.lifecycle import new_container
    from jailbee.autostart_profiles import PROFILE_KEY, CREATED_KEY
    from tests.test_lifecycle_autostart_detach import _cfg_for_new, _opts, _patch_new_container_deps
    from jailbee.autostart_plan import AutostartPlan
    cfg = _cfg_for_new(make_cfg, tmp_path).model_copy(update={"autostart": profile_cfg(make_cfg, tmp_path / "profiles").autostart})
    _patch_new_container_deps(mocker)
    incus, state = state_incus(mocker)
    incus.exists.return_value = False
    seen = []
    def execute(c, i, n, trigger, **kwargs):
        seen.append((dict(state), [s.stage for s in c.autostart.on_create]))
        return AutostartPlan([], [])
    mocker.patch("jailbee.autostart.run_autostart", side_effect=execute)
    new_container(cfg, incus, _opts(autostart_profile="light"))
    writes = [(c.args[1], c.args[2]) for c in incus.config_set.call_args_list if c.args[1] in (PROFILE_KEY, CREATED_KEY)]
    assert writes[:2] == [(PROFILE_KEY, "light"), (CREATED_KEY, "")]
    assert seen[0][0][CREATED_KEY] == ""
    assert seen[0][1] == ["deps"]


def test_start_runs_missing_on_create_then_on_start(make_cfg, tmp_path, mocker):
    from jailbee.cli import _post_start_actions
    from jailbee.autostart_profiles import CREATED_KEY, PROFILE_KEY
    from jailbee.autostart_plan import AutostartPlan
    cfg = profile_cfg(make_cfg, tmp_path)
    incus, _ = state_incus(mocker, {CREATED_KEY: "deps", PROFILE_KEY: "full"})
    for target in ("jailbee.hosts.sync_hosts", "jailbee.egress_proxy.sync_container", "jailbee.autostart.inject_github_token", "jailbee.cli._post_create_gui_launches"):
        mocker.patch(target)
    mocker.patch("jailbee.cli._mirror_endpoint_or_none", return_value=None)
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/r")
    runs = []
    def execute(c, i, n, trigger, **kwargs):
        runs.append((trigger.value, [s.stage for s in getattr(c.autostart, trigger.value)]))
        return AutostartPlan([], [])
    mocker.patch("jailbee.autostart.run_autostart", side_effect=execute)
    _post_start_actions(cfg, incus, "c", no_autostart=False)
    assert runs == [("on_create", ["images"]), ("on_start", ["frontend", "backend"])]


def test_new_unknown_profile_exits_2(make_cfg, tmp_path, mocker):
    from jailbee.lifecycle import new_container
    from jailbee.autostart_profiles import ProfileError
    from tests.test_lifecycle_autostart_detach import _cfg_for_new, _opts, _patch_new_container_deps
    cfg = _cfg_for_new(make_cfg, tmp_path).model_copy(update={"autostart": profile_cfg(make_cfg, tmp_path / "profiles").autostart})
    _patch_new_container_deps(mocker)
    incus, _ = state_incus(mocker)
    incus.exists.return_value = False
    with pytest.raises(ProfileError, match="autostart profile `bad` is not defined"):
        new_container(cfg, incus, _opts(autostart_profile="bad"))
    incus.init.assert_not_called()


def test_new_flag_in_repo_without_profiles_exits_2(make_cfg, tmp_path, mocker):
    from jailbee.lifecycle import new_container
    from jailbee.autostart_profiles import ProfileError
    from tests.test_lifecycle_autostart_detach import _cfg_for_new, _opts, _patch_new_container_deps
    _patch_new_container_deps(mocker)
    incus, _ = state_incus(mocker)
    incus.exists.return_value = False
    with pytest.raises(ProfileError, match="this repository defines no autostart profiles"):
        new_container(_cfg_for_new(make_cfg, tmp_path), incus, _opts(autostart_profile="bad"))


def test_new_no_autostart_records_all_profile_on_create(make_cfg, tmp_path, mocker):
    from jailbee.lifecycle import new_container
    from jailbee.autostart_profiles import CREATED_KEY
    from tests.test_lifecycle_autostart_detach import _cfg_for_new, _opts, _patch_new_container_deps
    cfg = _cfg_for_new(make_cfg, tmp_path).model_copy(update={"autostart": profile_cfg(make_cfg, tmp_path / "profiles").autostart})
    _patch_new_container_deps(mocker)
    incus, state = state_incus(mocker)
    incus.exists.return_value = False
    new_container(cfg, incus, _opts(autostart=False, autostart_profile="light"))
    assert state[CREATED_KEY] == "deps"
