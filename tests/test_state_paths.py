from __future__ import annotations

import os
from pathlib import Path

import pytest

from jailbee.state_service import StateServiceError, paths


def test_runtime_dir_lives_under_xdg_runtime_dir(runtime_dir):
    assert paths.runtime_dir() == runtime_dir
    assert paths.socket_path() == runtime_dir / "state.sock"
    assert paths.lock_path().parent == paths.spawn_lock_path().parent == runtime_dir


def test_runtime_dir_falls_back_to_a_per_user_temp_dir(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(paths.tempfile, "gettempdir", lambda: "/tmp")
    assert paths.runtime_dir() == Path(f"/tmp/jailbee-{os.getuid()}")


def test_ensure_runtime_dir_creates_it_private(runtime_dir):
    assert paths.ensure_runtime_dir() == runtime_dir
    assert runtime_dir.stat().st_mode & 0o777 == 0o700


def test_ensure_runtime_dir_refuses_a_shared_dir(runtime_dir):
    runtime_dir.mkdir(mode=0o755)
    runtime_dir.chmod(0o755)
    with pytest.raises(StateServiceError, match="private"):
        paths.ensure_runtime_dir()


def test_socket_path_fits_sun_path(runtime_dir):
    assert len(os.fsencode(paths.socket_path())) < 104


def test_log_path_is_under_the_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert paths.log_path() == tmp_path / "jailbee" / "state-service.log"
