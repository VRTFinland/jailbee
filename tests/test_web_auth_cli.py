"""`jailbee remote web auth ...`: CLI-only management of web logins (#109 ticket 11).

Through `CliRunner` over the real per-test state database; prompts through the
`jailbee.prompting` seams.
"""

from __future__ import annotations

import builtins
import re
from typing import Any

import pytest
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.db import get_engine
from jailbee.web import auth_admin
from jailbee.web.auth import SessionStore
from tests.conftest import flat_output
from tests.web_fakes import FakeClock

pytest.importorskip("pyotp")


def run(*argv: str, input: str | None = None) -> Any:
    return CliRunner().invoke(app, ["remote", "web", "auth", *argv], input=input)


def make_session(device: str) -> None:
    SessionStore(get_engine(), FakeClock()).create(device)


@pytest.fixture
def interactive(mocker: MockerFixture) -> None:
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)


@pytest.fixture
def noninteractive(mocker: MockerFixture) -> None:
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)


def test_status_on_a_fresh_install_shows_the_pin_on_and_nothing_else() -> None:
    result = run("status")

    assert result.exit_code == 0, result.output
    out = flat_output(result.output)
    assert "PIN: on" in out
    assert "TOTP keys: 0" in out
    assert "Sessions: 0" in out


def test_totp_add_prints_the_secret_and_a_qr_code_and_stores_the_key() -> None:
    result = run("totp", "add", "--name", "phone")

    assert result.exit_code == 0, result.output
    [key] = auth_admin.list_totp_keys(get_engine())
    assert key.name == "phone"
    secret = re.search(r"Secret: ([A-Z2-7]{32})", result.output)
    assert secret is not None
    assert "otpauth://totp/" in result.output
    assert "█" in result.output or "▀" in result.output, "a terminal QR code"


def test_totp_ls_names_each_key_and_never_its_secret() -> None:
    new = auth_admin.add_totp_key(get_engine(), "phone")

    result = run("totp", "ls")

    assert result.exit_code == 0, result.output
    assert "phone" in result.output
    assert new.secret not in result.output


def test_totp_rm_asks_which_key_even_for_a_single_one(
    interactive: None, mocker: MockerFixture
) -> None:
    auth_admin.add_totp_key(get_engine(), "phone")
    select = mocker.patch(
        "jailbee.prompting._select", side_effect=lambda noun, options, **_: options[0].value
    )

    result = run("totp", "rm")

    assert result.exit_code == 0, result.output
    assert select.call_count == 1
    assert auth_admin.list_totp_keys(get_engine()) == []


def test_totp_rm_off_a_terminal_names_the_candidates(noninteractive: None) -> None:
    auth_admin.add_totp_key(get_engine(), "phone")

    result = run("totp", "rm")

    assert result.exit_code == 2
    assert "phone" in flat_output(result.output)
    assert len(auth_admin.list_totp_keys(get_engine())) == 1


def test_totp_rm_by_id_removes_that_key() -> None:
    keep = auth_admin.add_totp_key(get_engine(), "phone").key
    drop = auth_admin.add_totp_key(get_engine(), "spare").key

    result = run("totp", "rm", str(drop.id))

    assert result.exit_code == 0, result.output
    assert auth_admin.list_totp_keys(get_engine()) == [keep]


def test_pin_off_without_a_totp_key_is_refused() -> None:
    result = run("pin", "off")

    assert result.exit_code == 1
    assert "no TOTP key" in flat_output(result.output)
    assert auth_admin.pin_enabled(get_engine())


def test_pin_off_then_on() -> None:
    auth_admin.add_totp_key(get_engine(), "phone")

    assert run("pin", "off").exit_code == 0
    assert not auth_admin.pin_enabled(get_engine())
    assert run("pin", "on").exit_code == 0
    assert auth_admin.pin_enabled(get_engine())


def test_sessions_ls_shows_device_and_last_use() -> None:
    make_session("boox")

    result = run("sessions", "ls")

    assert result.exit_code == 0, result.output
    assert "boox" in result.output
    assert "2026-11-21" in result.output


def test_sessions_revoke_picks_a_device_and_audits_it(
    interactive: None, mocker: MockerFixture
) -> None:
    make_session("boox")
    make_session("lost tablet")
    mocker.patch(
        "jailbee.prompting._select",
        side_effect=lambda noun, options, **_: next(
            o.value for o in options if "lost tablet" in o.title
        ),
    )

    result = run("sessions", "revoke")

    assert result.exit_code == 0, result.output
    assert [s.device_name for s in auth_admin.list_sessions(get_engine())] == ["boox"]
    [event] = auth_admin.recent_events(get_engine())
    assert (event.event, event.device_name) == ("revoke", "lost tablet")


def test_sessions_revoke_of_an_unknown_id_fails() -> None:
    result = run("sessions", "revoke", "99")

    assert result.exit_code == 1
    assert "No web session with id 99" in flat_output(result.output)


def test_log_lists_audit_events_with_device_and_time() -> None:
    auth_admin.record_event(
        get_engine(), "login_failed", clock=FakeClock(), method="totp", device_name="thief"
    )

    result = run("log")

    assert result.exit_code == 0, result.output
    out = flat_output(result.output)
    assert "login_failed" in out
    assert "totp" in out
    assert "thief" in out
    assert "2026-11-21" in out


def test_totp_add_without_the_web_extra_names_it(mocker: MockerFixture) -> None:
    real_import = builtins.__import__

    def without_pyotp(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "pyotp":
            raise ModuleNotFoundError("No module named 'pyotp'", name="pyotp")
        return real_import(name, *args, **kwargs)

    mocker.patch("builtins.__import__", side_effect=without_pyotp)

    result = run("totp", "add")

    assert result.exit_code == 1
    assert "uv tool install --reinstall jailbee" in flat_output(result.output)
    assert auth_admin.list_totp_keys(get_engine()) == []


@pytest.fixture
def serve_ready(tmp_path: Any, mocker: MockerFixture) -> Any:
    """`remote web serve` with default config, a built frontend and uvicorn's run mocked."""
    pytest.importorskip("fastapi")
    uvicorn = pytest.importorskip("uvicorn")
    from jailbee.config.models_remote import RemoteConfig, RemoteWebConfig
    from jailbee.global_config import GlobalConfig

    mocker.patch(
        "jailbee.cli._load_global",
        return_value=GlobalConfig(remote=RemoteConfig(web=RemoteWebConfig())),
    )
    (tmp_path / "index.html").write_text("<!doctype html>")
    mocker.patch("jailbee.web.assets.default_static_dir", return_value=tmp_path)
    return mocker.patch.object(uvicorn.Server, "run", autospec=True)


def test_serve_with_the_pin_off_prints_no_pin(serve_ready: Any) -> None:
    auth_admin.add_totp_key(get_engine(), "phone")
    auth_admin.set_pin_enabled(get_engine(), False)

    result = CliRunner().invoke(app, ["remote", "web", "serve"])

    assert result.exit_code == 0, result.output
    out = flat_output(result.stdout)
    assert re.search(r"PIN: \d{6}", out) is None
    assert "PIN login is off" in out


def test_the_running_server_prints_audit_lines(serve_ready: Any) -> None:
    serve_ready.side_effect = lambda _self: auth_admin.record_event(
        get_engine(), "login", clock=FakeClock(), method="totp", device_name="boox"
    )

    result = CliRunner().invoke(app, ["remote", "web", "serve"])

    assert result.exit_code == 0, result.output
    assert "web auth login method=totp device='boox'" in flat_output(result.stderr)
