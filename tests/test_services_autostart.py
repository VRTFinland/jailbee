"""Host autostart service stage."""

import json
from unittest.mock import MagicMock

import pytest

from jailbee import autostart, container_app
from jailbee.autostart_profiles import PREPARED_KEY, SERVICES_KEY, decode_names, encode_names
from jailbee.config import CONTAINER_USERNAME
from jailbee.incus import ExecResult, IncusError
from jailbee.services.autostart import run_services_stage, selected_services, services_stage_wanted


def labels():
    incus = MagicMock()
    values = {}
    incus.config_get.side_effect = lambda container, key: values.get(key)
    incus.config_set.side_effect = lambda container, key, value: values.__setitem__(key, value)
    return incus, values


def invoke(cfg, incus):
    run_services_stage(
        cfg, incus, "box", "/repo space", mirror_endpoint=None, on_progress=None, cas_restore=False
    )


def payload():
    return {
        "order": ["db", "web"],
        "services": [
            {
                "name": "db",
                "prepare": [
                    {
                        "name": "prep",
                        "run": "setup",
                        "env": {},
                        "working_dir": "",
                        "timeout": 3,
                        "continue_on_error": False,
                    }
                ],
                "elevation": {"network": "loose", "mounts": []},
                "ready_timeout": 10,
            },
            {
                "name": "web",
                "prepare": [],
                "elevation": {"network": None, "mounts": []},
                "ready_timeout": 20,
            },
        ],
    }


def test_selection_and_presence(tmp_path, make_cfg):
    cfg = make_cfg(
        tmp_path,
        services={"db": {"run": "db"}},
        autostart={
            "on_start": [{"stage": "setup", "steps": [{"name": "x", "run": "true"}]}],
            "profiles": {"p": {"stages": ["setup"], "services": ["db"]}},
            "default_profile": "p",
        },
    )
    incus, values = labels()
    assert selected_services(cfg, incus, "box") == ["db"]
    assert services_stage_wanted(cfg, incus, "box")
    values[SERVICES_KEY] = "web"
    assert selected_services(cfg, incus, "box") == ["web"]
    values[SERVICES_KEY] = ","
    assert selected_services(cfg, incus, "box") == []
    assert not services_stage_wanted(cfg, incus, "box")
    assert decode_names(None) is None
    assert decode_names(encode_names([])) == []
    assert decode_names(encode_names(["db", "web"])) == ["db", "web"]


def test_child_prepare_refuses_ungranted_loose_before_stage(tmp_path, mocker, make_cfg):
    from jailbee.orchestration import labels as orch_labels
    from jailbee.orchestration.grant import ChildRights

    cfg = make_cfg(tmp_path)
    incus, values = labels()
    values[orch_labels.SPAWNED_BY] = "parent"
    values[orch_labels.RIGHTS] = ChildRights().to_label()
    mocker.patch.object(container_app, "run_in_container", return_value=ExecResult(0, json.dumps(payload()).encode(), b""))
    run = mocker.patch.object(autostart, "run_stage")
    with pytest.raises(autostart.AutostartStepError, match="loose"):
        invoke(cfg, incus)
    run.assert_not_called()


def test_child_prepare_refuses_ungranted_mount(tmp_path, mocker, make_cfg):
    from jailbee.orchestration import labels as orch_labels
    from jailbee.orchestration.grant import ChildRights

    cfg = make_cfg(tmp_path)
    incus, values = labels()
    values[orch_labels.SPAWNED_BY] = "parent"
    values[orch_labels.RIGHTS] = ChildRights(loose=True).to_label()
    data = payload()
    data["services"][0]["elevation"]["mounts"] = ["aws"]
    mocker.patch.object(container_app, "run_in_container", return_value=ExecResult(0, json.dumps(data).encode(), b""))
    run = mocker.patch.object(autostart, "run_stage")
    with pytest.raises(autostart.AutostartStepError, match="child rights: aws"):
        invoke(cfg, incus)
    run.assert_not_called()


def test_prepare_then_start_and_skip(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path)
    incus, values = labels()
    metadata = mocker.patch.object(
        container_app,
        "run_in_container",
        return_value=ExecResult(0, json.dumps(payload()).encode(), b""),
    )
    mocker.patch.object(container_app, "expected_version", return_value="1.2 hash")
    run = mocker.patch.object(autostart, "run_stage")
    invoke(cfg, incus)
    metadata.assert_called_once_with(
        cfg,
        incus,
        "box",
        ["service", "--repo", "/repo space", "plan", "--op", "start", "--json"],
        uid=cfg.container_user.uid,
        cwd="/repo space",
        env={"HOME": f"/home/{CONTAINER_USERNAME}"},
        init_groups=True,
    )
    assert [c.args[3].stage for c in run.call_args_list] == ["prepare-db", "services"]
    stage = run.call_args_list[0].args[3]
    assert stage.network == "loose" and stage.chains[0].steps[0].run == "setup"
    start = run.call_args_list[1].args[3].chains[0].steps[0]
    assert (
        start.run == "jailbee --expect-version '1.2 hash' service --repo '/repo space' start db web"
    )
    assert start.timeout == 60
    assert values[PREPARED_KEY] == "db"
    run.reset_mock()
    invoke(cfg, incus)
    assert [c.args[3].stage for c in run.call_args_list] == ["services"]


def test_prepare_failure_not_recorded(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path)
    incus, values = labels()
    mocker.patch.object(
        container_app,
        "run_in_container",
        return_value=ExecResult(0, json.dumps(payload()).encode(), b""),
    )
    error = autostart.AutostartStepError(
        container="box", step_name="prep", reason="exit", exit_code=2
    )
    run = mocker.patch.object(autostart, "run_stage", side_effect=error)
    with pytest.raises(autostart.AutostartStepError) as caught:
        invoke(cfg, incus)
    assert caught.value.step_name == "prepare-db"
    assert caught.value.reason == "exit" and caught.value.exit_code == 2
    assert PREPARED_KEY not in values and run.call_count == 1


def test_prepare_infrastructure_error_wrapped(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path)
    incus, values = labels()
    mocker.patch.object(
        container_app,
        "run_in_container",
        return_value=ExecResult(0, json.dumps(payload()).encode(), b""),
    )
    error = IncusError("host mount path missing")
    run = mocker.patch.object(autostart, "run_stage", side_effect=error)
    with pytest.raises(autostart.AutostartStepError) as caught:
        invoke(cfg, incus)
    assert caught.value.step_name == "prepare-db"
    assert "host mount path missing" in caught.value.reason
    assert caught.value.original is error
    assert PREPARED_KEY not in values and run.call_count == 1


def test_prepare_cancellation_propagates_unchanged(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path)
    incus, values = labels()
    mocker.patch.object(
        container_app,
        "run_in_container",
        return_value=ExecResult(0, json.dumps(payload()).encode(), b""),
    )
    error = autostart.AutostartCancelledError(container="box", signum=15)
    mocker.patch.object(autostart, "run_stage", side_effect=error)
    with pytest.raises(autostart.AutostartCancelledError) as caught:
        invoke(cfg, incus)
    assert caught.value is error
    assert PREPARED_KEY not in values


def test_unknown_mount(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path)
    incus, _ = labels()
    data = payload()
    data["services"][0]["elevation"]["mounts"] = ["missing"]
    mocker.patch.object(
        container_app,
        "run_in_container",
        return_value=ExecResult(0, json.dumps(data).encode(), b""),
    )
    with pytest.raises(autostart.AutostartStepError, match="optional mount 'missing'"):
        invoke(cfg, incus)


def test_old_image_and_plan_error_and_empty(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path)
    incus, _ = labels()
    call = mocker.patch.object(
        container_app,
        "run_in_container",
        side_effect=container_app.ContainerJailbeeError("old image"),
    )
    warn = mocker.patch("jailbee.services.autostart.warn")
    run = mocker.patch.object(autostart, "run_stage")
    invoke(cfg, incus)
    assert "old image" in warn.call_args.args[0]
    call.side_effect = None
    call.return_value = ExecResult(2, b"", b"unknown service 'x'\nmore")
    with pytest.raises(autostart.AutostartStepError, match="unknown service 'x'") as caught:
        invoke(cfg, incus)
    assert caught.value.step_name == "services"
    call.return_value = ExecResult(0, b'{"order": [], "services": []}', b"")
    invoke(cfg, incus)
    run.assert_not_called()


def test_dispatch_services_stage(tmp_path, mocker, make_cfg):
    from jailbee.autostart_plan import SERVICES_STAGE
    from jailbee.config import AutostartStage
    from jailbee.services import autostart as service_autostart

    cfg = make_cfg(tmp_path)
    incus, _ = labels()
    dispatch = mocker.patch.object(service_autostart, "run_services_stage")
    ordinary = mocker.patch.object(autostart, "run_stage")
    stage = AutostartStage.model_construct(
        stage=SERVICES_STAGE, network=None, mounts=[], detach=False, chains=[], steps=[]
    )
    autostart.run_stages(cfg, incus, "box", [stage], "/repo")
    dispatch.assert_called_once()
    ordinary.assert_not_called()


def test_all_profile_and_explicit_selection_with_empty_host(tmp_path, make_cfg):
    cfg = make_cfg(tmp_path, autostart={"profiles": {"p": "all"}, "default_profile": "p"})
    incus, values = labels()
    assert selected_services(cfg, incus, "box") is None
    assert not services_stage_wanted(cfg, incus, "box")
    values[SERVICES_KEY] = "web"
    assert services_stage_wanted(cfg, incus, "box")


def test_wider_selection_prepares_only_new_service(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path)
    incus, values = labels()
    data = payload()
    data["services"][1]["prepare"] = [
        {
            "name": "web-prep",
            "run": "setup web",
            "env": {},
            "working_dir": "",
            "timeout": 3,
            "continue_on_error": False,
        }
    ]
    values[PREPARED_KEY] = "db"
    mocker.patch.object(
        container_app,
        "run_in_container",
        return_value=ExecResult(0, json.dumps(data).encode(), b""),
    )
    run = mocker.patch.object(autostart, "run_stage")
    invoke(cfg, incus)
    assert [c.args[3].stage for c in run.call_args_list] == ["prepare-web", "services"]
    assert values[PREPARED_KEY] == "db,web"
