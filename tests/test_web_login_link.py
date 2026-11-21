"""The localhost login link and auth-disabled mode (#109 ticket 13).

HTTP seam (`TestClient` over `create_app`) with a fake clock and the per-test
state database; the link is minted through `jailbee.web.login_link`, the
module `jailbee remote web open` drives.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from pytest_mock import MockerFixture
from starlette.websockets import WebSocketDisconnect
from typer.testing import CliRunner

from jailbee.cli import app as cli_app
from jailbee.config.models_remote import RemoteConfig, RemoteWebAuthConfig, RemoteWebConfig
from jailbee.db import get_engine
from jailbee.global_config import GlobalConfig
from jailbee.web import auth_admin, login_link
from jailbee.web.app import WebDeps, create_app
from jailbee.web.auth import PinHolder
from jailbee.web.protocol import PROTOCOL_VERSION
from tests.conftest import flat_output
from tests.web_fakes import FakeClock, FakeSourceFactory, pin_sequence


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_client(clock: FakeClock, **extra: object) -> TestClient:
    deps = WebDeps(
        snapshot_source=FakeSourceFactory(),
        engine=get_engine(),
        clock=clock,
        monotonic=clock.monotonic,
        pin=PinHolder(generate=pin_sequence()),
        **extra,  # type: ignore[arg-type]  # test helper forwards optional WebDeps fields
    )
    return TestClient(create_app(deps))


@pytest.fixture
def client(clock: FakeClock) -> TestClient:
    return make_client(clock)


def hello(token: str) -> str:
    return json.dumps({"type": "auth", "v": PROTOCOL_VERSION, "token": token})


def link_login(client: TestClient, token: str, device: str = "laptop") -> int:
    r = client.post("/api/auth", json={"method": "link", "token": token, "device_name": device})
    return r.status_code


def test_a_link_token_logs_in_once(client: TestClient, clock: FakeClock) -> None:
    token = login_link.mint(get_engine(), clock=clock)

    first = client.post(
        "/api/auth", json={"method": "link", "token": token, "device_name": "laptop"}
    )
    assert first.status_code == 200
    session = client.get(
        "/api/session", headers={"Authorization": f"Bearer {first.json()['token']}"}
    )
    assert session.json()["device_name"] == "laptop"

    assert link_login(client, token) == 401, "a used link must not log in again"


def test_an_expired_link_is_rejected(client: TestClient, clock: FakeClock) -> None:
    token = login_link.mint(get_engine(), clock=clock)
    clock.advance(login_link.LINK_TTL + timedelta(seconds=1))

    assert link_login(client, token) == 401


def test_a_link_works_right_up_to_its_expiry(client: TestClient, clock: FakeClock) -> None:
    token = login_link.mint(get_engine(), clock=clock)
    clock.advance(login_link.LINK_TTL - timedelta(seconds=1))

    assert link_login(client, token) == 200


def test_an_unknown_link_token_is_rejected(client: TestClient) -> None:
    assert link_login(client, "nope") == 401


def test_the_token_is_stored_hashed(clock: FakeClock) -> None:
    from sqlmodel import Session, select

    from jailbee.db.models import WebLoginLink

    token = login_link.mint(get_engine(), clock=clock)
    with Session(get_engine()) as s:
        rows = s.exec(select(WebLoginLink)).all()
    assert [r.token_hash for r in rows] and token not in [r.token_hash for r in rows]


def test_link_logins_are_audited(client: TestClient, clock: FakeClock) -> None:
    good = login_link.mint(get_engine(), clock=clock)
    link_login(client, good, "laptop")
    link_login(client, good, "laptop")

    events = [(e.event, e.method) for e in auth_admin.recent_events(get_engine())]
    assert events == [("login", "link"), ("login_failed", "link")]


def test_auth_methods_do_not_offer_the_link(client: TestClient) -> None:
    assert "link" not in client.get("/api/auth/methods").json()["methods"]


# --- auth disabled ----------------------------------------------------------

LOCAL = {"host": "localhost:8910"}
"""With login off only a loopback `Host` is served (`web.origin_guard`)."""


def test_auth_disabled_serves_the_api_without_a_session(clock: FakeClock) -> None:
    client = make_client(clock, auth_enabled=False)

    assert client.get("/api/session", headers=LOCAL).status_code == 200
    assert client.get("/api/auth/methods", headers=LOCAL).json()["auth_disabled"] is True



def test_auth_disabled_serves_the_websocket_without_a_session(clock: FakeClock) -> None:
    client = make_client(clock, auth_enabled=False)

    with client.websocket_connect("/api/ws", headers=LOCAL) as ws:
        ws.send_text(hello("anonymous"))
        assert ws.receive_json()["type"] == "hello"


def test_with_auth_enabled_loopback_still_needs_a_session(client: TestClient) -> None:
    # TestClient's peer address is not loopback, so pin the source explicitly.
    loopback = TestClient(client.app, client=("127.0.0.1", 50000))

    assert loopback.get("/api/session").status_code == 401
    assert loopback.get("/api/auth/methods").json()["auth_disabled"] is False
    with pytest.raises(WebSocketDisconnect):
        with loopback.websocket_connect("/api/ws") as ws:
            ws.send_text(hello("x"))
            ws.receive_json()


# --- CLI: bind refusal, warning, open ---------------------------------------


def configure(mocker: MockerFixture, **web: object) -> None:
    cfg = GlobalConfig(remote=RemoteConfig(web=RemoteWebConfig(**web)))  # type: ignore[arg-type]
    mocker.patch("jailbee.cli._load_global", return_value=cfg)


@pytest.fixture
def built_frontend(tmp_path: Path, mocker: MockerFixture) -> Path:
    (tmp_path / "index.html").write_text("<!doctype html>")
    mocker.patch("jailbee.web.assets.default_static_dir", return_value=tmp_path)
    return tmp_path


def auth_off(**extra: object) -> dict[str, object]:
    return {"auth": RemoteWebAuthConfig(enabled=False, **extra)}  # type: ignore[arg-type]


def test_auth_disabled_on_a_non_loopback_address_is_refused_naming_the_opt_in(
    built_frontend: Path, mocker: MockerFixture
) -> None:
    import uvicorn

    run = mocker.patch.object(uvicorn.Server, "run", autospec=True)
    configure(mocker, listen="0.0.0.0", **auth_off())

    result = CliRunner().invoke(cli_app, ["serve"])

    assert result.exit_code == 1
    assert "remote.web.auth.allow_unauthenticated_remote" in flat_output(result.stderr)
    run.assert_not_called()


def test_the_listen_override_is_checked_too(built_frontend: Path, mocker: MockerFixture) -> None:
    import uvicorn

    run = mocker.patch.object(uvicorn.Server, "run", autospec=True)
    configure(mocker, **auth_off())

    result = CliRunner().invoke(cli_app, ["serve", "--listen", "192.168.1.5"])

    assert result.exit_code == 1
    run.assert_not_called()


def test_auth_disabled_on_loopback_serves_and_always_warns(
    built_frontend: Path, mocker: MockerFixture
) -> None:
    import uvicorn

    run = mocker.patch.object(uvicorn.Server, "run", autospec=True)
    configure(mocker, **auth_off())

    result = CliRunner().invoke(cli_app, ["serve"])

    out = flat_output(result.stdout)
    assert result.exit_code == 0, result.output
    assert "reverse proxy or SSH tunnel" in out
    assert "PIN:" not in out
    run.assert_called_once()
    assert run.call_args.args[0].config.app.state.web.deps.auth_enabled is False


def test_opt_in_allows_a_non_loopback_address_and_still_warns(
    built_frontend: Path, mocker: MockerFixture
) -> None:
    import uvicorn

    run = mocker.patch.object(uvicorn.Server, "run", autospec=True)
    configure(mocker, listen="0.0.0.0", **auth_off(allow_unauthenticated_remote=True))

    result = CliRunner().invoke(cli_app, ["serve"])

    assert result.exit_code == 0, result.output
    assert "reverse proxy or SSH tunnel" in flat_output(result.stdout)
    run.assert_called_once()


def test_auth_enabled_prints_no_bypass_warning(built_frontend: Path, mocker: MockerFixture) -> None:
    import uvicorn

    mocker.patch.object(uvicorn.Server, "run", autospec=True)
    configure(mocker)

    result = CliRunner().invoke(cli_app, ["serve"])

    assert "bypass" not in flat_output(result.stdout)


def test_open_mints_a_link_and_hands_it_to_the_browser(mocker: MockerFixture) -> None:
    configure(mocker, port=9123)
    mocker.patch("jailbee.web.open_link.server_running", return_value=True)
    opened = mocker.patch("webbrowser.open", return_value=True)

    result = CliRunner().invoke(cli_app, ["remote", "web", "open"])

    assert result.exit_code == 0, result.output
    (url,) = opened.call_args.args
    prefix = "http://localhost:9123/#/login?link="
    assert url.startswith(prefix)
    token = url.removeprefix(prefix)
    client = make_client(FakeClock())
    assert link_login(client, token, "this machine") == 200
    assert token not in flat_output(result.stdout), "the token goes to the browser, not the screen"


def test_open_says_so_when_the_server_is_not_running(mocker: MockerFixture) -> None:
    configure(mocker)
    mocker.patch("jailbee.web.open_link.server_running", return_value=False)
    opened = mocker.patch("webbrowser.open", return_value=True)

    result = CliRunner().invoke(cli_app, ["remote", "web", "open"])

    assert result.exit_code == 1
    assert "not running" in flat_output(result.stderr)
    opened.assert_not_called()


def test_open_with_auth_disabled_opens_the_plain_address(mocker: MockerFixture) -> None:
    configure(mocker, **auth_off())
    mocker.patch("jailbee.web.open_link.server_running", return_value=True)
    opened = mocker.patch("webbrowser.open", return_value=True)

    result = CliRunner().invoke(cli_app, ["remote", "web", "open"])

    assert result.exit_code == 0, result.output
    assert opened.call_args.args == ("http://localhost:8910/",)
