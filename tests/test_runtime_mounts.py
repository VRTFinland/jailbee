"""Tests for runtime_mounts: attaching GUI/IPC sockets after logind boot."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from jailbee.config import load_config
from jailbee.incus import IncusError
from jailbee.runtime_mounts import (
    GPG_DEVICES,
    SOCKET_DEVICES,
    WAYLAND_ONLY_DEVICES,
    attach_runtime_devices,
    detach_runtime_devices,
)
from tests.conftest import make_cfg

FIXTURES = Path(__file__).parent / "fixtures"


def _cfg():
    return load_config(FIXTURES / "full_config.yaml")


def test_attach_polls_until_logind_provisions_dir(monkeypatch):
    """attach_runtime_devices must wait for /run/user/<uid> to be owned
    by the dev UID before adding devices — otherwise the bind mounts
    land on Incus's auto-created (root-owned) tmpfs and get shadowed.
    """
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    cfg = _cfg()
    incus = MagicMock()
    # First two stat calls return root (logind not done yet), third returns dev UID
    incus.exec.side_effect = ["0\n", "0\n", f"{cfg.container_user.uid}\n"]
    sleep_fn = MagicMock()

    ok = attach_runtime_devices(
        cfg,
        incus,
        "feat-smoke",
        timeout_s=10.0,
        poll_interval_s=0.01,
        sleep_fn=sleep_fn,
    )

    assert ok is True
    assert incus.exec.call_count == 3
    assert sleep_fn.call_count == 2  # slept twice between polls


def test_attach_adds_all_four_socket_devices_on_wayland(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    cfg = _cfg()
    uid = cfg.container_user.uid
    incus = MagicMock()
    incus.exec.return_value = f"{uid}\n"
    sleep_fn = MagicMock()

    attach_runtime_devices(
        cfg,
        incus,
        "feat-smoke",
        timeout_s=1.0,
        poll_interval_s=0.01,
        sleep_fn=sleep_fn,
    )

    added = {c.args[1]: c.args[3] for c in incus.config_device_add.call_args_list}
    assert set(added.keys()) == set(SOCKET_DEVICES.keys())
    runtime = f"/run/user/{uid}"
    assert added["wayland-socket"]["source"] == f"{runtime}/wayland-0"
    assert added["pulse-socket"]["source"] == f"{runtime}/pulse"
    assert added["dbus-socket"]["source"] == f"{runtime}/bus"
    assert added["gpg-socket"]["source"] == f"{runtime}/gnupg"


def test_attach_skips_wayland_socket_on_x11_host(monkeypatch):
    """On X11 hosts /run/user/<uid>/wayland-0 doesn't exist and Incus
    rejects the device add. The wayland-only sockets must be skipped so
    `gie new` completes; pulse/dbus/gpg still get attached.
    """
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    cfg = _cfg()
    uid = cfg.container_user.uid
    incus = MagicMock()
    incus.exec.return_value = f"{uid}\n"
    sleep_fn = MagicMock()

    ok = attach_runtime_devices(
        cfg,
        incus,
        "feat-smoke",
        timeout_s=1.0,
        poll_interval_s=0.01,
        sleep_fn=sleep_fn,
    )

    assert ok is True
    added = {c.args[1] for c in incus.config_device_add.call_args_list}
    assert added == set(SOCKET_DEVICES) - WAYLAND_ONLY_DEVICES
    assert "wayland-socket" not in added


def test_attach_warns_that_gui_launches_will_not_display_off_wayland(monkeypatch, mocker):
    """Skipping the display socket is not a neutral note. The old message
    said only 'skipped on X11 host', which reads as a detail rather than as
    'the thing you attached these for will not work'."""
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    warn = mocker.patch("jailbee.runtime_mounts.warn")
    info = mocker.patch("jailbee.runtime_mounts.info")
    cfg = _cfg()
    incus = MagicMock()
    incus.exec.return_value = f"{cfg.container_user.uid}\n"

    attach_runtime_devices(
        cfg, incus, "feat-smoke", timeout_s=1.0, poll_interval_s=0.01, sleep_fn=MagicMock()
    )

    info.assert_not_called()
    message = warn.call_args[0][0]
    assert "wayland-socket" in message
    assert "will not display" in message


def test_attach_reports_a_config_skip_as_a_note_next_to_the_display_warning(
    monkeypatch, mocker, tmp_path
):
    """Two different reasons for a missing device must not be conflated:
    the absent display is a problem, `gpg.enabled: false` is a choice. The
    gpg skip must still be reported when the warning fires too — folding
    it into the Wayland message would blame the session for it.
    """
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    warn = mocker.patch("jailbee.runtime_mounts.warn")
    info = mocker.patch("jailbee.runtime_mounts.info")
    cfg = make_cfg(tmp_path, gpg={"enabled": False})
    incus = MagicMock()
    incus.exec.return_value = f"{cfg.container_user.uid}\n"

    attach_runtime_devices(
        cfg, incus, "feat-smoke", timeout_s=1.0, poll_interval_s=0.01, sleep_fn=MagicMock()
    )

    warning = warn.call_args[0][0]
    assert "wayland-socket" in warning
    assert "gpg-socket" not in warning
    note = info.call_args[0][0]
    assert "gpg-socket" in note
    assert "config" in note


def test_attach_skips_gpg_socket_when_gpg_disabled(monkeypatch, tmp_path):
    """`gpg.enabled: false` must keep the host gpg-agent socket dir out
    of the container. Such a host may not run a gpg-agent at all, in
    which case /run/user/<uid>/gnupg doesn't exist and Incus rejects the
    device add — breaking `jailbee start` for everyone who opted out.
    """
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    cfg = make_cfg(tmp_path, gpg={"enabled": False})
    incus = MagicMock()
    incus.exec.return_value = f"{cfg.container_user.uid}\n"
    sleep_fn = MagicMock()

    ok = attach_runtime_devices(
        cfg,
        incus,
        "feat-smoke",
        timeout_s=1.0,
        poll_interval_s=0.01,
        sleep_fn=sleep_fn,
    )

    assert ok is True
    added = {c.args[1] for c in incus.config_device_add.call_args_list}
    assert "gpg-socket" not in added
    assert added == set(SOCKET_DEVICES) - GPG_DEVICES


def test_attach_adds_gpg_socket_when_gpg_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    cfg = make_cfg(tmp_path, gpg={"enabled": True})
    incus = MagicMock()
    incus.exec.return_value = f"{cfg.container_user.uid}\n"
    sleep_fn = MagicMock()

    attach_runtime_devices(
        cfg,
        incus,
        "feat-smoke",
        timeout_s=1.0,
        poll_interval_s=0.01,
        sleep_fn=sleep_fn,
    )

    added = {c.args[1] for c in incus.config_device_add.call_args_list}
    assert "gpg-socket" in added


def test_detach_removes_gpg_socket_even_when_gpg_disabled(tmp_path):
    """Detach stays unconditional so that flipping gpg.enabled to false
    and restarting actually drops a previously attached gpg-socket.
    """
    cfg = make_cfg(tmp_path, gpg={"enabled": False})
    incus = MagicMock()

    detach_runtime_devices(cfg, incus, "feat-smoke")

    removed = {c.args[1] for c in incus.config_device_remove.call_args_list}
    assert removed == set(SOCKET_DEVICES.keys())


def test_attach_returns_false_on_logind_timeout(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    cfg = _cfg()
    incus = MagicMock()
    incus.exec.return_value = "0\n"  # never becomes dev UID
    sleep_fn = MagicMock()

    ok = attach_runtime_devices(
        cfg,
        incus,
        "feat-smoke",
        timeout_s=0.05,
        poll_interval_s=0.01,
        sleep_fn=sleep_fn,
    )

    assert ok is False
    incus.config_device_add.assert_not_called()


def test_attach_tolerates_already_attached_devices(monkeypatch):
    """Re-running attach (e.g. after `gie start` on an already-running
    container) must not fail when devices are already attached.
    """
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    cfg = _cfg()
    incus = MagicMock()
    incus.exec.return_value = f"{cfg.container_user.uid}\n"
    incus.config_device_add.side_effect = IncusError('Device "wayland-socket" already exists')
    sleep_fn = MagicMock()

    ok = attach_runtime_devices(
        cfg,
        incus,
        "feat-smoke",
        timeout_s=1.0,
        poll_interval_s=0.01,
        sleep_fn=sleep_fn,
    )

    assert ok is True
    assert incus.config_device_add.call_count == len(SOCKET_DEVICES)


def test_attach_reraises_other_device_add_errors(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    cfg = _cfg()
    incus = MagicMock()
    incus.exec.return_value = f"{cfg.container_user.uid}\n"
    incus.config_device_add.side_effect = IncusError("Error: invalid device source")
    sleep_fn = MagicMock()

    try:
        attach_runtime_devices(
            cfg,
            incus,
            "feat-smoke",
            timeout_s=1.0,
            poll_interval_s=0.01,
            sleep_fn=sleep_fn,
        )
    except IncusError as e:
        assert "invalid device source" in str(e)
    else:
        raise AssertionError("Expected IncusError to propagate")


def test_attach_treats_exec_error_as_not_ready_yet(monkeypatch):
    """During very early boot, `incus exec` may itself fail (container
    not fully up). Treat that the same as 'logind hasn't provisioned
    yet' and keep polling.
    """
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    cfg = _cfg()
    incus = MagicMock()
    incus.exec.side_effect = [
        IncusError("not running"),
        f"{cfg.container_user.uid}\n",
    ]
    sleep_fn = MagicMock()

    ok = attach_runtime_devices(
        cfg,
        incus,
        "feat-smoke",
        timeout_s=1.0,
        poll_interval_s=0.01,
        sleep_fn=sleep_fn,
    )

    assert ok is True


def test_detach_removes_all_four_socket_devices():
    cfg = _cfg()
    incus = MagicMock()

    detach_runtime_devices(cfg, incus, "feat-smoke")

    removed = [c.args[1] for c in incus.config_device_remove.call_args_list]
    assert set(removed) == set(SOCKET_DEVICES.keys())


def test_detach_tolerates_devices_that_dont_exist():
    cfg = _cfg()
    incus = MagicMock()
    incus.config_device_remove.side_effect = IncusError("Error: Device doesn't exist")

    # Should not raise.
    detach_runtime_devices(cfg, incus, "feat-smoke")

    assert incus.config_device_remove.call_count == len(SOCKET_DEVICES)


def test_detach_reraises_other_errors():
    cfg = _cfg()
    incus = MagicMock()
    incus.config_device_remove.side_effect = IncusError("Error: container is being deleted")

    try:
        detach_runtime_devices(cfg, incus, "feat-smoke")
    except IncusError as e:
        assert "being deleted" in str(e)
    else:
        raise AssertionError("Expected IncusError to propagate")
