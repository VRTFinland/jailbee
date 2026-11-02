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
