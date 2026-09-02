"""Tests for runtime_mounts: attaching GUI/IPC sockets after logind boot."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jailbee.config import load_config
from jailbee.incus import IncusError
from jailbee.runtime_mounts import (
    GPG_DEVICES,
    SOCKET_DEVICES,
    WAYLAND_DEVICE,
    attach_runtime_devices,
    detach_runtime_devices,
)
from tests.conftest import make_cfg

FIXTURES = Path(__file__).parent / "fixtures"


def _cfg():
    return load_config(FIXTURES / "full_config.yaml")


@pytest.fixture
def wayland_session(monkeypatch, mocker):
    """A Wayland host whose compositor socket is live on disk.

    Both halves matter: the socket *name* comes from ``$WAYLAND_DISPLAY``,
    and the host path built from it is checked for existence before being
    handed to Incus.
    """
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    mocker.patch("jailbee.runtime_mounts._host_path_exists", return_value=True)


def test_attach_polls_until_logind_provisions_dir(wayland_session):
    """attach_runtime_devices must wait for /run/user/<uid> to be owned
    by the dev UID before adding devices — otherwise the bind mounts
    land on Incus's auto-created (root-owned) tmpfs and get shadowed.
    """
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


def test_attach_adds_all_four_socket_devices_on_wayland(wayland_session):
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
    assert set(added.keys()) == set(SOCKET_DEVICES)
    runtime = f"/run/user/{uid}"
    assert added["wayland-socket"]["source"] == f"{runtime}/wayland-0"
    assert added["pulse-socket"]["source"] == f"{runtime}/pulse"
    assert added["dbus-socket"]["source"] == f"{runtime}/bus"
    assert added["gpg-socket"]["source"] == f"{runtime}/gnupg"


def test_attach_mounts_the_socket_wayland_display_names(monkeypatch, mocker):
    """Hyprland and Sway commonly export ``WAYLAND_DISPLAY=wayland-1`` and
    have no ``/run/user/<uid>/wayland-0`` at all. Bind-mounting the
    hardcoded name made Incus reject the device add and `jailbee new`
    exit 1 (#17), on a host jailbee had just decided *was* Wayland — from
    the very variable it then ignored.
    """
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    mocker.patch("jailbee.runtime_mounts._host_path_exists", return_value=True)
    cfg = _cfg()
    uid = cfg.container_user.uid
    incus = MagicMock()
    incus.exec.return_value = f"{uid}\n"

    attach_runtime_devices(
        cfg, incus, "feat-smoke", timeout_s=1.0, poll_interval_s=0.01, sleep_fn=MagicMock()
    )

    added = {c.args[1]: c.args[3] for c in incus.config_device_add.call_args_list}
    assert added["wayland-socket"]["source"] == f"/run/user/{uid}/wayland-1"
    assert added["wayland-socket"]["path"] == f"/run/user/{uid}/wayland-1"


def test_attach_skips_the_wayland_socket_when_the_host_socket_is_absent(monkeypatch, mocker):
    """``$WAYLAND_DISPLAY`` can name a socket that isn't there: a stale value
    inherited by a long-lived tmux or ssh environment after the compositor
    restarted, or an absolute path (which the Wayland spec allows) that is
    not under ``/run/user/<uid>``. Incus rejects such a device add, which
    killed the whole create; the compositor socket is optional, so skip it
    the way an X11 host already does.
    """
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-9")
    exists = mocker.patch("jailbee.runtime_mounts._host_path_exists", return_value=False)
    warn = mocker.patch("jailbee.runtime_mounts.warn")
    cfg = _cfg()
    uid = cfg.container_user.uid
    incus = MagicMock()
    incus.exec.return_value = f"{uid}\n"

    ok = attach_runtime_devices(
        cfg, incus, "feat-smoke", timeout_s=1.0, poll_interval_s=0.01, sleep_fn=MagicMock()
    )

    assert ok is True
    added = {c.args[1] for c in incus.config_device_add.call_args_list}
    assert added == set(SOCKET_DEVICES) - {WAYLAND_DEVICE}
    exists.assert_called_once_with(f"/run/user/{uid}/wayland-9")
    message = warn.call_args[0][0]
    assert f"/run/user/{uid}/wayland-9" in message
    assert "will not display" in message


def test_attach_does_not_stat_a_socket_path_off_wayland(monkeypatch, mocker):
    """On an X11 host there is no socket name to build a path from, so the
    session check must settle it before the filesystem is consulted —
    otherwise the skip reason reported is the wrong one.
    """
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    exists = mocker.patch("jailbee.runtime_mounts._host_path_exists", return_value=False)
    cfg = _cfg()
    incus = MagicMock()
    incus.exec.return_value = f"{cfg.container_user.uid}\n"

    attach_runtime_devices(
        cfg, incus, "feat-smoke", timeout_s=1.0, poll_interval_s=0.01, sleep_fn=MagicMock()
    )

    exists.assert_not_called()


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
    assert added == set(SOCKET_DEVICES) - {WAYLAND_DEVICE}
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


def test_attach_skips_gpg_socket_when_gpg_disabled(wayland_session, tmp_path):
    """`gpg.enabled: false` must keep the host gpg-agent socket dir out
    of the container. Such a host may not run a gpg-agent at all, in
    which case /run/user/<uid>/gnupg doesn't exist and Incus rejects the
    device add — breaking `jailbee start` for everyone who opted out.
    """
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


def test_attach_adds_gpg_socket_when_gpg_enabled(wayland_session, tmp_path):
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
    assert removed == set(SOCKET_DEVICES)


def test_attach_returns_false_on_logind_timeout(wayland_session):
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


def test_attach_tolerates_already_attached_devices(wayland_session):
    """Re-running attach (e.g. after `gie start` on an already-running
    container) must not fail when devices are already attached.
    """
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


def test_attach_reraises_other_device_add_errors(wayland_session):
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


def test_attach_treats_exec_error_as_not_ready_yet(wayland_session):
    """During very early boot, `incus exec` may itself fail (container
    not fully up). Treat that the same as 'logind hasn't provisioned
    yet' and keep polling.
    """
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
    assert set(removed) == set(SOCKET_DEVICES)


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


def test_attach_mounts_the_shared_socket_dirs_read_only(wayland_session):
    """The gnupg and pulse devices are *directory* mounts straight into the
    host's own /run/user/<uid>, and the golden image enables linger — so the
    container runs its own `systemd --user`. Its `gpg-agent.socket` /
    `pulseaudio.socket` unlink and re-create the socket files they listen on,
    which kills the host's agent ("socket file has been removed - shutting
    down") and leaves the container's agent — with no smartcard access, so no
    YubiKey keys for anyone — answering on the shared socket.

    Read-only makes that unlink EROFS. Connecting to a unix socket needs no
    writable filesystem, so agent forwarding is unaffected, and the host stays
    free to re-create its sockets: the mount is read-only from the container's
    side only.
    """
    cfg = _cfg()
    uid = cfg.container_user.uid
    incus = MagicMock()
    incus.exec.return_value = f"{uid}\n"

    attach_runtime_devices(
        cfg,
        incus,
        "feat-smoke",
        timeout_s=1.0,
        poll_interval_s=0.01,
        sleep_fn=MagicMock(),
    )

    added = {c.args[1]: c.args[3] for c in incus.config_device_add.call_args_list}
    assert added["gpg-socket"]["readonly"] == "true"
    assert added["pulse-socket"]["readonly"] == "true"
    # wayland-0 and bus are single-file mounts: unlinking a bind-mount point
    # returns EBUSY, so the container cannot clobber them and a read-only
    # flag would only risk breaking a writer we haven't thought of.
    assert "readonly" not in added["wayland-socket"]
    assert "readonly" not in added["dbus-socket"]


# ---- WAYLAND_DISPLAY, pinned per boot next to the mount ----
#
# The base profile carries an apply-time snapshot of the socket name. The
# mount is recomputed on every boot, so after a reboot that renumbers the
# compositor socket the two disagree: the container gets the right mount and
# the wrong variable, until someone happens to run `jailbee apply`. These
# pin the variable where the mount is decided.


def test_attach_pins_the_containers_wayland_display_to_the_mounted_socket(monkeypatch, mocker):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    mocker.patch("jailbee.runtime_mounts._host_path_exists", return_value=True)
    cfg = _cfg()
    incus = MagicMock()
    incus.exec.return_value = f"{cfg.container_user.uid}\n"

    attach_runtime_devices(
        cfg, incus, "feat-smoke", timeout_s=1.0, poll_interval_s=0.01, sleep_fn=MagicMock()
    )

    incus.config_set.assert_called_once_with(
        "feat-smoke", "environment.WAYLAND_DISPLAY", "wayland-1"
    )
    incus.config_unset.assert_not_called()


def test_attach_clears_the_containers_wayland_display_when_no_socket_is_mounted(
    monkeypatch, mocker
):
    """Nothing was mounted, so the container must not keep a per-boot value
    from the session that did have a socket — that would name a path which
    is no longer in the container at all.
    """
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    mocker.patch("jailbee.runtime_mounts.warn")
    cfg = _cfg()
    incus = MagicMock()
    incus.exec.return_value = f"{cfg.container_user.uid}\n"

    attach_runtime_devices(
        cfg, incus, "feat-smoke", timeout_s=1.0, poll_interval_s=0.01, sleep_fn=MagicMock()
    )

    incus.config_unset.assert_called_once_with("feat-smoke", "environment.WAYLAND_DISPLAY")
    incus.config_set.assert_not_called()


def test_attach_leaves_a_wayland_display_pinned_in_config_alone(monkeypatch, mocker):
    """`container.env` overriding a key jailbee sets itself is documented to
    win. A per-container override outranks the profile the user's value is
    rendered into, so writing one here would silently beat their config —
    clear it instead and let the profile speak.
    """
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    mocker.patch("jailbee.runtime_mounts._host_path_exists", return_value=True)
    cfg = _cfg()
    cfg = cfg.model_copy(
        update={
            "container": cfg.container.model_copy(update={"env": {"WAYLAND_DISPLAY": "wayland-7"}})
        }
    )
    incus = MagicMock()
    incus.exec.return_value = f"{cfg.container_user.uid}\n"

    attach_runtime_devices(
        cfg, incus, "feat-smoke", timeout_s=1.0, poll_interval_s=0.01, sleep_fn=MagicMock()
    )

    incus.config_set.assert_not_called()
    incus.config_unset.assert_called_once_with("feat-smoke", "environment.WAYLAND_DISPLAY")


def test_detach_clears_the_containers_wayland_display():
    """Detach undoes what attach did, so a boot whose attach never gets to
    run (logind timeout) starts from the profile's value, not the last
    session's.
    """
    cfg = _cfg()
    incus = MagicMock()

    detach_runtime_devices(cfg, incus, "feat-smoke")

    incus.config_unset.assert_called_once_with("feat-smoke", "environment.WAYLAND_DISPLAY")
