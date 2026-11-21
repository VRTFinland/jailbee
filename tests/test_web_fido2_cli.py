"""`jailbee remote web auth fido2 ...` and the FIDO2 lock-out guards (#109 ticket 12).

Through `CliRunner` over the real per-test state database. Keys are
registered with `Fido2Auth` and a software authenticator, as the setup
server would; the setup server itself runs with uvicorn's `run` mocked.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from pytest_mock import MockerFixture
from typer.testing import CliRunner

pytest.importorskip("fido2")
pytest.importorskip("fastapi")

from jailbee.cli import app
from jailbee.db import get_engine
from jailbee.web import auth_admin
from jailbee.web.fido2_auth import Fido2Auth, RelyingParty
from tests.conftest import flat_output
from tests.fido2_authenticator import SoftAuthenticator
from tests.web_fakes import FakeClock

RP = RelyingParty("jb.example.com", "https://jb.example.com")


def run(*argv: str) -> Any:
    return CliRunner().invoke(app, ["remote", "web", "auth", *argv])


def add_key(name: str) -> auth_admin.Fido2KeyInfo:
    clock = FakeClock()
    fido2 = Fido2Auth(get_engine(), clock=clock, monotonic=clock.monotonic)
    challenge = fido2.register_begin(RP)
    credential = SoftAuthenticator(RP.origin).create(challenge.options)
    return fido2.register_complete(RP, challenge.id, credential, name)


@pytest.fixture
def interactive(mocker: MockerFixture) -> None:
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)


@pytest.fixture
def noninteractive(mocker: MockerFixture) -> None:
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)


def test_fido2_ls_names_each_key_and_its_domain() -> None:
    add_key("yubikey")

    result = run("fido2", "ls")

    assert result.exit_code == 0, result.output
    out = flat_output(result.output)
    assert "yubikey" in out
    assert "jb.example.com" in out


def test_status_counts_fido2_keys() -> None:
    add_key("yubikey")

    out = flat_output(run("status").output)

    assert "FIDO2 keys: 1" in out


def test_fido2_rm_by_id_removes_that_key() -> None:
    keep = add_key("yubikey")
    drop = add_key("spare")

    result = run("fido2", "rm", str(drop.id))

    assert result.exit_code == 0, result.output
    assert [k.id for k in auth_admin.list_fido2_keys(get_engine())] == [keep.id]


def test_fido2_rm_asks_which_key_even_for_a_single_one(
    interactive: None, mocker: MockerFixture
) -> None:
    add_key("yubikey")
    select = mocker.patch(
        "jailbee.prompting._select", side_effect=lambda noun, options, **_: options[0].value
    )

    result = run("fido2", "rm")

    assert result.exit_code == 0, result.output
    assert select.call_count == 1
    assert auth_admin.list_fido2_keys(get_engine()) == []


def test_fido2_rm_off_a_terminal_names_the_candidates(noninteractive: None) -> None:
    add_key("yubikey")

    result = run("fido2", "rm")

    assert result.exit_code == 2
    assert "yubikey" in flat_output(result.output)
    assert len(auth_admin.list_fido2_keys(get_engine())) == 1


def test_a_fido2_key_lets_the_pin_be_turned_off() -> None:
    add_key("yubikey")

    result = run("pin", "off")

    assert result.exit_code == 0, result.output
    assert not auth_admin.pin_enabled(get_engine())


def test_the_last_fido2_key_cannot_be_removed_while_it_is_the_only_login() -> None:
    key = add_key("yubikey")
    auth_admin.set_pin_enabled(get_engine(), False)

    result = run("fido2", "rm", str(key.id))

    assert result.exit_code == 1
    assert "nobody could log in" in flat_output(result.output)
    assert len(auth_admin.list_fido2_keys(get_engine())) == 1


def test_the_last_totp_key_can_go_while_a_fido2_key_remains() -> None:
    totp = auth_admin.add_totp_key(get_engine(), "phone").key
    add_key("yubikey")
    auth_admin.set_pin_enabled(get_engine(), False)

    result = run("totp", "rm", str(totp.id))

    assert result.exit_code == 0, result.output


@pytest.fixture
def setup_ready(tmp_path: Path, mocker: MockerFixture) -> Any:
    """Default config, a built frontend, and uvicorn's `run` mocked."""
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


def test_fido2_setup_serves_the_setup_app_on_the_dashboard_address(setup_ready: Any) -> None:
    result = run("fido2", "setup")

    assert result.exit_code == 0, result.output
    out = flat_output(result.stdout)
    assert re.search(r"Setup PIN: \d{6}\b", out)
    assert "#/setup" in out
    server = setup_ready.call_args.args[0]
    assert (server.config.host, server.config.port) == ("127.0.0.1", 8910)
    setup_app = server.config.app
    assert setup_app.title == "JailBee setup"


def test_fido2_setup_port_override(setup_ready: Any) -> None:
    result = run("fido2", "setup", "--port", "9200")

    assert result.exit_code == 0, result.output
    assert setup_ready.call_args.args[0].config.port == 9200
