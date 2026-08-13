"""Tests for systemd unit installation in jailbee init.

Each test takes the ``private_home`` fixture: install_systemd_units()
writes into ``$HOME/.config/systemd/user`` for real, so it needs a home
of its own rather than the session-wide one.
"""

from __future__ import annotations

from pathlib import Path

from pytest_mock import MockerFixture


def test_install_writes_units_to_xdg_config(
    private_home: Path,
    mocker: MockerFixture,
) -> None:
    from jailbee.init_command import install_systemd_units

    mocker.patch("shutil.which", return_value="/usr/local/bin/jailbee")
    run_mock = mocker.patch("subprocess.run")

    install_systemd_units()

    units_dir = private_home / ".config" / "systemd" / "user"
    service = (units_dir / "jailbee-net-refresh.service").read_text()
    timer = (units_dir / "jailbee-net-refresh.timer").read_text()
    assert "ExecStart=/usr/local/bin/jailbee net refresh" in service
    assert "OnUnitActiveSec=60s" in timer

    cmds = [list(c.args[0]) for c in run_mock.call_args_list]
    assert any("daemon-reload" in c for c in cmds)
    assert any("enable" in c and "--now" in c for c in cmds)


def test_install_skips_when_jailbee_not_on_path(
    private_home: Path,
    mocker: MockerFixture,
) -> None:
    from jailbee.init_command import install_systemd_units

    mocker.patch("shutil.which", return_value=None)
    run_mock = mocker.patch("subprocess.run")

    install_systemd_units()

    assert not (
        private_home / ".config" / "systemd" / "user" / "jailbee-net-refresh.timer"
    ).exists()
    run_mock.assert_not_called()


def test_install_idempotent_skips_daemon_reload_when_unchanged(
    private_home: Path,
    mocker: MockerFixture,
) -> None:
    _ = private_home  # units are written under it; only the calls matter here
    from jailbee.init_command import install_systemd_units

    mocker.patch("shutil.which", return_value="/usr/local/bin/jailbee")
    mocker.patch("subprocess.run")

    install_systemd_units()  # first install: writes
    run_mock = mocker.patch("subprocess.run")
    install_systemd_units()  # second install: same content

    cmds = [list(c.args[0]) for c in run_mock.call_args_list]
    # daemon-reload only if files changed
    assert not any("daemon-reload" in c for c in cmds)
    # enable --now still called (idempotent)
    assert any("enable" in c and "--now" in c for c in cmds)


def test_install_systemd_units_writes_jailbee_units(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    from jailbee.init_command import install_systemd_units

    mocker.patch("shutil.which", return_value="/usr/bin/jailbee")
    mocker.patch("pathlib.Path.home", return_value=tmp_path)
    run = mocker.patch("subprocess.run")

    install_systemd_units()

    units = tmp_path / ".config" / "systemd" / "user"
    assert (units / "jailbee-net-refresh.service").is_file()
    assert (units / "jailbee-net-refresh.timer").is_file()
    assert "/usr/bin/jailbee" in (units / "jailbee-net-refresh.service").read_text()
    assert any("jailbee-net-refresh.timer" in str(c.args) for c in run.call_args_list)
