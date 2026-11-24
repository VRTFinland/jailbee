"""Host-wide lifecycle commands, observed at their command-function boundary."""

import json
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.db import state_dir
from jailbee.incus import Incus, IncusError


@pytest.fixture
def host(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path / "one", container_prefix="one")
    other = make_cfg(tmp_path / "two", container_prefix="two")
    cfg.repo_root.mkdir()
    other.repo_root.mkdir()
    mocker.patch("jailbee.cli._load_orphan_base_or_exit", return_value=cfg)
    mocker.patch("jailbee.dashboard.model.registered_repo_roots", return_value=[other.repo_root])
    mocker.patch(
        "jailbee.config.load_repo_config",
        side_effect=lambda p: other if p == other.repo_root else cfg,
    )
    incus = Mock(spec=Incus)
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    incus.list_containers.return_value = [
        {"name": "one-work", "status": "Running", "profiles": ["one-base"]},
        {
            "name": "two-child",
            "status": "Running",
            "profiles": ["two-base"],
            "config": {"user.jailbee.spawned_by": "two-parent"},
        },
        {
            "name": "one-mount",
            "status": "Running",
            "profiles": ["one-base"],
            "config": {"user.jailbee.mode": "mount"},
        },
        {"name": "one-off", "status": "Stopped", "profiles": ["one-base"]},
        {
            "name": "jailbee-registry-mirror",
            "status": "Running",
            "profiles": ["default"],
            "config": {"boot.autostart": "true"},
        },
    ]
    incus.exec.return_value = "present"
    capture = mocker.patch("jailbee.agent_resume.capture")
    return cfg, other, incus, capture


def recorded():
    return json.loads((state_dir() / "resume-set.json").read_text())["containers"]


def test_stop_all_records_only_successful_stops_across_repos(host):
    cfg, _other, incus, capture = host
    incus.stop.side_effect = [None, IncusError("cannot stop child"), None]

    result = CliRunner().invoke(app, ["stop", "--all", "--yes"])

    assert result.exit_code == 1, result.output
    assert recorded() == {"one-work": str(cfg.repo_root), "one-mount": str(cfg.repo_root)}
    assert [c.args[0] for c in incus.stop.call_args_list] == ["one-work", "two-child", "one-mount"]
    assert [(c.args[0].container_prefix, c.args[2]) for c in capture.call_args_list] == [
        ("one", "one-work"),
        ("two", "two-child"),
        ("one", "one-mount"),
    ]
    assert "cannot stop child" in result.output
    assert "one-mount" in result.output


def test_resume_restores_stopped_and_running_without_session(host, mocker):
    cfg, _other, incus, _ = host
    path = state_dir() / "resume-set.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "containers": {"one-off": str(cfg.repo_root)}}))
    post = mocker.patch("jailbee.cli._post_start_actions")
    mocker.patch("jailbee.create_flow.preflight_cache_pools")
    mocker.patch("jailbee.cli._mirror_endpoint_or_none", return_value=None)
    # The real boot path is covered separately; leave its start call observable.
    mocker.patch(
        "jailbee.lifecycle.boot_container",
        side_effect=lambda cfg, incus, name, **kw: incus.start(name) if name == "one-off" else None,
    )

    def probe(name, argv, **kwargs):
        if "has-session" in " ".join(argv) and name != "one-work":
            return "missing"
        return "present"

    incus.exec.side_effect = probe
    result = CliRunner().invoke(app, ["resume"])

    assert result.exit_code == 0, result.output
    incus.start.assert_called_once_with("one-off")
    assert {c.args[2] for c in post.call_args_list} == {"one-off", "one-mount", "two-child"}
    assert all(c.kwargs["override"] == "wait" for c in post.call_args_list)
    assert recorded() == {}


@pytest.mark.parametrize("answer, expected", [("y\n", 0), ("n\n", 0), (None, 2)])
def test_stop_all_confirmation(host, mocker, answer, expected):
    _, _, incus, _ = host
    mocker.patch("jailbee.prompting.is_interactive", return_value=answer is not None)
    result = CliRunner().invoke(app, ["stop", "--all"], input=answer)
    assert result.exit_code == expected, result.output
    assert all(name in result.output for name in ["one-work", "two-child", "one-mount"])
    if answer == "y\n":
        assert incus.stop.call_count == 3
    else:
        incus.stop.assert_not_called()
    if answer is None:
        assert "--yes" in result.output


@pytest.mark.parametrize("kind", ["create", "boot", "autostart", "stop"])
def test_stop_all_skips_live_jobs(host, kind):
    import os
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee.background import start_job
    from jailbee.db import get_engine

    _, _, incus, _ = host
    with Session(get_engine()) as session:
        start_job(
            session,
            container_name="one-work",
            container_prefix="one",
            branch=None,
            pid=os.getpid(),
            log_path="/tmp/job",
            now=datetime.now(UTC),
            op_kind=kind,
        )
    result = CliRunner().invoke(app, ["stop", "--all", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Skipping one-work: live background job" in result.output
    assert "one-work" not in recorded()
    assert [c.args[0] for c in incus.stop.call_args_list] == ["two-child", "one-mount"]


def test_resume_keeps_failures_and_live_boot_entries(host, mocker):
    import os
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee.background import start_job
    from jailbee.db import get_engine

    cfg, other, incus, _ = host
    entries = {
        "one-off": str(cfg.repo_root),
        "two-child": str(other.repo_root),
        "one-mount": str(cfg.repo_root),
    }
    path = state_dir() / "resume-set.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "containers": entries}))
    with Session(get_engine()) as session:
        start_job(
            session,
            container_name="two-child",
            container_prefix="two",
            branch=None,
            pid=os.getpid(),
            log_path="/tmp/job",
            now=datetime.now(UTC),
            op_kind="boot",
        )
    mocker.patch("jailbee.lifecycle.boot_container")
    mocker.patch("jailbee.create_flow.preflight_cache_pools")
    post = mocker.patch(
        "jailbee.cli._post_start_actions", side_effect=[IncusError("boot failed"), None]
    )

    def probe(name, argv, **kwargs):
        if "has-session" in " ".join(argv) and name != "one-work":
            return "missing"
        return "present"

    incus.exec.side_effect = probe
    result = CliRunner().invoke(app, ["resume"])
    assert result.exit_code == 1, result.output
    assert [c.args[2] for c in post.call_args_list] == ["one-off", "one-mount"]
    assert recorded() == {"one-off": str(cfg.repo_root), "two-child": str(other.repo_root)}
    assert "boot failed" in result.output
    assert "Skipping two-child" in result.output
    assert "Resumed: one-mount" in result.output


def test_resume_nothing_to_do(host):
    result = CliRunner().invoke(app, ["resume"])
    assert result.exit_code == 0, result.output
    assert "Nothing to resume" in result.output


def test_repeated_stop_preserves_earlier_set(host):
    cfg, _, _incus, _ = host
    path = state_dir() / "resume-set.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "containers": {"one-off": str(cfg.repo_root)}}))
    result = CliRunner().invoke(app, ["stop", "--all", "--yes"])
    assert result.exit_code == 0, result.output
    assert set(recorded()) == {"one-off", "one-work", "two-child", "one-mount"}


def test_resume_retains_running_failure_for_retry_even_if_session_now_exists(host, mocker):
    _, _, incus, _ = host
    incus.list_containers.return_value = incus.list_containers.return_value[:1]

    def probe(*args, **kwargs):
        return "missing"

    incus.exec.side_effect = probe
    start = mocker.patch("jailbee.lifecycle.boot_container")
    mocker.patch("jailbee.create_flow.preflight_cache_pools")
    post = mocker.patch("jailbee.cli._post_start_actions", side_effect=IncusError("stage failed"))
    first = CliRunner().invoke(app, ["resume"])
    assert first.exit_code == 1, first.output
    assert "one-work" in recorded()
    incus.exec.side_effect = None
    incus.exec.return_value = "present"
    post.side_effect = None
    second = CliRunner().invoke(app, ["resume"])
    assert second.exit_code == 0, second.output
    assert start.call_count == 2
    assert recorded() == {}


def test_resume_does_not_treat_transport_failure_as_missing_session(host, mocker):
    _, _, incus, _ = host
    incus.list_containers.return_value = incus.list_containers.return_value[:1]
    incus.exec.side_effect = IncusError("exec transport unavailable")
    start = mocker.patch("jailbee.lifecycle.boot_container")
    result = CliRunner().invoke(app, ["resume"])
    assert result.exit_code == 1, result.output
    start.assert_not_called()
    assert "transport unavailable" in result.output


def test_stop_all_durably_records_intent_before_stopping(host):
    cfg, _, incus, _ = host
    incus.list_containers.return_value = incus.list_containers.return_value[:1]

    def stop(name, **kwargs):
        data = json.loads((state_dir() / "resume-set.json").read_text())
        assert data["stopping"] == {"one-work": str(cfg.repo_root)}
        assert data["containers"] == {}

    incus.stop.side_effect = stop
    result = CliRunner().invoke(app, ["stop", "--all", "--yes"])
    assert result.exit_code == 0, result.output
    assert recorded() == {"one-work": str(cfg.repo_root)}


def test_resume_recovers_interrupted_stop_journal(host, mocker):
    cfg, _, incus, _ = host
    path = state_dir() / "resume-set.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "containers": {}, "stopping": {
        "one-off": str(cfg.repo_root), "one-work": str(cfg.repo_root)
    }}))
    mocker.patch("jailbee.lifecycle.boot_container")
    mocker.patch("jailbee.create_flow.preflight_cache_pools")
    post = mocker.patch("jailbee.cli._post_start_actions")
    result = CliRunner().invoke(app, ["resume"])
    assert result.exit_code == 0, result.output
    assert [c.args[2] for c in post.call_args_list] == ["one-off"]
    assert recorded() == {}


def test_stop_all_continues_after_a_state_write_failure(host, mocker):
    import os

    _, _, incus, _ = host
    replace = os.replace
    writes = 0

    def flaky_write(source, target):
        nonlocal writes
        writes += 1
        if writes == 3:
            raise OSError("disk temporarily unavailable")
        replace(source, target)

    mocker.patch("jailbee.host_resume.os.replace", side_effect=flaky_write)
    result = CliRunner().invoke(app, ["stop", "--all", "--yes"])
    assert result.exit_code == 1, result.output
    assert "disk temporarily unavailable" in result.output
    assert incus.stop.call_count == 3
    assert set(recorded()) == {"one-work", "two-child", "one-mount"}


@pytest.mark.parametrize("service", ["jailbee-registry-mirror", "jailbee-litellm", "jailbee-display", "jailbee-egress-proxy"])
def test_host_lifecycle_excludes_service_containers(host, service):
    _, _, incus, _ = host
    incus.list_containers.return_value = [{"name": service, "status": "Running",
        "profiles": ["default"], "config": {"boot.autostart": "true"}}]
    result = CliRunner().invoke(app, ["stop", "--all", "--yes"])
    assert result.exit_code == 0, result.output
    incus.stop.assert_not_called()
    result = CliRunner().invoke(app, ["resume"])
    assert result.exit_code == 0, result.output
    incus.exec.assert_not_called()
    incus.start.assert_not_called()


def test_host_lifecycle_is_blocked_in_restricted_remote_sessions():
    from jailbee.config.models_remote import RemoteCommandPolicy
    from jailbee.remote_ssh.router import RouteError, policy_allows

    policy = RemoteCommandPolicy(mode="full")
    assert policy_allows(["stop", "box"], policy) == "stop"
    with pytest.raises(RouteError, match="may not set --all"):
        policy_allows(["stop", "--all", "--yes"], policy)
    with pytest.raises(RouteError, match="manages the host"):
        policy_allows(["resume"], policy)
