from pathlib import Path
from unittest.mock import Mock

import pytest

from jailbee.config import Config
from jailbee.config.models_services import Service, ServicePrepareStep
from jailbee.services.refresh import elevation_refusal, run_refresh
from jailbee.services.runtime import RunReport, ServiceRuntime


def test_mixed_refresh_refuses_before_lock_or_commands(tmp_path: Path) -> None:
    cfg = Config(
        services={
            "safe": Service(run="true", prepare=[ServicePrepareStep(run="true")]),
            "backend": Service(
                run="true", prepare=[ServicePrepareStep(run="true", network="loose")]
            ),
        }
    )
    rt = ServiceRuntime(cfg, tmp_path, Mock(), tmp_path / "state")
    message = elevation_refusal(
        cfg.model_copy(update={"container_prefix": "myrepo"}), ["safe", "backend"], "myrepo-feat-x"
    )
    assert (
        message
        and "jailbee-inbox send refresh --child feat-x --services safe backend --wait" in message
    )
    assert "jb service refresh feat-x safe backend" in message
    with pytest.raises(ValueError, match="elevated"):
        run_refresh(rt, cfg, ["safe", "backend"])
    assert not rt.state_dir.exists()
    rt.proc.run_shell.assert_not_called()


def test_refresh_prepares_before_restart(tmp_path: Path) -> None:
    cfg = Config(services={"db": Service(run="true", prepare=[ServicePrepareStep(run="migrate")])})
    proc = Mock()
    proc.run_shell.return_value = 0
    rt = ServiceRuntime(cfg, tmp_path, proc, tmp_path / "state")
    rt._restart_unlocked = Mock(return_value=RunReport(True, ("db",), None, ""))
    assert run_refresh(rt, cfg, ["db"]).ok
    proc.run_shell.assert_called_once_with(
        "migrate", cwd=str(tmp_path), env={}, timeout=cfg.autostart.step_timeout, capture=False
    )
    rt._restart_unlocked.assert_called_once_with(["db"])
    proc.run_shell.return_value = 1
    rt._restart_unlocked.reset_mock()
    assert not run_refresh(rt, cfg, ["db"]).ok
    rt._restart_unlocked.assert_not_called()


def test_continue_on_error_runs_remaining_steps(tmp_path: Path) -> None:
    cfg = Config(
        services={
            "db": Service(
                run="true",
                prepare=[
                    ServicePrepareStep(run="first", continue_on_error=True),
                    ServicePrepareStep(run="second"),
                ],
            )
        }
    )
    proc = Mock()
    proc.run_shell.side_effect = [1, 0]
    rt = ServiceRuntime(cfg, tmp_path, proc, tmp_path / "state")
    rt._restart_unlocked = Mock(return_value=RunReport(True, ("db",), None, ""))
    assert run_refresh(rt, cfg, ["db"]).ok
    assert [call.args[0] for call in proc.run_shell.call_args_list] == ["first", "second"]
    rt._restart_unlocked.assert_called_once_with(["db"])


def test_unknown_name_does_not_mutate(tmp_path: Path) -> None:
    cfg = Config(services={"db": Service(run="true", prepare=[ServicePrepareStep(run="first")])})
    proc = Mock()
    rt = ServiceRuntime(cfg, tmp_path, proc, tmp_path / "state")
    with pytest.raises(ValueError, match="Unknown"):
        run_refresh(rt, cfg, ["db", "missing"])
    assert not rt.state_dir.exists()
    proc.run_shell.assert_not_called()


def test_refresh_transaction_cascades_under_one_lock(tmp_path: Path) -> None:
    from contextlib import contextmanager

    from jailbee.config.models_services import ServiceDependency
    from tests.conftest import make_config

    services = {
        "db": Service(run="db", prepare=[ServicePrepareStep(run="prepare db")]),
        "api": Service(
            run="api",
            prepare=[ServicePrepareStep(run="prepare api")],
            depends_on={"db": ServiceDependency(restart=True)},
        ),
        "web": Service(
            run="web",
            prepare=[ServicePrepareStep(run="prepare web")],
            depends_on={"api": ServiceDependency(restart=True)},
        ),
    }
    cfg = make_config(tmp_path).model_copy(update={"services": services})
    events = []
    proc = Mock()
    proc.run_shell.side_effect = lambda cmd, **kw: events.append(cmd) or 0
    rt = ServiceRuntime(cfg, tmp_path, proc, tmp_path / "state")

    @contextmanager
    def lock():
        events.append("lock")
        yield
        events.append("unlock")

    rt.locked = lock

    def stop(names):
        events.append(("stop", tuple(names)))
        return RunReport(True, (), None, "")

    def start(names):
        events.append(("start", tuple(names)))
        return RunReport(True, (), None, "")

    rt._stop_unlocked = stop
    rt._start_unlocked = start
    assert run_refresh(rt, cfg, ["api", "db"]).ok
    assert events == [
        "lock",
        "prepare db",
        "prepare api",
        ("stop", ("db", "api", "web")),
        ("start", ("db", "api", "web")),
        "unlock",
    ]
