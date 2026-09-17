"""PyPI update check: comparison, install detection, cache, hint and probe."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlmodel import Session

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


# --- version comparison ----------------------------------------------------


def test_newer_version_reports_a_strictly_newer_release() -> None:
    from jailbee.update_check import newer_version

    assert newer_version("1.4.0", "1.5.0") == "1.5.0"


@pytest.mark.parametrize("latest", ["1.4.0", "1.3.9", "0.9.0"])
def test_newer_version_is_silent_when_not_behind(latest: str) -> None:
    from jailbee.update_check import newer_version

    assert newer_version("1.4.0", latest) is None


@pytest.mark.parametrize(
    ("current", "latest"),
    [
        ("0.0.0+unknown", "1.5.0"),  # an install with no package metadata
        ("1.4.0", "1.5.0rc1"),  # PyPI serving a prerelease as `info.version`
        ("1.4.0", None),  # nothing cached yet
    ],
)
def test_newer_version_is_silent_on_a_version_it_cannot_compare(
    current: str, latest: str | None
) -> None:
    from jailbee.update_check import newer_version

    assert newer_version(current, latest) is None


# --- install detection -----------------------------------------------------


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        (
            "/home/u/.local/share/uv/tools/jailbee/lib/python3.13/site-packages",
            "uv tool upgrade jailbee",
        ),
        (
            "/home/u/.local/share/pipx/venvs/jailbee/lib/python3.13/site-packages",
            "pipx upgrade jailbee",
        ),
        ("/home/u/.venvs/jailbee/lib/python3.13/site-packages", "pip install -U jailbee"),
    ],
)
def test_classify_install_names_the_upgrade_command_for_the_manager(
    location: str, expected: str
) -> None:
    from jailbee.update_check import classify_install

    assert classify_install(editable=False, location=Path(location)).upgrade_command == expected


def test_classify_install_has_no_command_for_an_editable_install() -> None:
    """`uv tool install -e .` runs from a checkout — PyPI has nothing to say
    to it, and advising an upgrade would overwrite the developer's own tree."""
    from jailbee.update_check import classify_install

    install = classify_install(editable=True, location=Path("/home/u/src/jailbee/src"))
    assert install.manager == "editable"
    assert install.upgrade_command is None


def test_detect_install_is_silent_when_the_package_metadata_is_missing(mocker) -> None:
    """No metadata means no way to tell which manager owns the install, and a
    guessed command could damage it."""
    from importlib.metadata import PackageNotFoundError

    from jailbee import update_check

    mocker.patch.object(
        update_check.metadata, "distribution", side_effect=PackageNotFoundError("jailbee")
    )
    assert update_check.detect_install().upgrade_command is None


# --- hint rendering --------------------------------------------------------


def test_hint_lines_name_both_versions_and_the_command() -> None:
    from jailbee.update_check import Install, hint_lines

    lines = hint_lines("1.4.0", "1.5.0", Install("uv", "uv tool upgrade jailbee"))

    assert "1.5.0" in lines[0]
    assert "1.4.0" in lines[0]
    assert any("uv tool upgrade jailbee" in line for line in lines)


def test_hint_lines_are_empty_without_an_upgrade_command() -> None:
    from jailbee.update_check import Install, hint_lines

    assert hint_lines("1.4.0", "1.5.0", Install("editable", None)) == []


# --- opt-out ---------------------------------------------------------------


def test_check_enabled_follows_the_global_config() -> None:
    from jailbee.update_check import check_enabled

    assert check_enabled(configured=True, env={}) is True
    assert check_enabled(configured=False, env={}) is False


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_env_var_disables_the_check_even_when_the_config_enables_it(value: str) -> None:
    """Scripts and CI need an off switch that does not edit the user's file."""
    from jailbee.update_check import ENV_DISABLE, check_enabled

    assert check_enabled(configured=True, env={ENV_DISABLE: value}) is False


@pytest.mark.parametrize("value", ["0", ""])
def test_env_var_set_to_a_falsey_value_leaves_the_check_on(value: str) -> None:
    from jailbee.update_check import ENV_DISABLE, check_enabled

    assert check_enabled(configured=True, env={ENV_DISABLE: value}) is True


# --- cache staleness -------------------------------------------------------


def test_needs_probe_on_a_database_that_has_never_been_checked(db_session: Session) -> None:
    from jailbee.update_check import needs_probe

    assert needs_probe(db_session, now=NOW) is True


def test_needs_probe_is_false_right_after_a_check(db_session: Session) -> None:
    from jailbee.update_check import needs_probe, record_check

    record_check(db_session, "1.5.0", now=NOW)

    assert needs_probe(db_session, now=NOW + timedelta(hours=1)) is False


def test_needs_probe_again_once_the_cache_has_expired(db_session: Session) -> None:
    from jailbee.update_check import CACHE_TTL, needs_probe, record_check

    record_check(db_session, "1.5.0", now=NOW)

    assert needs_probe(db_session, now=NOW + CACHE_TTL) is True


def test_a_failed_fetch_still_stamps_the_check(db_session: Session) -> None:
    """Otherwise an offline host spawns a probe on every single command."""
    from jailbee.update_check import needs_probe, record_check

    record_check(db_session, None, now=NOW)

    assert needs_probe(db_session, now=NOW + timedelta(hours=1)) is False


def test_a_failed_fetch_keeps_the_last_known_version(db_session: Session) -> None:
    from jailbee.update_check import Install, consume_hint, record_check

    record_check(db_session, "1.5.0", now=NOW)
    record_check(db_session, None, now=NOW + timedelta(days=1))

    lines = consume_hint(
        db_session, "1.4.0", now=NOW + timedelta(days=1), install=Install("uv", "cmd")
    )
    assert lines != []


# --- the hint read path ----------------------------------------------------


def test_no_hint_before_anything_has_been_fetched(db_session: Session) -> None:
    from jailbee.update_check import Install, consume_hint

    assert consume_hint(db_session, "1.4.0", now=NOW, install=Install("uv", "cmd")) == []


def test_a_cached_newer_release_produces_a_hint(db_session: Session) -> None:
    from jailbee.update_check import Install, consume_hint, record_check

    record_check(db_session, "1.5.0", now=NOW)

    lines = consume_hint(db_session, "1.4.0", now=NOW, install=Install("uv", "cmd"))
    assert any("1.5.0" in line for line in lines)


def test_the_same_release_is_not_advertised_twice_in_a_row(db_session: Session) -> None:
    """`jailbee ls` runs many times a day; one line per day per release is
    advice, one line per invocation is nagging."""
    from jailbee.update_check import Install, consume_hint, record_check

    record_check(db_session, "1.5.0", now=NOW)
    consume_hint(db_session, "1.4.0", now=NOW, install=Install("uv", "cmd"))

    again = consume_hint(
        db_session, "1.4.0", now=NOW + timedelta(hours=1), install=Install("uv", "cmd")
    )
    assert again == []


def test_the_hint_returns_once_the_interval_has_passed(db_session: Session) -> None:
    from jailbee.update_check import HINT_INTERVAL, Install, consume_hint, record_check

    record_check(db_session, "1.5.0", now=NOW)
    consume_hint(db_session, "1.4.0", now=NOW, install=Install("uv", "cmd"))

    again = consume_hint(db_session, "1.4.0", now=NOW + HINT_INTERVAL, install=Install("uv", "cmd"))
    assert again != []


def test_a_newer_release_is_advertised_without_waiting_out_the_interval(
    db_session: Session,
) -> None:
    from jailbee.update_check import Install, consume_hint, record_check

    record_check(db_session, "1.5.0", now=NOW)
    consume_hint(db_session, "1.4.0", now=NOW, install=Install("uv", "cmd"))
    record_check(db_session, "1.6.0", now=NOW + timedelta(hours=1))

    lines = consume_hint(
        db_session, "1.4.0", now=NOW + timedelta(hours=1), install=Install("uv", "cmd")
    )
    assert any("1.6.0" in line for line in lines)


def test_an_editable_install_is_never_advertised_to(db_session: Session) -> None:
    from jailbee.update_check import Install, consume_hint, record_check

    record_check(db_session, "1.5.0", now=NOW)

    assert consume_hint(db_session, "1.4.0", now=NOW, install=Install("editable", None)) == []


def test_the_hint_stops_once_the_user_has_upgraded(db_session: Session) -> None:
    from jailbee.update_check import Install, consume_hint, record_check

    record_check(db_session, "1.5.0", now=NOW)

    assert consume_hint(db_session, "1.5.0", now=NOW, install=Install("uv", "cmd")) == []


# --- fetching --------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_fetch_latest_reads_the_version_from_the_pypi_payload(mocker) -> None:
    from jailbee import update_check

    payload = json.dumps({"info": {"version": "1.5.0"}}).encode()
    mocker.patch.object(update_check, "urlopen", return_value=_FakeResponse(payload))

    assert update_check.fetch_latest() == "1.5.0"


@pytest.mark.parametrize(
    "payload",
    [b"not json", b"{}", json.dumps({"info": {}}).encode()],
)
def test_fetch_latest_returns_none_on_an_unusable_payload(mocker, payload: bytes) -> None:
    from jailbee import update_check

    mocker.patch.object(update_check, "urlopen", return_value=_FakeResponse(payload))

    assert update_check.fetch_latest() is None


def test_fetch_latest_returns_none_when_the_network_fails(mocker) -> None:
    """An offline host must get silence, not a traceback."""
    from urllib.error import URLError

    from jailbee import update_check

    mocker.patch.object(update_check, "urlopen", side_effect=URLError("no route to host"))

    assert update_check.fetch_latest() is None


# --- the probe -------------------------------------------------------------


def test_probe_argv_runs_this_interpreter_on_the_probe_module() -> None:
    import sys

    from jailbee.update_check import probe_argv

    assert probe_argv() == [sys.executable, "-m", "jailbee.update_check"]


def test_maybe_probe_spawns_when_the_cache_is_stale(db_session: Session) -> None:
    from jailbee.update_check import maybe_probe

    spawned: list[bool] = []
    maybe_probe(db_session, now=NOW, enabled=True, spawn=lambda: spawned.append(True))

    assert spawned == [True]


def test_maybe_probe_stays_quiet_while_the_cache_is_fresh(db_session: Session) -> None:
    from jailbee.update_check import maybe_probe, record_check

    record_check(db_session, "1.5.0", now=NOW)

    spawned: list[bool] = []
    maybe_probe(
        db_session,
        now=NOW + timedelta(hours=1),
        enabled=True,
        spawn=lambda: spawned.append(True),
    )

    assert spawned == []


def test_maybe_probe_does_nothing_when_the_check_is_disabled(db_session: Session) -> None:
    from jailbee.update_check import maybe_probe

    spawned: list[bool] = []
    maybe_probe(db_session, now=NOW, enabled=False, spawn=lambda: spawned.append(True))

    assert spawned == []


def test_run_probe_records_what_it_fetched(db_engine: Engine, mocker) -> None:
    from jailbee import update_check

    mocker.patch.object(update_check, "get_engine", return_value=db_engine)
    mocker.patch.object(update_check, "fetch_latest", return_value="1.5.0")
    mocker.patch.object(update_check, "_configured_enabled", return_value=True)

    update_check.run_probe(now=NOW)

    with Session(db_engine) as session:
        assert update_check.needs_probe(session, now=NOW) is False
        lines = update_check.consume_hint(
            session, "1.4.0", now=NOW, install=update_check.Install("uv", "cmd")
        )
    assert any("1.5.0" in line for line in lines)


def test_run_probe_fetches_nothing_when_the_check_is_disabled(db_engine: Engine, mocker) -> None:
    """The opt-out has to hold in the probe too — it is a separate process
    and reads the config itself."""
    from jailbee import update_check

    mocker.patch.object(update_check, "get_engine", return_value=db_engine)
    fetch = mocker.patch.object(update_check, "fetch_latest", return_value="1.5.0")
    mocker.patch.object(update_check, "_configured_enabled", return_value=False)

    update_check.run_probe(now=NOW)

    fetch.assert_not_called()
