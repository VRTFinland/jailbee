"""`jailbee remote web serve` and its `jailbee serve` alias (#109 ticket 01)."""

from __future__ import annotations

import builtins
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.config.models_remote import RemoteConfig, RemoteWebConfig
from jailbee.global_config import GlobalConfig
from tests.conftest import flat_output

SERVE_COMMANDS = [["remote", "web", "serve"], ["serve"]]


@pytest.fixture
def global_config(mocker: MockerFixture) -> GlobalConfig:
    cfg = GlobalConfig(remote=RemoteConfig(web=RemoteWebConfig()))
    mocker.patch("jailbee.cli._load_global", return_value=cfg)
    return cfg


@pytest.fixture
def built_frontend(tmp_path: Path, mocker: MockerFixture) -> Path:
    (tmp_path / "index.html").write_text("<!doctype html>")
    mocker.patch("jailbee.web.assets.default_static_dir", return_value=tmp_path)
    return tmp_path


def block_web_extra(mocker: MockerFixture) -> None:
    real_import = builtins.__import__

    def import_without_fastapi(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "jailbee.web" and "server" in (fromlist or ()):
            raise ModuleNotFoundError("No module named 'fastapi'", name="fastapi")
        return real_import(name, globals, locals, fromlist, level)

    mocker.patch("builtins.__import__", side_effect=import_without_fastapi)


@pytest.mark.parametrize("argv", SERVE_COMMANDS)
def test_serve_without_the_web_extra_names_it(
    argv: list[str], global_config: GlobalConfig, mocker: MockerFixture
) -> None:
    block_web_extra(mocker)

    result = CliRunner().invoke(app, argv)

    assert result.exit_code == 1
    message = flat_output(result.stderr)
    assert "optional 'web' extra" in message
    assert "jailbee[web]" in message
    assert "Traceback" not in message


@pytest.mark.parametrize("argv", SERVE_COMMANDS)
def test_serve_without_a_built_frontend_says_make_web(
    argv: list[str], global_config: GlobalConfig, tmp_path: Path, mocker: MockerFixture
) -> None:
    pytest.importorskip("fastapi")
    mocker.patch("jailbee.web.assets.default_static_dir", return_value=tmp_path)
    run = mocker.patch("uvicorn.run")

    result = CliRunner().invoke(app, argv)

    assert result.exit_code == 1
    assert "make web" in flat_output(result.stderr)
    run.assert_not_called()


@pytest.mark.parametrize("argv", SERVE_COMMANDS)
def test_serve_prints_url_and_pin_then_runs_the_server(
    argv: list[str], global_config: GlobalConfig, built_frontend: Path, mocker: MockerFixture
) -> None:
    pytest.importorskip("fastapi")
    run = mocker.patch("uvicorn.run")

    result = CliRunner().invoke(app, argv)

    assert result.exit_code == 0, result.output
    output = flat_output(result.stdout)
    assert "http://127.0.0.1:8910/" in output
    assert re.search(r"PIN: \d{6}\b", output)
    run.assert_called_once()
    assert run.call_args.kwargs["host"] == "127.0.0.1"
    assert run.call_args.kwargs["port"] == 8910


def test_serve_listen_and_port_override_config_for_one_run(
    global_config: GlobalConfig, built_frontend: Path, mocker: MockerFixture
) -> None:
    pytest.importorskip("fastapi")
    run = mocker.patch("uvicorn.run")

    result = CliRunner().invoke(app, ["serve", "--listen", "::1", "--port", "9123"])

    assert result.exit_code == 0, result.output
    assert "http://[::1]:9123/" in flat_output(result.stdout)
    assert (run.call_args.kwargs["host"], run.call_args.kwargs["port"]) == ("::1", 9123)


def test_serve_rejects_a_listen_address_that_is_not_an_ip(
    global_config: GlobalConfig, built_frontend: Path, mocker: MockerFixture
) -> None:
    pytest.importorskip("fastapi")
    run = mocker.patch("uvicorn.run")

    result = CliRunner().invoke(app, ["serve", "--listen", "example.org"])

    assert result.exit_code == 1
    assert "listen" in flat_output(result.stderr)
    run.assert_not_called()


def test_help_and_container_cli_never_import_the_web_package() -> None:
    script = """
import sys
from typer.testing import CliRunner
from jailbee.cli import app

runner = CliRunner()
for argv in (["--help"], ["remote", "web", "--help"], ["serve", "--help"]):
    result = runner.invoke(app, argv)
    if result.exit_code != 0:
        raise SystemExit(f"{argv!r}: {result.exit_code}: {result.output}")
loaded = sorted(m for m in sys.modules if m.split(".")[0] in {"fastapi", "starlette", "uvicorn"}
                or m == "jailbee.web.server" or m == "jailbee.web.app")
print(loaded)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script], check=False, capture_output=True, text=True
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "[]\n"
