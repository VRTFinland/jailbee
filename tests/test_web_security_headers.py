"""Every response of both web apps carries the browser hardening headers.

The session token lives in localStorage and transcripts render through
`v-html`, so a script the page did not ship must not run, the page must not
be framed, and no response may be sniffed into another type.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from jailbee.db import get_engine
from jailbee.web.app import WebDeps, create_app
from jailbee.web.auth import PinHolder
from jailbee.web.setup_app import SetupDeps, create_setup_app
from tests.web_fakes import FakeClock, FakeSourceFactory, pin_sequence


@pytest.fixture
def static(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text("<!doctype html><title>x</title>")
    return tmp_path


def dashboard(static: Path) -> TestClient:
    clock = FakeClock()
    deps = WebDeps(
        snapshot_source=FakeSourceFactory(),
        engine=get_engine(),
        clock=clock,
        monotonic=clock.monotonic,
        pin=PinHolder(generate=pin_sequence()),
        static_dir=static,
    )
    return TestClient(create_app(deps))


def setup(static: Path) -> TestClient:
    deps = SetupDeps(engine=get_engine(), pin=PinHolder(generate=pin_sequence()), static_dir=static)
    return TestClient(create_setup_app(deps))


def directives(csp: str) -> dict[str, str]:
    parts = (d.strip().split(None, 1) for d in csp.split(";") if d.strip())
    return {p[0]: p[1] if len(p) > 1 else "" for p in parts}


@pytest.mark.parametrize("app", [dashboard, setup], ids=["dashboard", "setup"])
@pytest.mark.parametrize("path", ["/", "/index.html", "/api/auth/methods", "/api/nope"])
def test_every_response_carries_the_hardening_headers(app, static: Path, path: str) -> None:
    response = app(static).get(path)

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    csp = directives(response.headers["Content-Security-Policy"])
    assert csp["default-src"] == "'self'"
    assert csp["frame-ancestors"] == "'none'"
    assert csp["object-src"] == "'none'"
    assert csp["base-uri"] == "'none'"
    assert "unsafe-inline" not in response.headers["Content-Security-Policy"]
