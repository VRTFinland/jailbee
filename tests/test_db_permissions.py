"""The state database holds secrets (web TOTP keys): only its owner may read it."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from jailbee import db


def mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path / "jailbee"


def test_a_new_state_database_is_private_to_its_owner(state_home: Path) -> None:
    previous = os.umask(0o022)
    try:
        db.get_engine()
    finally:
        os.umask(previous)

    assert mode(state_home) == 0o700
    assert mode(state_home / "state.sqlite") == 0o600
    for suffix in ("-wal", "-shm"):
        sidecar = state_home / f"state.sqlite{suffix}"
        if sidecar.exists():
            assert mode(sidecar) == 0o600


def test_an_existing_loose_state_database_is_tightened_on_open(state_home: Path) -> None:
    state_home.mkdir(mode=0o755)
    state_home.chmod(0o755)
    for name in ("state.sqlite", "state.sqlite-wal", "state.sqlite-shm"):
        (state_home / name).touch()
        (state_home / name).chmod(0o644)

    db.get_engine()

    assert mode(state_home) == 0o700
    for name in ("state.sqlite", "state.sqlite-wal", "state.sqlite-shm"):
        assert mode(state_home / name) == 0o600
