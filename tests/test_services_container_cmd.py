import json
from pathlib import Path

from typer.testing import CliRunner

from jailbee.container_cli import container_app


def test_service_help_without_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(container_app, ["service", "--help"])
    assert result.exit_code == 0
    for command in ("ls", "start", "stop", "restart", "refresh", "logs", "plan"):
        assert command in result.stdout


def test_plan_from_explicit_repo_without_git(tmp_path: Path, monkeypatch) -> None:
    def forbid(*args, **kwargs):
        raise AssertionError("git subprocess called")

    monkeypatch.setattr("jailbee.config.loader.detect_upstream_remote", forbid)
    monkeypatch.setattr("jailbee.config.loader.detect_default_branch", forbid)
    config = tmp_path / ".jailbee" / "config.yaml"
    config.parent.mkdir()
    config.write_text("container_prefix: example\nservices:\n  db:\n    run: sleep 10\n")
    result = CliRunner().invoke(
        container_app,
        ["service", "--repo", str(tmp_path), "plan", "db", "--op", "refresh", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["order"] == ["db"]


def test_refusal_and_unknown_never_run_commands(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / ".jailbee" / "config.yaml"
    config.parent.mkdir()
    config.write_text(
        "container_prefix: myrepo\nservices:\n  backend:\n    run: sleep 10\n"
        "    prepare:\n      - run: echo test\n        network: loose\n"
    )
    monkeypatch.setattr(
        "jailbee.services.container_cmd.socket.gethostname", lambda: "myrepo-feat-x"
    )

    def forbid(*args, **kwargs):
        raise AssertionError("unexpected mutation")

    monkeypatch.setattr("jailbee.services.local_proc.LocalProc.run_shell", forbid)
    runner = CliRunner()
    result = runner.invoke(
        container_app, ["service", "--repo", str(tmp_path), "refresh", "backend"]
    )
    assert result.exit_code == 3
    assert "jailbee inbox send refresh --child feat-x --services backend --wait" in result.output
    assert "jb service refresh feat-x backend" in result.output
    result = runner.invoke(
        container_app, ["service", "--repo", str(tmp_path), "refresh", "backend", "missing"]
    )
    assert result.exit_code == 2
    assert "missing" in result.output


def test_default_discovery_from_git_file(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".git").write_text("gitdir: somewhere")
    config = tmp_path / ".jailbee" / "config.yaml"
    config.parent.mkdir()
    config.write_text("container_prefix: myrepo\nservices:\n  db:\n    run: sleep 10\n")
    nested = tmp_path / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    result = CliRunner().invoke(container_app, ["service", "plan", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["order"] == ["db"]


def test_follow_repeated_lines_and_burst(tmp_path: Path, monkeypatch) -> None:
    from jailbee.services.runtime import ServiceRuntime

    config = tmp_path / ".jailbee" / "config.yaml"
    config.parent.mkdir()
    config.write_text("container_prefix: repo\nservices:\n  db:\n    run: sleep 1\n")

    def follow(self, name, lines):
        yield "same\n"
        yield "same\nnew\n"
        raise KeyboardInterrupt

    monkeypatch.setattr(ServiceRuntime, "follow_logs", follow)
    result = CliRunner().invoke(
        container_app, ["service", "--repo", str(tmp_path), "logs", "db", "-n", "1", "--follow"]
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == "same\nsame\nnew\n"


def test_logs_capture_error_exits_nonzero(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    config = tmp_path / ".jailbee" / "config.yaml"
    config.parent.mkdir()
    config.write_text("container_prefix: repo\nservices:\n  db:\n    run: sleep 1\n")
    monkeypatch.setattr(
        "jailbee.services.local_proc.LocalProc.tmux",
        lambda self, args: subprocess.CompletedProcess(args, 1, "", "pane missing"),
    )
    result = CliRunner().invoke(container_app, ["service", "--repo", str(tmp_path), "logs", "db"])
    assert result.exit_code == 1
    assert "pane missing" in result.output


def test_missing_config_and_unknown_plan(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(container_app, ["service", "--repo", str(tmp_path), "ls"])
    assert result.exit_code == 1
    assert "no .jailbee/config.yaml" in result.output
    config = tmp_path / ".jailbee" / "config.yaml"
    config.parent.mkdir()
    config.write_text("container_prefix: repo\nservices:\n  db:\n    run: sleep 1\n")
    result = runner.invoke(
        container_app, ["service", "--repo", str(tmp_path), "plan", "missing", "--op", "refresh"]
    )
    assert result.exit_code == 2
    assert "db" in result.output


def test_ls_json_and_default(tmp_path: Path, monkeypatch) -> None:
    from jailbee.services.runtime import ServiceRuntime, ServiceStatus

    config = tmp_path / ".jailbee" / "config.yaml"
    config.parent.mkdir()
    config.write_text("container_prefix: repo\nservices:\n  db:\n    run: sleep 1\n")
    monkeypatch.setattr(
        ServiceRuntime,
        "status",
        lambda self: [ServiceStatus("db", "ready", "svc-db", "today", "ok")],
    )
    runner = CliRunner()
    base = ["service", "--repo", str(tmp_path)]
    result = runner.invoke(container_app, [*base, "ls", "--json"])
    assert json.loads(result.stdout) == [
        {
            "name": "db",
            "state": "ready",
            "window": "svc-db",
            "since": "today",
            "detail": "ok",
            "launch": None,
            "status": None,
        }
    ]
    result = runner.invoke(container_app, base)
    assert result.exit_code == 0 and "db" in result.stdout and "ready" in result.stdout
    result = runner.invoke(container_app, [*base, "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["name"] == "db"


def test_missing_names_noninteractive(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / ".jailbee" / "config.yaml"
    config.parent.mkdir()
    config.write_text(
        "container_prefix: repo\nservices:\n  db:\n    run: sleep 1\n  web:\n    run: sleep 1\n"
    )
    monkeypatch.setattr("jailbee.prompting.is_interactive", lambda: False)
    result = CliRunner().invoke(container_app, ["service", "--repo", str(tmp_path), "restart"])
    assert result.exit_code == 2
    assert "db" in result.output and "web" in result.output


def test_plan_json_matches_graph(tmp_path: Path) -> None:
    from jailbee.config.repo_layer import load_repo_layer
    from jailbee.services.graph import plan_payload

    config = tmp_path / ".jailbee" / "config.yaml"
    config.parent.mkdir()
    config.write_text("container_prefix: repo\nservices:\n  db:\n    run: sleep 1\n")
    cfg = load_repo_layer(config)
    result = CliRunner().invoke(
        container_app,
        ["service", "--repo", str(tmp_path), "plan", "db", "--op", "refresh", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == plan_payload(
        cfg.services, ["db"], "refresh", step_timeout=cfg.autostart.step_timeout
    )


def test_service_shared_name() -> None:
    from jailbee.cli_context import SHARED_COMMANDS

    assert "service" in SHARED_COMMANDS


def test_child_help_ignores_missing_and_invalid_config(tmp_path: Path) -> None:
    runner = CliRunner()
    args = ["service", "--repo", str(tmp_path), "logs", "--help"]
    assert runner.invoke(container_app, args).exit_code == 0
    config = tmp_path / ".jailbee" / "config.yaml"
    config.parent.mkdir()
    config.write_text("services: [invalid")
    assert runner.invoke(container_app, args).exit_code == 0


def test_wait_outputs_match_and_rejects_unknown(tmp_path, monkeypatch):
    from jailbee.services.runtime import ServiceRuntime, ServiceStatus

    config = tmp_path / ".jailbee" / "config.yaml"
    config.parent.mkdir()
    config.write_text("container_prefix: repo\nservices:\n  db:\n    run: sleep 1\n")
    monkeypatch.setattr(
        ServiceRuntime,
        "wait",
        lambda self, names, until, **kw: (0, ServiceStatus("db", "ready", None, None, "ok")),
    )
    runner = CliRunner()
    base = ["service", "--repo", str(tmp_path), "wait"]
    result = runner.invoke(container_app, [*base, "db", "--until", "ready", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["state"] == "ready"
    result = runner.invoke(container_app, [*base, "db", "--until", "ready"])
    assert result.exit_code == 0 and result.stdout == "db ready ok\n"
    result = runner.invoke(container_app, [*base, "missing", "--until", "ready"])
    assert result.exit_code == 2


def test_runtime_separator_and_validation(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from jailbee.services.runtime import RunReport, ServiceRuntime

    config = tmp_path / ".jailbee" / "config.yaml"
    config.parent.mkdir()
    config.write_text(
        "container_prefix: repo\nservices:\n  backend:\n    run: sleep 1\n"
        "    args: true\n  other:\n    run: sleep 1\n"
    )
    restart = Mock(return_value=RunReport(True, (), None, ""))
    monkeypatch.setattr(ServiceRuntime, "restart", restart)
    base = ["service", "--repo", str(tmp_path), "restart"]
    result = CliRunner().invoke(container_app, [*base, "backend", "--", "-P", "flyway"])
    assert result.exit_code == 0, result.output
    assert restart.call_args.args[0] == ["backend"]
    assert restart.call_args.kwargs["launch"].args == ("-P", "flyway")
    result = CliRunner().invoke(container_app, [*base, "backend", "other", "--", "-x"])
    assert result.exit_code == 2
    assert "runtime options apply to exactly one service" in result.output
    for value in ("secret-invalid", "BAD-KEY=secret-value", "--"):
        result = CliRunner().invoke(container_app, [*base, "backend", "--env", value])
        assert result.exit_code == 2
        assert "secret" not in result.output


def test_args_require_enabled_service(tmp_path):
    config = tmp_path / ".jailbee" / "config.yaml"
    config.parent.mkdir()
    config.write_text("container_prefix: repo\nservices:\n  backend:\n    run: sleep 1\n")
    result = CliRunner().invoke(
        container_app, ["service", "--repo", str(tmp_path), "start", "backend", "--", "-x"]
    )
    assert result.exit_code == 2
    assert "services.backend.args" in result.output


def test_cli_redacts_runtime_env_in_status_and_failure(tmp_path, monkeypatch):
    from jailbee.services.launch_store import LaunchRequest
    from tests.test_services_runtime import runtime
    rt, proc = runtime(tmp_path, {"web": {"run": "web"}})
    monkeypatch.setattr("jailbee.services.container_cmd._runtime", lambda _: rt)
    secret = "secret-runtime-value"
    assert rt.start(["web"], launch=LaunchRequest(None, {"TOKEN": secret}, False, False)).ok
    runner = CliRunner()
    for suffix in (["ls"], ["ls", "--json"]):
        result = runner.invoke(container_app, ["service", *suffix])
        assert result.exit_code == 0, result.output
        assert "TOKEN" in result.output
        assert secret not in result.output
    proc.immediate_exit.add("svc-web")
    original = proc.tmux
    def capture(args):
        if args[0] == "capture-pane":
            import subprocess
            return subprocess.CompletedProcess(args, 0, secret, "")
        return original(args)
    proc.tmux = capture
    result = runner.invoke(container_app, ["service", "restart", "web", "--env", f"TOKEN={secret}"])
    assert result.exit_code == 1
    assert "***" in result.output
    assert secret not in result.output
