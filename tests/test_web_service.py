"""The web dashboard's systemd user unit and its version-change exit (#109 ticket 14)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.config.models_remote import RemoteConfig, RemoteWebConfig
from jailbee.global_config import GlobalConfig


@pytest.fixture
def web_home(private_home: Path) -> Path:
    return private_home


def _probe(mocker: MockerFixture, *, enabled: bool, active: bool) -> None:
    def run(cmd: list[str], **_kw: object) -> object:
        ok = {"is-enabled": enabled, "is-active": active}.get(cmd[2], True)
        return mocker.Mock(returncode=0 if ok else 1, stdout="", stderr="")

    mocker.patch("subprocess.run", side_effect=run)


def test_enable_writes_the_unit_and_starts_it_without_the_web_extra(
    web_home: Path, mocker: MockerFixture
) -> None:
    from jailbee.web.service import WEB_SERVICE, enable

    mocker.patch("shutil.which", return_value="/usr/local/bin/jailbee")
    run = mocker.patch("subprocess.run")

    enable()

    unit = web_home / ".config" / "systemd" / "user" / WEB_SERVICE
    assert "ExecStart=/usr/local/bin/jailbee remote web serve" in unit.read_text()
    assert "Restart=on-failure" in unit.read_text()
    assert [c.args[0] for c in run.call_args_list] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", WEB_SERVICE],
    ]


def test_enable_a_second_time_does_not_reload_an_unchanged_unit(
    web_home: Path, mocker: MockerFixture
) -> None:
    from jailbee.web.service import WEB_SERVICE, enable

    _ = web_home
    mocker.patch("shutil.which", return_value="/usr/local/bin/jailbee")
    enable_run = mocker.patch("subprocess.run")
    enable()
    enable_run.reset_mock()

    enable()

    assert [c.args[0] for c in enable_run.call_args_list] == [
        ["systemctl", "--user", "enable", "--now", WEB_SERVICE]
    ]


def test_enable_without_jailbee_on_path_is_an_error(web_home: Path, mocker: MockerFixture) -> None:
    from jailbee.web.service import enable

    _ = web_home
    mocker.patch("shutil.which", return_value=None)
    run = mocker.patch("subprocess.run")

    with pytest.raises(RuntimeError, match="not on PATH"):
        enable()

    run.assert_not_called()


def test_disable_stops_the_unit(mocker: MockerFixture) -> None:
    from jailbee.web.service import WEB_SERVICE, disable

    run = mocker.patch("subprocess.run")

    disable()

    run.assert_called_once_with(
        ["systemctl", "--user", "disable", "--now", WEB_SERVICE], check=True
    )


def test_status_reports_a_missing_unit_as_warnings(web_home: Path, mocker: MockerFixture) -> None:
    from jailbee.web.service import status

    _ = web_home
    _probe(mocker, enabled=False, active=False)
    mocker.patch(
        "jailbee.web.service.load_global_config",
        return_value=(GlobalConfig(remote=RemoteConfig(web=RemoteWebConfig(port=9001))), []),
    )

    result = status()

    assert (result.installed, result.enabled, result.active) == (False, False, False)
    assert (result.listen, result.port) == ("127.0.0.1", 9001)
    messages = " ".join(p.message for p in result.problems)
    assert "not installed" in messages
    assert "not enabled" in messages
    assert "not active" in messages


def test_status_of_a_running_unit_has_no_problems(web_home: Path, mocker: MockerFixture) -> None:
    from jailbee.systemd import systemd_user_dir
    from jailbee.web.service import WEB_SERVICE, status

    _ = web_home
    unit_dir = systemd_user_dir()
    unit_dir.mkdir(parents=True)
    (unit_dir / WEB_SERVICE).write_text("[Unit]\n")
    _probe(mocker, enabled=True, active=True)
    mocker.patch(
        "jailbee.web.service.load_global_config",
        return_value=(GlobalConfig(remote=RemoteConfig(web=RemoteWebConfig())), []),
    )

    result = status()

    assert result.installed and result.enabled and result.active
    assert result.problems == ()


def test_cli_enable_disable_report_failures_as_exit_1(mocker: MockerFixture) -> None:
    import subprocess

    mocker.patch(
        "jailbee.web.service.enable",
        side_effect=subprocess.CalledProcessError(1, ["systemctl"]),
    )
    result = CliRunner().invoke(app, ["remote", "web", "enable"])
    assert result.exit_code == 1

    disable = mocker.patch("jailbee.web.service.disable")
    result = CliRunner().invoke(app, ["remote", "web", "disable"])
    assert result.exit_code == 0
    disable.assert_called_once_with()


def test_cli_status_prints_stable_lines_and_the_linger_tip(mocker: MockerFixture) -> None:
    from jailbee.web.service import Problem, WebServiceStatus

    mocker.patch(
        "jailbee.web.service.status",
        return_value=WebServiceStatus(
            unit_path=Path("/x/jailbee-web.service"),
            installed=True,
            enabled=True,
            active=False,
            listen="127.0.0.1",
            port=8910,
            problems=(Problem("The web service is not active."),),
        ),
    )
    tip = mocker.patch("jailbee.setup_command.linger_tip")

    result = CliRunner().invoke(app, ["remote", "web", "status"])

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        "installed: yes",
        "enabled: yes",
        "active: no",
        "listen: 127.0.0.1:8910",
        "problem: The web service is not active.",
    ]
    tip.assert_called_once_with()


# --- version watch -------------------------------------------------------


def test_watch_returns_the_new_version_and_stops_the_server() -> None:
    from jailbee.web.update_watch import watch_for_update

    versions = iter(["1.0", "1.0", "1.1"])
    stopped: list[str] = []

    replaced = watch_for_update(
        "1.0",
        stopped.append,
        installed=lambda: next(versions),
        wait=lambda _s: False,
        interval=30,
    )

    assert replaced == "1.1"
    assert stopped == ["1.1"]


def test_watch_treats_unknown_version_as_no_change_and_ends_when_told_to_stop() -> None:
    from jailbee.web.update_watch import watch_for_update

    waits = iter([False, False, True])
    stopped: list[str] = []

    replaced = watch_for_update(
        "1.0",
        stopped.append,
        installed=lambda: None,
        wait=lambda _s: next(waits),
        interval=30,
    )

    assert replaced is None
    assert stopped == []


def test_serve_exits_with_the_update_error_when_the_installed_version_changes(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    pytest.importorskip("fastapi")
    import uvicorn

    from jailbee.web import server
    from jailbee.web.errors import ServiceUpdatedError

    (tmp_path / "index.html").write_text("<!doctype html>")
    finished = threading.Event()

    def fake_run(self: uvicorn.Server, *_a: object, **_k: object) -> None:
        deadline = time.monotonic() + 5
        while not self.should_exit and time.monotonic() < deadline:
            time.sleep(0.005)
        finished.set()

    mocker.patch.object(uvicorn.Server, "run", autospec=True, side_effect=fake_run)
    echoed: list[str] = []

    with pytest.raises(ServiceUpdatedError) as caught:
        server.serve(
            RemoteWebConfig(),
            echo=echoed.append,
            static_dir=tmp_path,
            installed_version=lambda: "99.0",
            poll_interval=0.01,
        )

    assert finished.is_set()
    assert caught.value.installed == "99.0"
