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
    assert set(group.commands) == {"ls", "start", "stop", "restart", "refresh", "logs", "wait"}


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
    incus.config_get_checked.return_value = None
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


def test_host_child_refresh_registers_before_resume(make_cfg, tmp_path, monkeypatch, db_engine):
    from jailbee import db
    from jailbee.orchestration import labels, watch
    from jailbee.services import elevated

    cfg, incus, run = refresh_setup(make_cfg, tmp_path, monkeypatch)
    values = {labels.SPAWNED_BY: "parent"}
    incus.config_get_checked.side_effect = lambda _, key: values.get(key)
    incus.config_set.side_effect = lambda _, key, value: values.__setitem__(key, value)
    incus.list_containers.return_value = [{"name": "one", "profiles": ["jb-net-strict"]}]
    incus.config_show.return_value = "devices: {}\n"
    monkeypatch.setattr("jailbee.lifecycle.current_network_mode", lambda *a: "strict")
    monkeypatch.setattr(db, "get_engine", lambda: db_engine)
    monkeypatch.setattr("jailbee.orchestration.state.state_root", lambda: tmp_path)
    monkeypatch.setattr("jailbee.orchestration.agent_watch.probe", lambda *a, **kw: Mock())
    monkeypatch.setattr(elevated, "foreign_agents", lambda *a: [])
    monkeypatch.setattr("jailbee.docker_daemon.mirror_endpoint_or_none", lambda *a: None)
    monkeypatch.setattr("jailbee.autostart.run_stage", lambda *a, **kw: None)
    monkeypatch.setattr(
        elevated,
        "pause_agent",
        lambda *a, **kw: elevated.PausedAgent(
            "claude", "11111111-1111-1111-1111-111111111111", None
        ),
    )
    run.side_effect = [run.return_value, ExecResult(0, b"", b"")]
    events = []
    monkeypatch.setattr(
        watch, "register", lambda root, owner, entry: events.append(("watch", owner, entry))
    )
    resume = Mock(side_effect=lambda *a, **kw: events.append(("resume", a[4])))
    monkeypatch.setattr(elevated, "resume_agent", resume)
    assert host.refresh(cfg, incus, "one", ["web"], assume_yes=True) == 0
    assert [event[0] for event in events] == ["watch", "resume"]
    assert events[0][1] == "parent"
    assert events[0][2].agent_only and events[0][2].supervise
    assert "parent" in events[1][1]


def test_host_child_watch_failure_withholds_resume(
    make_cfg, tmp_path, monkeypatch, db_engine, capsys
):
    from jailbee import db
    from jailbee.orchestration import labels, watch
    from jailbee.services import elevated

    cfg, incus, run = refresh_setup(make_cfg, tmp_path, monkeypatch)
    values = {labels.SPAWNED_BY: "parent"}
    incus.config_get_checked.side_effect = lambda _, key: values.get(key)
    incus.config_set.side_effect = lambda _, key, value: values.__setitem__(key, value)
    incus.list_containers.return_value = [{"name": "one", "profiles": ["jb-net-strict"]}]
    incus.config_show.return_value = "devices: {}\n"
    monkeypatch.setattr("jailbee.lifecycle.current_network_mode", lambda *a: "strict")
    monkeypatch.setattr(db, "get_engine", lambda: db_engine)
    monkeypatch.setattr("jailbee.orchestration.state.state_root", lambda: tmp_path)
    monkeypatch.setattr("jailbee.orchestration.agent_watch.probe", lambda *a, **kw: Mock())
    monkeypatch.setattr(elevated, "foreign_agents", lambda *a: [])
    monkeypatch.setattr("jailbee.docker_daemon.mirror_endpoint_or_none", lambda *a: None)
    monkeypatch.setattr("jailbee.autostart.run_stage", lambda *a, **kw: None)
    monkeypatch.setattr(
        elevated,
        "pause_agent",
        lambda *a, **kw: elevated.PausedAgent(
            "claude", "11111111-1111-1111-1111-111111111111", None
        ),
    )
    run.side_effect = [run.return_value, ExecResult(0, b"", b"")]
    monkeypatch.setattr(watch, "register", Mock(side_effect=RuntimeError("watch failed")))
    resume = Mock()
    monkeypatch.setattr(elevated, "resume_agent", resume)
    assert host.refresh(cfg, incus, "one", ["web"], assume_yes=True) == 1
    text = capsys.readouterr().out
    assert "resume withheld: could not register agent supervision" in text
    assert "jailbee inbox send restart --child one --agent-only --resume" in text
    resume.assert_not_called()


def test_host_refresh_release_failure_returns_error(
    make_cfg, tmp_path, monkeypatch, db_engine, capsys
):
    from jailbee import background, db
    from jailbee.services import elevated

    cfg, incus, _ = refresh_setup(make_cfg, tmp_path, monkeypatch)
    monkeypatch.setattr(db, "get_engine", lambda: db_engine)
    monkeypatch.setattr("jailbee.orchestration.state.state_root", lambda: tmp_path)
    monkeypatch.setattr("jailbee.orchestration.agent_watch.probe", lambda *a, **kw: Mock())
    monkeypatch.setattr(elevated, "foreign_agents", lambda *a: [])
    monkeypatch.setattr("jailbee.docker_daemon.mirror_endpoint_or_none", lambda *a: None)
    monkeypatch.setattr(
        elevated,
        "run_refresh",
        lambda *a, **kw: elevated.RefreshReport(
            (elevated.ServiceOutcome("web", "[red]failed[/red]"),), False, "not running"
        ),
    )
    release = Mock(side_effect=RuntimeError("database failed"))
    monkeypatch.setattr(background, "release_job", release)
    assert host.refresh(cfg, incus, "one", ["web"], assume_yes=True) == 1
    captured = capsys.readouterr()
    assert "[red]failed[/red]" in captured.out
    assert "database failed" in captured.err
    assert release.call_args.args[1:] == ("one", __import__("os").getpid())


def test_host_claim_waits_for_actual_stop_handler(make_cfg, tmp_path, monkeypatch):
    import threading
    from datetime import UTC, datetime

    from sqlalchemy import create_engine
    from sqlmodel import SQLModel

    from jailbee import background, db
    from jailbee.global_config import GlobalConfig
    from jailbee.orchestration import actions, labels
    from jailbee.orchestration.protocol import StopManifest
    from jailbee.services import elevated

    cfg, incus, _ = refresh_setup(make_cfg, tmp_path, monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr("jailbee.orchestration.state.state_root", lambda: tmp_path)
    monkeypatch.setattr("jailbee.orchestration.agent_watch.probe", lambda *a, **k: Mock())
    monkeypatch.setattr(elevated, "foreign_agents", lambda *a: [])
    monkeypatch.setattr("jailbee.docker_daemon.mirror_endpoint_or_none", lambda *a: None)
    monkeypatch.setattr(
        elevated, "run_refresh", lambda *a, **k: elevated.RefreshReport((), True, "none")
    )
    orch = actions.Node("orch", "orch", True, {})
    child = actions.Node("one", "one", True, {labels.SPAWNED_BY: "orch"})
    ctx = actions.ActionContext(
        cfg,
        GlobalConfig(),
        incus,
        tmp_path,
        orch,
        {orch.full: orch, child.full: child},
        datetime.now(UTC),
    )
    entered, release, claimed = threading.Event(), threading.Event(), threading.Event()

    def head(*args):
        entered.set()
        assert release.wait(5)
        return None

    monkeypatch.setattr(actions, "_child_head", head)
    monkeypatch.setattr(actions.lifecycle, "container_repo_dir", lambda *a: "/repo")
    monkeypatch.setattr(actions.sync, "_container_dirty_quiet", lambda *a, **k: False)
    monkeypatch.setattr("jailbee.stopping.stop_container", lambda *a, **k: None)
    original = background.claim_job

    def claim(*a, **k):
        claimed.set()
        return original(*a, **k)

    monkeypatch.setattr(background, "claim_job", claim)
    results = []
    stop = threading.Thread(
        target=lambda: results.append(
            actions.handle_stop(ctx, StopManifest(version=1, id="s1", type="stop", child="one"))
        )
    )
    refresh = threading.Thread(
        target=lambda: results.append(host.refresh(cfg, incus, "one", ["web"], assume_yes=True))
    )
    stop.start()
    assert entered.wait(5)
    refresh.start()
    try:
        assert not claimed.wait(0.5)
    finally:
        release.set()
        stop.join(5)
        refresh.join(5)
    assert not stop.is_alive() and not refresh.is_alive()
    assert claimed.is_set() and 0 in results


def test_wait_host_delegates_options(make_cfg, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from jailbee import cli

    cfg = make_cfg(tmp_path)
    incus = Mock()
    monkeypatch.setattr(cli, "_load_or_exit", lambda _: cfg)
    monkeypatch.setattr(cli, "_resolve_existing", lambda *args, **kwargs: (incus, "one"))
    run = Mock(return_value=0)
    monkeypatch.setattr(host, "run_op", run)
    result = CliRunner().invoke(
        cli.app,
        [
            "service",
            "wait",
            "one",
            "web",
            "--until",
            "unhealthy",
            "--timeout",
            "60",
            "--interval",
            "10",
        ],
    )
    assert result.exit_code == 0, result.output
    run.assert_called_once_with(
        cfg,
        incus,
        "one",
        "wait",
        ["web"],
        extra=["--until", "unhealthy", "--timeout", "60", "--interval", "10"],
    )


def test_host_logs_passes_query_options(monkeypatch):
    from typer.testing import CliRunner

    from jailbee import cli

    run = Mock()
    monkeypatch.setattr(cli, "_service_command", run)
    result = CliRunner().invoke(
        cli.app,
        [
            "service",
            "logs",
            "one",
            "web",
            "--grep",
            "a",
            "--grep",
            "b",
            "--filter",
            "errors",
            "-A",
            "4",
            "-B",
            "3",
            "-C",
            "2",
            "--wait",
            "ready",
            "--timeout",
            "12",
            "--follow",
        ],
    )
    assert result.exit_code == 0, result.output
    assert run.call_args.args == (
        "logs",
        "one",
        ["web"],
        None,
        [
            "-n",
            "30",
            "--follow",
            "--grep",
            "a",
            "--grep",
            "b",
            "--filter",
            "errors",
            "-B",
            "3",
            "-A",
            "4",
            "-C",
            "2",
            "--wait",
            "ready",
            "--timeout",
            "12",
        ],
    )


def test_wait_run_op_argv(make_cfg, tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    incus = Mock()
    monkeypatch.setattr("jailbee.lifecycle.container_repo_dir", lambda *args: "/repo")
    run = Mock(return_value=0)
    monkeypatch.setattr("jailbee.container_app.run_in_container_interactive", run)
    extra = ["--until", "unhealthy", "--timeout", "60", "--interval", "10"]
    assert host.run_op(cfg, incus, "one", "wait", ["svc"], extra=extra) == 0
    assert run.call_args.args[3] == ["service", "--repo", "/repo", "wait", "svc", *extra]
