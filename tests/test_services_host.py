import json
from unittest.mock import Mock

from jailbee.incus import ExecResult
from jailbee.services import host


def test_run_op_builds_container_argv(make_cfg, tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    incus = Mock()
    monkeypatch.setattr("jailbee.lifecycle.container_repo_dir", lambda *args: "/repo")
    run = Mock(return_value=0)
    monkeypatch.setattr("jailbee.container_app.run_in_container_interactive", run)
    assert host.run_op(cfg, incus, "one", "restart", ["web"]) == 0
    run.assert_called_once_with(
        cfg,
        incus,
        "one",
        ["service", "--repo", "/repo", "restart", "web"],
        uid=cfg.container_user.uid,
        cwd="/repo",
    )


def test_elevated_refresh_refused(make_cfg, tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr("jailbee.lifecycle.container_repo_dir", lambda *args: "/repo")
    payload = {
        "services": [
            {"name": "web", "will_prepare": True, "elevation": {"network": "loose", "mounts": []}}
        ]
    }
    run = Mock(return_value=ExecResult(0, json.dumps(payload).encode(), b""))
    monkeypatch.setattr("jailbee.container_app.run_in_container", run)
    assert "web: network loose" in host.elevated_refresh_refusal(cfg, Mock(), "one", ["web"])
    assert run.call_args.args[3][:3] == ["service", "--repo", "/repo"]


def test_host_service_commands_present():
    from typer.main import get_command

    from jailbee.cli import app

    group = get_command(app).commands["service"]
    assert set(group.commands) == {"ls", "start", "stop", "restart", "refresh", "logs"}


def test_cli_refresh_refuses_before_interactive(make_cfg, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from jailbee import cli

    cfg = make_cfg(tmp_path)
    incus = Mock()
    monkeypatch.setattr(cli, "_load_or_exit", lambda _: cfg)
    monkeypatch.setattr(cli, "_resolve_existing", lambda *args, **kwargs: (incus, "one"))
    monkeypatch.setattr(host, "elevated_refresh_refusal", lambda *args: "not available yet")
    run = Mock()
    monkeypatch.setattr(host, "run_op", run)
    result = CliRunner().invoke(cli.app, ["service", "refresh", "one", "web"])
    assert result.exit_code == 1
    assert "not available yet" in result.output
    run.assert_not_called()


def test_cli_omitted_service_uses_container_names(make_cfg, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from jailbee import cli, prompting

    cfg = make_cfg(tmp_path)
    incus = Mock()
    monkeypatch.setattr(cli, "_load_or_exit", lambda _: cfg)
    resolve = Mock(return_value=(incus, "one"))
    monkeypatch.setattr(cli, "_resolve_existing", resolve)
    monkeypatch.setattr(host, "service_names", lambda *args: ["web", "db"])
    monkeypatch.setattr(prompting, "is_interactive", lambda: True)
    monkeypatch.setattr(prompting, "_select", lambda *args, **kwargs: "web")
    run = Mock(return_value=0)
    monkeypatch.setattr(host, "run_op", run)
    result = CliRunner().invoke(cli.app, ["service", "start"])
    assert result.exit_code == 0, result.output
    resolve.assert_called_once_with(cfg, None, always_prompt=False)
    run.assert_called_once_with(cfg, incus, "one", "start", ["web"], extra=())


def test_service_names_uses_container_definitions_and_context(make_cfg, tmp_path, monkeypatch):
    from jailbee import __version__, container_app
    from jailbee.config import CONTAINER_USERNAME
    from jailbee.config.models_services import Service

    cfg = make_cfg(tmp_path).model_copy(update={"services": {"host-only": Service(run="true")}})
    incus = Mock()
    incus.exec_bytes.return_value = ExecResult(0, b'[{"name":"container-only"}]', b"")
    monkeypatch.setattr("jailbee.lifecycle.container_repo_dir", lambda *args: "/container/repo")
    assert host.service_names(cfg, incus, "chosen") == ["container-only"]
    incus.exec_bytes.assert_called_once_with(
        "chosen", ["jailbee", "--expect-version", f"{__version__} {container_app.code_hash()}",
                   "service", "--repo", "/container/repo", "ls", "--json"],
        uid=cfg.container_user.uid, cwd="/container/repo",
        env={"HOME": f"/home/{CONTAINER_USERNAME}"}, init_groups=True,
    )


def test_run_op_interactive_skew_preserves_context(make_cfg, tmp_path, monkeypatch):
    from unittest.mock import call
    from jailbee import __version__, container_app
    from jailbee.config import CONTAINER_USERNAME

    cfg = make_cfg(tmp_path)
    incus = Mock()
    incus.exec_interactive.side_effect = [86, 0]
    monkeypatch.setattr("jailbee.lifecycle.container_repo_dir", lambda *args: "/container/repo")
    stage = Mock()
    sync = Mock()
    monkeypatch.setattr(container_app, "stage", stage)
    monkeypatch.setattr("jailbee.agent_skills.sync_agent_skills", sync)
    assert host.run_op(cfg, incus, "chosen", "restart", ["web"]) == 0
    expected = call("chosen", ["jailbee", "--expect-version",
        f"{__version__} {container_app.code_hash()}", "service", "--repo", "/container/repo",
        "restart", "web"], uid=cfg.container_user.uid, cwd="/container/repo",
        env={"HOME": f"/home/{CONTAINER_USERNAME}"}, init_groups=True)
    assert incus.exec_interactive.call_args_list == [expected, expected]
    incus.exec_bytes.assert_not_called()
    stage.assert_called_once_with(force=True)
    sync.assert_called_once_with(cfg)


def test_refresh_refusal_only_prepared_mounts(make_cfg, tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    payload = {"services": [
        {"name": "web", "will_prepare": True, "elevation": {"network": None, "mounts": ["creds"]}},
        {"name": "db", "will_prepare": False, "elevation": {"network": "loose", "mounts": ["other"]}},
    ]}
    monkeypatch.setattr(host, "_metadata", lambda *args: payload)
    refusal = host.elevated_refresh_refusal(cfg, Mock(), "one", ["web"])
    assert "web: mounts creds" in refusal
    assert "db" not in refusal and "other" not in refusal
    payload["services"][0]["will_prepare"] = False
    assert host.elevated_refresh_refusal(cfg, Mock(), "one", ["web"]) is None


def test_real_container_and_service_pickers(make_cfg, tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from jailbee import cli, prompting, tui
    from jailbee.lifecycle import ContainerInfo

    cfg = make_cfg(tmp_path)
    incus = Mock()
    incus.exec_bytes.return_value = ExecResult(0, b'[{"name":"web"},{"name":"db"}]', b"")
    incus.exec_interactive.return_value = 0
    monkeypatch.setattr(cli, "_load_or_exit", lambda _: cfg)
    monkeypatch.setattr("jailbee.incus.Incus", lambda: incus)
    containers = [ContainerInfo(n, "Running", "strict", None, None) for n in ("first", "second")]
    monkeypatch.setattr("jailbee.lifecycle.list_containers", lambda *args, **kwargs: containers)
    monkeypatch.setattr("jailbee.lifecycle.container_repo_dir", lambda *args: "/repo")
    monkeypatch.setattr(prompting, "is_interactive", lambda: True)
    pick = Mock(return_value="second")
    select = Mock(return_value="db")
    monkeypatch.setattr(tui, "pick_container", pick)
    monkeypatch.setattr(prompting, "_select", select)
    result = CliRunner().invoke(cli.app, ["service", "start"])
    assert result.exit_code == 0, result.output
    pick.assert_called_once_with(containers)
    assert select.call_args.args[0] == "service"
    assert [option.value for option in select.call_args.args[1]] == ["web", "db"]
    assert incus.exec_bytes.call_args.args[0] == "second"
    assert incus.exec_interactive.call_args.args[0] == "second"
    assert incus.exec_interactive.call_args.args[1][-2:] == ["start", "db"]


def test_stop_single_container_picker_cancel_and_noninteractive(make_cfg, tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from jailbee import cli, prompting, tui
    from jailbee.lifecycle import ContainerInfo

    cfg = make_cfg(tmp_path)
    incus = Mock()
    monkeypatch.setattr(cli, "_load_or_exit", lambda _: cfg)
    monkeypatch.setattr("jailbee.incus.Incus", lambda: incus)
    containers = [ContainerInfo("only", "Running", "strict", None, None)]
    monkeypatch.setattr("jailbee.lifecycle.list_containers", lambda *args, **kwargs: containers)
    monkeypatch.setattr("jailbee.lifecycle.container_repo_dir", lambda *args: "/repo")
    incus.exec_bytes.return_value = ExecResult(0, b'[{"name":"web"}]', b"")
    incus.exec_interactive.return_value = 0
    monkeypatch.setattr(prompting, "is_interactive", lambda: True)
    pick = Mock(return_value="only")
    monkeypatch.setattr(tui, "pick_container", pick)
    select = Mock(return_value="web")
    monkeypatch.setattr(prompting, "_select", select)
    result = CliRunner().invoke(cli.app, ["service", "stop"])
    assert result.exit_code == 0, result.output
    pick.assert_called_once_with(containers)
    select.assert_called_once()
    incus.exec_bytes.reset_mock()
    incus.exec_interactive.reset_mock()
    pick.return_value = None
    result = CliRunner().invoke(cli.app, ["service", "stop"])
    assert result.exit_code == 1
    incus.exec_bytes.assert_not_called()
    incus.exec_interactive.assert_not_called()
    pick.reset_mock()
    monkeypatch.setattr(prompting, "is_interactive", lambda: False)
    result = CliRunner().invoke(cli.app, ["service", "stop"])
    assert result.exit_code == 2
    pick.assert_not_called()
    incus.exec_bytes.assert_not_called()
    incus.exec_interactive.assert_not_called()
