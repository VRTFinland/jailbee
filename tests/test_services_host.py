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


def test_host_service_commands_present():
    from typer.main import get_command

    from jailbee.cli import app

    group = get_command(app).commands["service"]
    assert set(group.commands) == {"ls", "start", "stop", "restart", "refresh", "logs"}


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
        "chosen",
        [
            "jailbee",
            "--expect-version",
            f"{__version__} {container_app.code_hash()}",
            "service",
            "--repo",
            "/container/repo",
            "ls",
            "--json",
        ],
        uid=cfg.container_user.uid,
        cwd="/container/repo",
        env={"HOME": f"/home/{CONTAINER_USERNAME}"},
        init_groups=True,
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
    expected = call(
        "chosen",
        [
            "jailbee",
            "--expect-version",
            f"{__version__} {container_app.code_hash()}",
            "service",
            "--repo",
            "/container/repo",
            "restart",
            "web",
        ],
        uid=cfg.container_user.uid,
        cwd="/container/repo",
        env={"HOME": f"/home/{CONTAINER_USERNAME}"},
        init_groups=True,
    )
    assert incus.exec_interactive.call_args_list == [expected, expected]
    incus.exec_bytes.assert_not_called()
    stage.assert_called_once_with(force=True)
    sync.assert_called_once_with(cfg)


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


def refresh_setup(make_cfg, tmp_path, monkeypatch, *, network="loose", mounts=()):
    from jailbee.config.models_services import Service
    from jailbee.services import graph

    cfg = make_cfg(tmp_path)
    incus = Mock()
    monkeypatch.setattr("jailbee.lifecycle.container_repo_dir", lambda *args: "/repo")
    steps = [{"run": "setup", "network": network, "mounts": list(mounts)}]
    payload = graph.plan_payload(
        {"web": Service(run="web", prepare=steps)}, ["web"], "refresh", step_timeout=42
    )
    run = Mock(return_value=ExecResult(0, json.dumps(payload).encode(), b""))
    monkeypatch.setattr("jailbee.container_app.run_in_container", run)
    return cfg, incus, run


def test_host_refresh_elevated_runs_sequence(make_cfg, tmp_path, monkeypatch, db_engine):
    from sqlmodel import Session

    from jailbee import db
    from jailbee.db.models import BackgroundJob
    from jailbee.services import elevated

    cfg, incus, run = refresh_setup(make_cfg, tmp_path, monkeypatch)
    monkeypatch.setattr(db, "get_engine", lambda: db_engine)
    monkeypatch.setattr("jailbee.orchestration.state.state_root", lambda: tmp_path)
    monkeypatch.setattr("jailbee.orchestration.agent_watch.probe", lambda *a, **kw: Mock())
    monkeypatch.setattr(elevated, "foreign_agents", lambda *a: [])
    monkeypatch.setattr("jailbee.docker_daemon.mirror_endpoint_or_none", lambda *a: None)

    def sequence(*args, **kwargs):
        with Session(db_engine) as session:
            assert session.get(BackgroundJob, "one").op_kind == "refresh"
        assert kwargs["who"] == "requested by the user on the host"
        assert kwargs["force"] is True
        return elevated.RefreshReport((), True, "not running")

    monkeypatch.setattr(elevated, "run_refresh", sequence)
    assert host.refresh(cfg, incus, "one", ["web"], assume_yes=False, force=True) == 0
    with Session(db_engine) as session:
        assert session.get(BackgroundJob, "one") is None
    assert run.call_count == 1


def test_host_refresh_plain_uses_container_refresh(make_cfg, tmp_path, monkeypatch):
    cfg, incus, _ = refresh_setup(make_cfg, tmp_path, monkeypatch, network=None)
    run = Mock(return_value=0)
    monkeypatch.setattr(host, "run_op", run)
    assert host.refresh(cfg, incus, "one", ["web"], assume_yes=False) == 0
    run.assert_called_once_with(cfg, incus, "one", "refresh", ["web"])


def test_host_refresh_unknown_mount(make_cfg, tmp_path, monkeypatch, capsys):
    cfg, incus, _ = refresh_setup(make_cfg, tmp_path, monkeypatch, mounts=("unknown",))
    assert host.refresh(cfg, incus, "one", ["web"], assume_yes=True) == 1
    assert "optional mount 'unknown'" in capsys.readouterr().err


def test_host_refresh_foreign_agents_noninteractive(
    make_cfg, tmp_path, monkeypatch, db_engine, capsys
):
    from jailbee import db, prompting
    from jailbee.services import elevated

    cfg, incus, _ = refresh_setup(make_cfg, tmp_path, monkeypatch)
    monkeypatch.setattr(db, "get_engine", lambda: db_engine)
    monkeypatch.setattr("jailbee.orchestration.state.state_root", lambda: tmp_path)
    monkeypatch.setattr("jailbee.orchestration.agent_watch.probe", lambda *a, **kw: Mock())
    monkeypatch.setattr(elevated, "foreign_agents", lambda *a: ["manual-agent"])
    monkeypatch.setattr(prompting, "is_interactive", lambda: False)
    monkeypatch.setattr("jailbee.docker_daemon.mirror_endpoint_or_none", lambda *a: None)
    run = Mock(return_value=elevated.RefreshReport((), True, "not running"))
    monkeypatch.setattr(elevated, "run_refresh", run)
    assert host.refresh(cfg, incus, "one", ["web"], assume_yes=False, force=True) == 1
    assert "manual-agent" in capsys.readouterr().err
    assert host.refresh(cfg, incus, "one", ["web"], assume_yes=True) == 0
    assert run.call_args.kwargs["force"] is False
    monkeypatch.setattr(prompting, "is_interactive", lambda: True)
    monkeypatch.setattr(prompting, "confirm", lambda *a, **kw: False)
    assert host.refresh(cfg, incus, "one", ["web"], assume_yes=False) == 1
    assert run.call_count == 1


def test_host_refresh_busy(make_cfg, tmp_path, monkeypatch, db_engine, capsys):
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background, db

    cfg, incus, _ = refresh_setup(make_cfg, tmp_path, monkeypatch)
    monkeypatch.setattr(db, "get_engine", lambda: db_engine)
    monkeypatch.setattr("jailbee.orchestration.state.state_root", lambda: tmp_path)
    with Session(db_engine) as session:
        background.start_job(
            session,
            container_name="one",
            container_prefix=cfg.container_prefix,
            branch=None,
            pid=1,
            log_path="",
            now=datetime.now(UTC),
            op_kind="autostart",
        )
    assert host.refresh(cfg, incus, "one", ["web"], assume_yes=True) == 1
    assert "autostart running" in capsys.readouterr().err


def test_cli_refresh_delegates_flags(make_cfg, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from jailbee import cli

    cfg = make_cfg(tmp_path)
    incus = Mock()
    monkeypatch.setattr(cli, "_load_or_exit", lambda _: cfg)
    monkeypatch.setattr(cli, "_resolve_existing", lambda *a, **kw: (incus, "one"))
    run = Mock(return_value=0)
    monkeypatch.setattr(host, "refresh", run, raising=False)
    result = CliRunner().invoke(cli.app, ["service", "refresh", "one", "web", "--yes", "--force"])
    assert result.exit_code == 0, result.output
    run.assert_called_once_with(cfg, incus, "one", ["web"], assume_yes=True, force=True)


def test_host_refresh_fails_closed_on_database_failure(make_cfg, tmp_path, monkeypatch, capsys):
    from jailbee import db

    cfg, incus, _ = refresh_setup(make_cfg, tmp_path, monkeypatch)
    monkeypatch.setattr(db, "get_engine", lambda: None)
    assert host.refresh(cfg, incus, "one", ["web"], assume_yes=True) == 1
    assert "database unavailable" in capsys.readouterr().err


def test_host_refresh_releases_claim_after_refresh_error(
    make_cfg, tmp_path, monkeypatch, db_engine, capsys
):
    from sqlmodel import Session

    from jailbee import db
    from jailbee.db.models import BackgroundJob
    from jailbee.services import elevated

    cfg, incus, _ = refresh_setup(make_cfg, tmp_path, monkeypatch)
    monkeypatch.setattr(db, "get_engine", lambda: db_engine)
    monkeypatch.setattr("jailbee.orchestration.state.state_root", lambda: tmp_path)
    monkeypatch.setattr("jailbee.orchestration.agent_watch.probe", lambda *a, **kw: Mock())
    monkeypatch.setattr(elevated, "foreign_agents", lambda *a: [])
    monkeypatch.setattr("jailbee.docker_daemon.mirror_endpoint_or_none", lambda *a: None)
    monkeypatch.setattr(
        elevated, "run_refresh", Mock(side_effect=elevated.RefreshError("not idle"))
    )
    assert host.refresh(cfg, incus, "one", ["web"], assume_yes=True) == 1
    assert "not idle" in capsys.readouterr().err
    with Session(db_engine) as session:
        assert session.get(BackgroundJob, "one") is None


def test_host_refresh_rejects_oversized_plan(make_cfg, tmp_path, monkeypatch, capsys):
    cfg, incus, run = refresh_setup(make_cfg, tmp_path, monkeypatch)
    run.return_value = ExecResult(0, b" " * (256 * 1024 + 1), b"")
    assert host.refresh(cfg, incus, "one", ["web"], assume_yes=True) == 1
    assert "invalid refresh plan" in capsys.readouterr().err
