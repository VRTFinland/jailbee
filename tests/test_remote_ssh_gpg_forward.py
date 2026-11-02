"""Filesystem routing must preserve host sockets and select live forwards."""

import os
import stat

import pytest

from jailbee.remote_ssh import gpg_forward as gpg


@pytest.fixture(autouse=True)
def state_directory(mocker, tmp_path):
    mocker.patch("jailbee.db.state_dir", return_value=tmp_path)


def agent_target():
    return os.readlink(gpg.switch_dir() / "S.gpg-agent")


def test_ensure_creates_host_links():
    directory = gpg.ensure_switch_dir()
    for name in gpg.SOCKET_NAMES:
        assert os.readlink(directory / name) == f"/run/jailbee-gpg/host/{name}"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(gpg.remote_dir().stat().st_mode) == 0o700


def test_ensure_keeps_existing_agent_link():
    gpg.point_agent("ab12")
    gpg.ensure_switch_dir()
    assert agent_target() == "/run/jailbee-gpg/remote/ab12.sock"


def test_add_points_newest():
    forwards = gpg.GpgForwards()
    forwards.add("a")
    forwards.add("b")
    assert agent_target() == "/run/jailbee-gpg/remote/b.sock"
    assert forwards.current() == "b"


@pytest.mark.parametrize(
    ("removed", "expected"),
    [("b", "/run/jailbee-gpg/remote/a.sock"), ("a", "/run/jailbee-gpg/remote/b.sock")],
)
def test_remove_falls_back_to_newest_live(removed, expected):
    forwards = gpg.GpgForwards()
    forwards.add("a")
    forwards.add("b")
    forwards.remove(removed)
    assert agent_target() == expected


def test_remove_last_falls_back_to_host():
    forwards = gpg.GpgForwards()
    forwards.add("a")
    forwards.remove("a")
    assert agent_target() == "/run/jailbee-gpg/host/S.gpg-agent"
    assert forwards.current() is None


def test_remove_unlinks_socket_file():
    forwards = gpg.GpgForwards()
    forwards.add("a")
    socket = gpg.remote_socket("a")
    socket.touch()
    forwards.remove("a")
    assert not socket.exists()


def test_remove_missing_socket_ok():
    forwards = gpg.GpgForwards()
    forwards.remove("missing")
    assert agent_target() == "/run/jailbee-gpg/host/S.gpg-agent"


def test_reset_points_host_and_clears_stale():
    forwards = gpg.GpgForwards()
    forwards.add("dead")
    gpg.remote_socket("dead").touch()
    forwards.reset()
    assert agent_target() == "/run/jailbee-gpg/host/S.gpg-agent"
    assert list(gpg.remote_dir().glob("*.sock")) == []
    assert forwards.current() is None


def test_ssh_link_always_host():
    forwards = gpg.GpgForwards()
    forwards.add("a")
    assert (
        os.readlink(gpg.switch_dir() / "S.gpg-agent.ssh") == "/run/jailbee-gpg/host/S.gpg-agent.ssh"
    )
