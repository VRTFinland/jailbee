"""Exponential backoff for failed PIN and TOTP logins (#109 ticket 17).

Through the HTTP seam (`TestClient` over `create_app`) with the fake clock,
which is also the limiter's monotonic clock.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("pyotp")

import pyotp
from fastapi.testclient import TestClient

from jailbee.db import get_engine
from jailbee.web import auth_admin, login_link
from jailbee.web.app import WebDeps, create_app
from jailbee.web.auth import PinHolder, RateLimiter
from tests.web_fakes import FakeClock, FakeSourceFactory, pin_sequence

ESCALATING_WAITS = [4, 8, 16, 32, 64, 128, 256, 512, 1024, 1800, 1800, 1800]
"""Retry-After after the 6th, 7th, … consecutive failure: doubling, capped at 30 min."""


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def rotations() -> list[tuple[str, str]]:
    return []


@pytest.fixture
def pin(rotations: list[tuple[str, str]]) -> PinHolder:
    return PinHolder(
        generate=pin_sequence(), on_rotate=lambda new, why: rotations.append((new, why))
    )


@pytest.fixture
def client(clock: FakeClock, pin: PinHolder) -> TestClient:
    deps = WebDeps(
        snapshot_source=FakeSourceFactory(),
        engine=get_engine(),
        clock=clock,
        monotonic=clock.monotonic,
        pin=pin,
    )
    return TestClient(create_app(deps))


def pin_attempt(client: TestClient, guess: str, device: str = "thief") -> tuple[int, str | None]:
    response = client.post("/api/auth", json={"method": "pin", "pin": guess, "device_name": device})
    return response.status_code, response.headers.get("Retry-After")


def wrong(pin: PinHolder) -> str:
    return "000000" if pin.current != "000000" else "999999"


def fail_pin(client: TestClient, pin: PinHolder) -> None:
    assert pin_attempt(client, wrong(pin))[0] == 401


def wait_out(client: TestClient, pin: PinHolder, clock: FakeClock) -> int:
    """Probe with a wrong PIN, expect a refusal, advance past its Retry-After; the wait."""
    status, retry_after = pin_attempt(client, wrong(pin))
    assert status == 429
    assert retry_after is not None
    clock.advance(timedelta(seconds=int(retry_after)))
    return int(retry_after)


def five_failures(client: TestClient, pin: PinHolder, clock: FakeClock) -> None:
    """Five wrong PINs at once, then the minute window drained."""
    for _ in range(5):
        fail_pin(client, pin)
    assert wait_out(client, pin, clock) == 60


def test_waits_double_after_five_failures_up_to_thirty_minutes(
    client: TestClient, pin: PinHolder, clock: FakeClock
) -> None:
    five_failures(client, pin, clock)

    waits = []
    for _ in ESCALATING_WAITS:
        fail_pin(client, pin)
        waits.append(wait_out(client, pin, clock))

    assert waits == ESCALATING_WAITS


def test_a_minute_window_refusal_says_when_to_retry(
    client: TestClient, pin: PinHolder, clock: FakeClock
) -> None:
    for _ in range(5):
        fail_pin(client, pin)
    clock.advance(timedelta(seconds=20))

    assert pin_attempt(client, wrong(pin)) == (429, "40")


def test_a_few_typos_cost_no_wait(client: TestClient, pin: PinHolder) -> None:
    for _ in range(4):
        fail_pin(client, pin)

    assert pin_attempt(client, pin.current, device="owner")[0] == 200


def test_attempts_during_the_wait_are_refused_without_being_checked(
    client: TestClient, pin: PinHolder, clock: FakeClock, rotations: list[tuple[str, str]]
) -> None:
    five_failures(client, pin, clock)
    fail_pin(client, pin)
    clock.advance(timedelta(seconds=1))
    spent = len(rotations)

    assert pin_attempt(client, pin.current, device="owner") == (429, "3")
    assert len(rotations) == spent  # the PIN was not tried, so not spent

    clock.advance(timedelta(seconds=3))
    assert pin_attempt(client, pin.current, device="owner")[0] == 200


def test_a_successful_login_resets_the_backoff(
    client: TestClient, pin: PinHolder, clock: FakeClock
) -> None:
    five_failures(client, pin, clock)
    for _ in range(3):
        fail_pin(client, pin)
        wait_out(client, pin, clock)
    assert pin_attempt(client, pin.current, device="owner")[0] == 200
    clock.advance(timedelta(seconds=61))

    for _ in range(4):
        fail_pin(client, pin)
    assert pin_attempt(client, pin.current, device="owner")[0] == 200


def test_each_escalation_is_audited_once(
    client: TestClient, pin: PinHolder, clock: FakeClock
) -> None:
    five_failures(client, pin, clock)
    for _ in ESCALATING_WAITS:
        fail_pin(client, pin)
        for _ in range(3):  # refused probes add nothing
            assert pin_attempt(client, wrong(pin))[0] == 429
        wait_out(client, pin, clock)

    backoffs = [
        (e.method, e.device_name)
        for e in auth_admin.recent_events(get_engine(), limit=1000)
        if e.event == "backoff"
    ]
    # Failures 5 through 15 each lengthen the wait; past the cap nothing escalates.
    assert backoffs == [("pin", "thief")] * 11


def test_totp_backs_off_on_its_own(client: TestClient, pin: PinHolder, clock: FakeClock) -> None:
    secret = auth_admin.add_totp_key(get_engine(), "phone", clock=clock).secret
    totp = pyotp.TOTP(secret)
    near = {str(totp.at(clock.now + timedelta(seconds=d))) for d in (-30, 0, 30)}
    bad = next(c for c in (f"{n:06d}" for n in range(1000)) if c not in near)

    def totp_attempt(code: str) -> tuple[int, str | None]:
        response = client.post(
            "/api/auth", json={"method": "totp", "code": code, "device_name": "thief"}
        )
        return response.status_code, response.headers.get("Retry-After")

    for _ in range(5):
        assert totp_attempt(bad)[0] == 401
    clock.advance(timedelta(seconds=60))
    assert totp_attempt(bad)[0] == 401
    assert totp_attempt(bad) == (429, "4")
    # The PIN bucket is untouched.
    assert pin_attempt(client, pin.current, device="owner")[0] == 200


def _parallel_refusal(
    clock: FakeClock, prior_failures: int, *, escalated: bool = False
) -> tuple[int, str | None]:
    """Fail ``prior_failures`` times, then fail once more with a parallel guess mid-check.

    ``escalated`` adds a failure past the threshold and sits out its wait first.
    """
    parallel: list[tuple[int, str | None]] = []
    holder: dict[str, TestClient] = {}
    armed: list[bool] = []

    def on_rotate(new: str, why: str) -> None:
        # Runs inside the armed attempt's check: the parallel request arrives now.
        if armed:
            armed.clear()
            parallel.append(pin_attempt(holder["client"], "000000"))

    pin = PinHolder(generate=pin_sequence(), on_rotate=on_rotate)
    deps = WebDeps(
        snapshot_source=FakeSourceFactory(),
        engine=get_engine(),
        clock=clock,
        monotonic=clock.monotonic,
        pin=pin,
    )
    client = holder["client"] = TestClient(create_app(deps))
    for _ in range(prior_failures):
        fail_pin(client, pin)
    clock.advance(timedelta(seconds=60))  # the minute window is not what holds the parallel one
    if escalated:
        fail_pin(client, pin)
        wait_out(client, pin, clock)
    armed.append(True)
    fail_pin(client, pin)
    assert len(parallel) == 1
    return parallel[0]


def test_an_attempt_in_flight_holds_back_a_parallel_one(clock: FakeClock) -> None:
    """Past the threshold, a second guess sent while the first is still being checked waits."""
    assert _parallel_refusal(clock, 5, escalated=True) == (429, "8")


def test_a_parallel_attempt_is_held_back_at_the_threshold_boundary(clock: FakeClock) -> None:
    """At four failures the in-flight attempt would earn the first wait: hold from there."""
    assert _parallel_refusal(clock, 4) == (429, "2")


def test_a_fractional_wait_is_rounded_up_in_retry_after(
    client: TestClient, pin: PinHolder, clock: FakeClock
) -> None:
    for _ in range(5):
        fail_pin(client, pin)
    clock.advance(timedelta(milliseconds=500))  # 59.5 s of the minute window left

    assert pin_attempt(client, wrong(pin)) == (429, "60")


def test_a_link_login_lifts_the_pin_and_totp_waits(
    client: TestClient, pin: PinHolder, clock: FakeClock
) -> None:
    five_failures(client, pin, clock)
    fail_pin(client, pin)
    secret = auth_admin.add_totp_key(get_engine(), "phone", clock=clock).secret
    totp = pyotp.TOTP(secret)
    near = {str(totp.at(clock.now + timedelta(seconds=d))) for d in (-30, 0, 30)}
    bad = next(c for c in (f"{n:06d}" for n in range(1000)) if c not in near)
    for _ in range(5):
        client.post("/api/auth", json={"method": "totp", "code": bad, "device_name": "thief"})
    assert pin_attempt(client, pin.current)[0] == 429

    token = login_link.mint(get_engine(), clock=clock)
    link = client.post(
        "/api/auth", json={"method": "link", "token": token, "device_name": "owner"}
    )
    assert link.status_code == 200

    assert pin_attempt(client, pin.current, device="owner")[0] == 200
    clock.advance(timedelta(seconds=60))  # only the minute window is left on TOTP
    code = client.post(
        "/api/auth",
        json={"method": "totp", "code": totp.at(clock.now), "device_name": "owner"},
    )
    assert code.status_code == 200


def test_a_pin_login_lifts_the_totp_wait(
    client: TestClient, pin: PinHolder, clock: FakeClock
) -> None:
    secret = auth_admin.add_totp_key(get_engine(), "phone", clock=clock).secret
    totp = pyotp.TOTP(secret)
    near = {str(totp.at(clock.now + timedelta(seconds=d))) for d in (-30, 0, 30)}
    bad = next(c for c in (f"{n:06d}" for n in range(1000)) if c not in near)
    for _ in range(5):
        client.post("/api/auth", json={"method": "totp", "code": bad, "device_name": "thief"})
    clock.advance(timedelta(seconds=60))
    client.post("/api/auth", json={"method": "totp", "code": bad, "device_name": "thief"})
    assert pin_attempt(client, pin.current)[0] == 200

    code = client.post(
        "/api/auth",
        json={"method": "totp", "code": totp.at(clock.now), "device_name": "owner"},
    )
    assert code.status_code == 200


def test_the_wait_stays_at_the_cap_however_long_the_run() -> None:
    limiter = RateLimiter(lambda: 0.0)
    for _ in range(2000):
        limiter.failed("pin")

    assert limiter.check("pin") == 1800.0
