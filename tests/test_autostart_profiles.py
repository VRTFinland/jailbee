"""Container profile state and attempted create stages."""

import pytest

from jailbee.autostart_plan import AutostartTrigger
from jailbee.config import Autostart


def profile_cfg(make_cfg, tmp_path):
    return make_cfg(
        tmp_path,
        autostart={
            "on_create": [{"stage": "deps", "steps": []}, {"stage": "images", "steps": []}],
            "on_start": [{"stage": "frontend", "steps": []}, {"stage": "backend", "steps": []}],
            "profiles": {"full": "all", "light": {"stages": ["deps", "frontend"]}},
            "default_profile": "light",
        },
    )


def state_incus(mocker, state=None):
    state = dict(state or {})
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda c, k: state.get(k)

    def set_config(c, k, v):
        if v:
            state[k] = v
        else:
            state.pop(k, None)  # Incus removes empty config values.

    incus.config_set.side_effect = set_config
    return incus, state


@pytest.mark.parametrize("stored,expected", [("full", "full"), (None, "light"), ("gone", "light")])
def test_resolve_profile_stored_absent_unknown(make_cfg, tmp_path, mocker, stored, expected):
    from jailbee.autostart_profiles import PROFILE_KEY, resolve_profile

    incus, _ = state_incus(mocker, {PROFILE_KEY: stored})
    warning = mocker.patch("jailbee.autostart_profiles.warn")
    assert resolve_profile(profile_cfg(make_cfg, tmp_path), incus, "c") == expected
    assert warning.call_count == (1 if stored == "gone" else 0)


def test_resolve_profile_no_profiles(make_cfg, tmp_path, mocker):
    from jailbee.autostart_profiles import resolve_profile

    incus, _ = state_incus(mocker)
    assert resolve_profile(make_cfg(tmp_path), incus, "c") is None


def test_profiled_config_carries_filtered_block(make_cfg, tmp_path):
    from jailbee.autostart_profiles import profiled_config

    cfg = profile_cfg(make_cfg, tmp_path)
    filtered = profiled_config(cfg, "light")
    assert [s.stage for s in filtered.autostart.on_create] == ["deps"]
    assert [s.stage for s in cfg.autostart.on_create] == ["deps", "images"]


def test_created_empty_marker_is_not_a_legacy_container(mocker):
    from jailbee.autostart_profiles import CREATED_KEY, created_stages

    incus, _ = state_incus(mocker, {CREATED_KEY: ","})
    assert created_stages(incus, "c") == frozenset()


def test_record_created_idempotent_order_preserved(mocker):
    from jailbee.autostart_profiles import CREATED_KEY, record_created

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
        run_stages(
            cfg, incus, "c", cfg.autostart.on_create, "/r", trigger=AutostartTrigger.ON_CREATE
        )
    assert state[CREATED_KEY] == "deps"


@pytest.mark.parametrize("named_profiles", [False, True])
def test_untracked_container_writes_no_keys(make_cfg, tmp_path, mocker, named_profiles):
    from jailbee.autostart import run_stages

    cfg = profile_cfg(make_cfg, tmp_path) if named_profiles else make_cfg(tmp_path)
    incus, state = state_incus(mocker)
    mocker.patch("jailbee.autostart.run_stage")
    run_stages(
        cfg,
        incus,
        "c",
        Autostart.model_validate({"on_create": [{"stage": "x"}]}).on_create,
        "/r",
        trigger=AutostartTrigger.ON_CREATE,
    )
    assert not state


def test_background_new_job_roundtrips_autostart_profile():
    from jailbee.background import job_to_opts, op_to_job
    from tests.test_lifecycle_autostart_detach import _opts

    opts = _opts(autostart_profile="light")
    decoded, _, _ = job_to_opts(op_to_job(opts, container_name="c", log_path="/l"))
    assert decoded.autostart_profile == "light"


def test_new_stores_profile_and_seeds_created_empty_first(make_cfg, tmp_path, mocker):
    from jailbee.autostart_plan import AutostartPlan
    from jailbee.autostart_profiles import CREATED_KEY, PROFILE_KEY
    from jailbee.lifecycle import new_container
    from tests.test_lifecycle_autostart_detach import _cfg_for_new, _opts, _patch_new_container_deps

    cfg = _cfg_for_new(make_cfg, tmp_path).model_copy(
        update={"autostart": profile_cfg(make_cfg, tmp_path / "profiles").autostart}
    )
    _patch_new_container_deps(mocker)
    incus, state = state_incus(mocker)
    incus.exists.return_value = False
    seen = []

    def execute(c, i, n, trigger, **kwargs):
        seen.append((dict(state), [s.stage for s in c.autostart.on_create]))
        return AutostartPlan([], [])

    mocker.patch("jailbee.autostart.run_autostart", side_effect=execute)
    new_container(cfg, incus, _opts(autostart_profile="light"))
    writes = [
        (c.args[1], c.args[2])
        for c in incus.config_set.call_args_list
        if c.args[1] in (PROFILE_KEY, CREATED_KEY)
    ]
    assert writes[:2] == [(PROFILE_KEY, "light"), (CREATED_KEY, ",")]
    assert seen[0][0][CREATED_KEY] == ","
    assert seen[0][1] == ["deps"]


@pytest.mark.parametrize("selection,encoded", [(None, None), ([], ","), (["web", "db"], "web,db")])
def test_new_stores_service_selection_before_autostart(
    make_cfg, tmp_path, mocker, selection, encoded
):
    from jailbee.autostart_plan import AutostartPlan
    from jailbee.autostart_profiles import SERVICES_KEY
    from jailbee.lifecycle import new_container
    from tests.test_lifecycle_autostart_detach import _cfg_for_new, _opts, _patch_new_container_deps

    cfg = _cfg_for_new(make_cfg, tmp_path)
    _patch_new_container_deps(mocker)
    incus, state = state_incus(mocker)
    incus.exists.return_value = False
    observed = []
    mocker.patch(
        "jailbee.autostart.run_autostart",
        side_effect=lambda *a, **kw: (observed.append(dict(state)), AutostartPlan([], []))[1],
    )
    new_container(cfg, incus, _opts(autostart_services=selection))
    writes = [
        call.args[2] for call in incus.config_set.call_args_list if call.args[1] == SERVICES_KEY
    ]
    assert writes == ([] if encoded is None else [encoded])
    assert observed and observed[0].get(SERVICES_KEY) == encoded


def test_start_runs_missing_on_create_then_on_start(make_cfg, tmp_path, mocker):
    from jailbee.autostart_plan import AutostartPlan
    from jailbee.autostart_profiles import CREATED_KEY, PROFILE_KEY
    from jailbee.cli import _post_start_actions

    cfg = profile_cfg(make_cfg, tmp_path)
    incus, _ = state_incus(mocker, {CREATED_KEY: "deps", PROFILE_KEY: "full"})
    for target in (
        "jailbee.hosts.sync_hosts",
        "jailbee.egress_proxy.sync_container",
        "jailbee.autostart.inject_github_token",
        "jailbee.cli._post_create_gui_launches",
    ):
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
    from jailbee.autostart_profiles import ProfileError
    from jailbee.lifecycle import new_container
    from tests.test_lifecycle_autostart_detach import _cfg_for_new, _opts, _patch_new_container_deps

    cfg = _cfg_for_new(make_cfg, tmp_path).model_copy(
        update={"autostart": profile_cfg(make_cfg, tmp_path / "profiles").autostart}
    )
    _patch_new_container_deps(mocker)
    incus, _ = state_incus(mocker)
    incus.exists.return_value = False
    with pytest.raises(ProfileError, match="autostart profile `bad` is not defined"):
        new_container(cfg, incus, _opts(autostart_profile="bad"))
    incus.init.assert_not_called()


def test_new_flag_in_repo_without_profiles_exits_2(make_cfg, tmp_path, mocker):
    from jailbee.autostart_profiles import ProfileError
    from jailbee.lifecycle import new_container
    from tests.test_lifecycle_autostart_detach import _cfg_for_new, _opts, _patch_new_container_deps

    _patch_new_container_deps(mocker)
    incus, _ = state_incus(mocker)
    incus.exists.return_value = False
    with pytest.raises(ProfileError, match="autostart profile `bad` is not defined"):
        new_container(_cfg_for_new(make_cfg, tmp_path), incus, _opts(autostart_profile="bad"))


def test_services_only_switch_runs_service_hook(make_cfg, tmp_path, mocker):
    from jailbee.autostart_profiles import PROFILE_KEY, switch_profile

    cfg = make_cfg(
        tmp_path,
        services={"db": {"run": "db"}},
        autostart={
            "on_start": [{"stage": "setup"}],
            "profiles": {
                "light": {"stages": ["setup"], "services": []},
                "full": {"stages": ["setup"], "services": ["db"]},
            },
            "default_profile": "light",
        },
    )
    incus, _ = state_incus(mocker, {PROFILE_KEY: "light"})
    incus.list_containers.return_value = [{"name": "c", "status": "Running"}]
    mocker.patch("jailbee.autostart_status.live_run", return_value=None)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/r")
    mocker.patch("jailbee.docker_daemon.mirror_endpoint_or_none", return_value=None)
    run = mocker.patch("jailbee.services.autostart.run_services_stage")
    switch_profile(cfg, incus, "c", "full", run=True)
    run.assert_called_once()
    assert run.call_args.kwargs["live_switch"] is True
    from jailbee.autostart_profiles import SERVICES_KEY

    incus, _ = state_incus(mocker, {PROFILE_KEY: "light", SERVICES_KEY: ","})
    incus.list_containers.return_value = [{"name": "c", "status": "Running"}]
    run.reset_mock()
    switch_profile(cfg, incus, "c", "full", run=True)
    run.assert_not_called()


def test_switch_to_all_uses_container_service_graph(make_cfg, tmp_path, mocker):
    import json

    from jailbee.autostart_profiles import PROFILE_KEY, switch_profile
    from jailbee.incus import ExecResult
    from jailbee.services.graph import plan_payload

    cfg = make_cfg(
        tmp_path,
        services={"db": {"run": "db"}},
        autostart={
            "on_start": [{"stage": "setup"}],
            "profiles": {
                "light": {"stages": ["setup"], "services": ["db"]},
                "full": "all",
            },
            "default_profile": "light",
        },
    )
    container_cfg = make_cfg(tmp_path, services={"db": {"run": "db"}, "web": {"run": "web"}})
    data = plan_payload(container_cfg.services, [], "start", step_timeout=30)
    incus, state = state_incus(mocker, {PROFILE_KEY: "light"})
    incus.config_get_checked.side_effect = lambda c, k: state.get(k)
    incus.list_containers.return_value = [{"name": "c", "status": "Running"}]
    mocker.patch("jailbee.autostart_status.live_run", return_value=None)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/r")
    mocker.patch("jailbee.docker_daemon.mirror_endpoint_or_none", return_value=None)
    mocker.patch(
        "jailbee.container_app.run_in_container",
        return_value=ExecResult(0, json.dumps(data).encode(), b""),
    )
    run = mocker.patch("jailbee.autostart.run_stage")
    agents = mocker.patch("jailbee.autostart.run_autostart")

    result = switch_profile(cfg, incus, "c", "full", run=True)

    assert not result.deferred
    assert run.call_args.args[3].chains[0].steps[0].run.endswith("start db web")
    agents.assert_not_called()


def test_switch_to_heavier_runs_missing_on_create_then_new_on_start(make_cfg, tmp_path, mocker):
    from jailbee.autostart_plan import AutostartPlan
    from jailbee.autostart_profiles import CREATED_KEY, PROFILE_KEY, switch_profile

    cfg = profile_cfg(make_cfg, tmp_path)
    incus, state = state_incus(mocker, {PROFILE_KEY: "light", CREATED_KEY: "deps"})
    incus.list_containers.return_value = [{"name": "c", "status": "Running"}]
    mocker.patch("jailbee.autostart_status.live_run", return_value=None)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/r")
    runs = []
    mocker.patch(
        "jailbee.docker_daemon.mirror_endpoint_or_none",
        create=True,
        return_value=("10.0.0.2", 3128),
    )

    def execute(c, i, n, trigger, **kwargs):
        runs.extend(s.stage for s in getattr(c.autostart, trigger.value))
        assert kwargs["mirror_endpoint"] == ("10.0.0.2", 3128)
        assert kwargs["override"] == "wait"
        return AutostartPlan([], [])

    mocker.patch("jailbee.autostart.run_autostart", side_effect=execute)
    result = switch_profile(cfg, incus, "c", "full", run=True)
    assert runs == ["images", "backend"]
    assert result.ran_on_create == ("images",)
    assert result.ran_on_start == ("backend",)
    assert state[PROFILE_KEY] == "full"


@pytest.mark.parametrize("running,run", [(False, True), (True, False)])
def test_switch_stopped_container_only_stores(make_cfg, tmp_path, mocker, running, run):
    from jailbee.autostart_profiles import PROFILE_KEY, switch_profile

    cfg = profile_cfg(make_cfg, tmp_path)
    incus, state = state_incus(mocker)
    incus.list_containers.return_value = [
        {"name": "c", "status": "Running" if running else "Stopped"}
    ]
    mocker.patch("jailbee.autostart_status.live_run", return_value=None)
    execute = mocker.patch("jailbee.autostart.run_autostart")
    assert switch_profile(cfg, incus, "c", "full", run=run).deferred
    assert state[PROFILE_KEY] == "full"
    execute.assert_not_called()


def test_switch_no_run_only_stores(make_cfg, tmp_path, mocker):
    test_switch_stopped_container_only_stores(make_cfg, tmp_path, mocker, True, False)


def test_switch_refused_during_live_detached_run_stores_nothing(make_cfg, tmp_path, mocker):
    from jailbee.autostart_profiles import switch_profile

    cfg = profile_cfg(make_cfg, tmp_path)
    incus, state = state_incus(mocker)
    mocker.patch("jailbee.autostart_status.live_run", return_value=mocker.MagicMock())
    with pytest.raises(RuntimeError, match="autostart stages running"):
        switch_profile(cfg, incus, "c", "full", run=True)
    assert not state


def test_background_new_rejects_unknown_profile_before_launch(make_cfg, tmp_path, mocker):
    from jailbee.create_flow import CreateError, Interaction, preflight_background
    from jailbee.lifecycle import BranchAutostartAssessment
    from tests.test_lifecycle_autostart_detach import _opts

    cfg = profile_cfg(make_cfg, tmp_path)
    mocker.patch("jailbee.lifecycle.resolve_clone_ref")
    mocker.patch(
        "jailbee.lifecycle.assess_branch_autostart",
        return_value=BranchAutostartAssessment(ref=None, effective_cfg=cfg, verdict=None),
    )
    with pytest.raises(CreateError, match="autostart profile `bad` is not defined") as exc:
        preflight_background(cfg, _opts(autostart_profile="bad"), interaction=Interaction.none())
    assert exc.value.exit_code == 2


def test_new_no_autostart_records_all_profile_on_create(make_cfg, tmp_path, mocker):
    from jailbee.autostart_profiles import CREATED_KEY
    from jailbee.lifecycle import new_container
    from tests.test_lifecycle_autostart_detach import _cfg_for_new, _opts, _patch_new_container_deps

    cfg = _cfg_for_new(make_cfg, tmp_path).model_copy(
        update={"autostart": profile_cfg(make_cfg, tmp_path / "profiles").autostart}
    )
    _patch_new_container_deps(mocker)
    incus, state = state_incus(mocker)
    incus.exists.return_value = False
    new_container(cfg, incus, _opts(autostart=False, autostart_profile="light"))
    assert state[CREATED_KEY] == "deps"


def test_stored_none_honoured_without_repo_profiles(make_cfg, tmp_path, mocker):
    from jailbee.autostart_profiles import PROFILE_KEY, resolve_profile

    incus, _ = state_incus(mocker, {PROFILE_KEY: "none"})
    assert resolve_profile(make_cfg(tmp_path), incus, "c") == "none"


def test_new_none_selects_no_services(make_cfg, tmp_path, mocker):
    from jailbee.lifecycle import new_container
    from tests.test_lifecycle_autostart_detach import _cfg_for_new, _opts, _patch_new_container_deps

    cfg = _cfg_for_new(make_cfg, tmp_path)
    _patch_new_container_deps(mocker)
    incus, _ = state_incus(mocker)
    incus.exists.return_value = False
    services = mocker.patch("jailbee.services.autostart.approval_envelope", return_value="ok")
    new_container(cfg, incus, _opts(autostart_profile="none"))
    assert services.call_args.args[1] == []


def test_services_selection_honours_stored_builtin(make_cfg, tmp_path, mocker):
    from jailbee.autostart_profiles import PROFILE_KEY
    from jailbee.services.autostart import selected_services

    cfg = make_cfg(tmp_path, services={"db": {"run": "db"}})
    incus, _ = state_incus(mocker, {PROFILE_KEY: "none"})
    assert selected_services(cfg, incus, "c") == []


def test_start_honours_stored_builtin_without_repo_profiles(make_cfg, tmp_path, mocker):
    from jailbee.autostart_plan import AutostartPlan
    from jailbee.autostart_profiles import PROFILE_KEY
    from jailbee.cli import _post_start_actions

    cfg = make_cfg(tmp_path, autostart={"on_start": [{"stage": "web"}]})
    incus, _ = state_incus(mocker, {PROFILE_KEY: "none"})
    for target in ("jailbee.hosts.sync_hosts", "jailbee.egress_proxy.sync_container",
                   "jailbee.autostart.inject_github_token", "jailbee.cli._post_create_gui_launches"):
        mocker.patch(target)
    mocker.patch("jailbee.cli._mirror_endpoint_or_none", return_value=None)
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/r")
    run = mocker.patch("jailbee.autostart.run_autostart", return_value=AutostartPlan([], []))
    _post_start_actions(cfg, incus, "c", no_autostart=False)
    assert run.call_args.args[0].autostart.on_start == []


@pytest.mark.parametrize("use_default", [False, True])
def test_new_builtin_all_records_real_create_attempts(make_cfg, tmp_path, mocker, use_default):
    from jailbee.autostart_profiles import CREATED_KEY, missing_on_create
    from jailbee.lifecycle import new_container
    from tests.test_lifecycle_autostart_detach import _cfg_for_new, _opts, _patch_new_container_deps

    cfg = _cfg_for_new(make_cfg, tmp_path).model_copy(update={
        "autostart": Autostart.model_validate({
            "on_create": [{"stage": "deps"}, {"stage": "images"}],
            **({"default_profile": "all"} if use_default else {}),
        })
    })
    _patch_new_container_deps(mocker)
    incus, state = state_incus(mocker)
    incus.exists.return_value = False
    run = mocker.patch("jailbee.autostart.run_stage")
    name = new_container(cfg, incus, _opts(autostart_profile=None if use_default else "all"))
    assert [call.args[3].stage for call in run.call_args_list] == ["deps", "images"]
    assert state[CREATED_KEY] == "deps,images"
    assert missing_on_create(cfg, incus, name, "all").on_create == []


@pytest.mark.parametrize("detached", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_builtin_create_attempts_skip_completed_and_failed_stages(
    make_cfg, tmp_path, mocker, detached, failed
):
    from jailbee.autostart import run_detached, run_stages
    from jailbee.autostart_profiles import CREATED_KEY, PROFILE_KEY, missing_on_create
    from tests.test_autostart_supervisor import _spec

    cfg = make_cfg(tmp_path, autostart={"on_create": [
        {"stage": "deps", "detach": detached}, {"stage": "images"}
    ]})
    incus, state = state_incus(mocker, {PROFILE_KEY: "all", CREATED_KEY: ","})
    run = mocker.patch("jailbee.autostart.run_stage")
    if failed:
        run.side_effect = RuntimeError("failed")

    def execute():
        if detached:
            run_detached(cfg, incus, _spec(tmp_path, cfg.autostart, from_trigger="on_create"))
        else:
            run_stages(cfg, incus, "c1", cfg.autostart.on_create, "/r",
                       trigger=AutostartTrigger.ON_CREATE)

    if failed:
        with pytest.raises(RuntimeError, match="failed"):
            execute()
    else:
        execute()
    assert state[CREATED_KEY] == ("deps" if failed else "deps,images")
    missing = missing_on_create(cfg, incus, "c1", "all")
    assert [s.stage for s in missing.on_create] == (["images"] if failed else [])
    run.reset_mock(side_effect=True)
    run_stages(cfg, incus, "c1", missing.on_create, "/r", trigger=AutostartTrigger.ON_CREATE)
    assert [call.args[3].stage for call in run.call_args_list] == (["images"] if failed else [])
    assert missing_on_create(cfg, incus, "c1", "all").on_create == []


def test_builtin_none_to_all_live_switch_records_once(make_cfg, tmp_path, mocker):
    from jailbee.autostart_profiles import CREATED_KEY, PROFILE_KEY, switch_profile

    cfg = make_cfg(tmp_path, autostart={"on_create": [{"stage": "deps"}]})
    incus, state = state_incus(mocker, {PROFILE_KEY: "none", CREATED_KEY: ","})
    incus.list_containers.return_value = [{"name": "c", "status": "Running"}]
    mocker.patch("jailbee.autostart_status.live_run", return_value=None)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/r")
    mocker.patch("jailbee.docker_daemon.mirror_endpoint_or_none", return_value=None)
    mocker.patch("jailbee.services.autostart.run_services_stage")
    run = mocker.patch("jailbee.autostart.run_stage")
    switch_profile(cfg, incus, "c", "all", run=True)
    assert state[CREATED_KEY] == "deps"
    switch_profile(cfg, incus, "c", "none", run=True)
    switch_profile(cfg, incus, "c", "all", run=True)
    assert [call.args[3].stage for call in run.call_args_list] == ["deps"]


@pytest.mark.parametrize("selection", ["none", "empty", "all"])
def test_real_autostart_service_selection_preserves_generated_agents(
    make_cfg, tmp_path, mocker, selection
):
    import json

    from jailbee.autostart import run_autostart
    from jailbee.autostart_profiles import PROFILE_KEY, SERVICES_KEY, profiled_config
    from jailbee.incus import ExecResult
    from jailbee.services import autostart as service_autostart
    from jailbee.services.graph import plan_payload

    cfg = make_cfg(tmp_path, services={"db": {"run": "db"}},
                   agents={"codex": {"enabled": True, "autostart": True}})
    state = {PROFILE_KEY: "none" if selection == "none" else "all"}
    if selection == "empty":
        state[SERVICES_KEY] = ","
    incus, _ = state_incus(mocker, state)
    incus.config_get_checked.side_effect = incus.config_get.side_effect
    mocker.patch("jailbee.initial_prompt.prompt_pending", return_value=False)
    mocker.patch("jailbee.orchestration.labels.child_env", return_value={})
    mocker.patch("jailbee.orchestration.labels.orchestrator_env", return_value={})
    mocker.patch("jailbee.orchestration.runner.read_runner", return_value=None)
    mocker.patch("jailbee.orchestration.runner.read_resume", return_value=None)
    # A real plan response makes accidental service submission observable at
    # both the plan boundary and the generated start command.
    payload = plan_payload(cfg.services, [], "start", step_timeout=30)
    submit = mocker.patch("jailbee.container_app.run_in_container",
                          return_value=ExecResult(0, json.dumps(payload).encode(), b""))
    service_run = mocker.spy(service_autostart, "run_services_stage")
    run = mocker.patch("jailbee.autostart.run_stage")
    run_autostart(profiled_config(cfg, state[PROFILE_KEY]), incus, "c",
                  AutostartTrigger.ON_START, "/r", override="wait")
    stages = [call.args[3] for call in run.call_args_list]
    agent = next(s for s in stages if s.stage == "agents")
    assert [(step.name, step.run) for chain in agent.all_chains() for step in chain.steps] == [
        ("codex", "exec codex")
    ]
    assert service_run.call_count == (0 if selection == "empty" else 1)
    if selection == "empty":
        # The planner omits this slot; the service executor must also reject
        # an explicit empty override if invoked directly or by a saved plan.
        service_autostart.run_services_stage(
            cfg, incus, "c", "/r", mirror_endpoint=None,
            on_progress=None, cas_restore=False,
        )
    if selection == "all":
        assert submit.call_args.args[3] == [
            "service", "--repo", "/r", "plan", "--op", "start", "--json"
        ]
        assert next(s for s in stages if s.stage == "services").all_chains()[0].steps[0].run.endswith("start db")
    else:
        submit.assert_not_called()
        assert [s.stage for s in stages] == ["agents"]
