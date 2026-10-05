"""The shared display container, the client wait and the connection recipe."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import yaml

from jailbee import remote_display as rd
from jailbee.incus import IncusError


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def _running(name=rd.DISPLAY_CONTAINER):
    return [{"name": name, "status": "Running"}]


def test_profile_uses_the_clients_idmap_and_the_loose_bridge():
    profile = yaml.safe_load(rd._display_profile_yaml(1234, 5678))

    assert profile["config"]["raw.idmap"] == "uid 1234 1234\ngid 5678 5678"
    # Without nesting, systemd 256+ hangs at (sd-mkuserns): no DHCP, no DNS.
    assert profile["config"]["security.nesting"] == "true"
    assert profile["devices"]["eth0"]["network"] == "jailbee-loose"


def test_status_missing_stopped_running_degraded():
    incus = MagicMock()
    incus.list_containers.return_value = []
    assert rd.display_status(incus) is rd.DisplayStatus.MISSING

    incus.list_containers.return_value = [{"name": rd.DISPLAY_CONTAINER, "status": "Stopped"}]
    assert rd.display_status(incus) is rd.DisplayStatus.STOPPED

    incus.list_containers.return_value = _running()
    incus.exec.return_value = "active\n"
    assert rd.display_status(incus) is rd.DisplayStatus.RUNNING

    incus.exec.side_effect = IncusError("inactive")
    assert rd.display_status(incus) is rd.DisplayStatus.DEGRADED


def test_up_creates_provisions_and_publishes_the_port():
    incus = MagicMock()
    incus.list_containers.return_value = []
    incus.profile_exists.return_value = False
    incus.exec.return_value = "active\n"

    rd.display_up(incus, sleep_fn=lambda _s: None)

    incus.init.assert_called_once()
    assert incus.init.call_args.args[1] == rd.DISPLAY_CONTAINER
    devices = {c.args[1]: c.args[3] for c in incus.config_device_add.call_args_list}
    # Not under /run: systemd mounts a tmpfs over it after Incus has mounted
    # the device, which hides the shared directory from weston.
    assert devices["shared"]["path"] == "/srv/jailbee-display"
    assert not devices["shared"]["path"].startswith("/run")
    # weston creates the socket here, so the display container's own mount is writable.
    assert "readonly" not in devices["shared"]
    assert devices["rdp"] == {
        "listen": "tcp:127.0.0.1:13389",
        "connect": "tcp:127.0.0.1:3389",
    }
    incus.start.assert_called_with(rd.DISPLAY_CONTAINER)


def test_up_creates_the_host_directory_privately(tmp_path):
    incus = MagicMock()
    incus.list_containers.return_value = []
    incus.exec.return_value = "active\n"

    rd.display_up(incus, sleep_fn=lambda _s: None)

    directory = tmp_path / "jailbee" / "display"
    assert directory.is_dir()
    assert directory.stat().st_mode & 0o777 == 0o700


def test_provisioning_script_carries_both_files_and_the_identity():
    incus = MagicMock()
    incus.list_containers.return_value = []
    incus.exec.return_value = "active\n"

    rd.display_up(incus, sleep_fn=lambda _s: None)

    scripts = [c.args[1][2] for c in incus.exec.call_args_list if c.args[1][:2] == ["bash", "-c"]]
    provisioning = next(s for s in scripts if "JAILBEE_INSTALL_EOF" in s)
    assert "--address=127.0.0.1" in provisioning
    assert "--shell=desktop" in provisioning
    # weston's default 300 s idle timeout locks the shared screen.
    assert "--idle-time=0" in provisioning
    assert "JAILBEE_UID=" in provisioning
    # weston 14 leaves FreeRDP 3's extended NLA on with no SAM file, so a client
    # asking for NLA is refused; the WinPR registry turns it off, leaving TLS.
    assert "install -d -m 0755 /etc/FreeRDP /etc/FreeRDP/FreeRDP" in provisioning
    assert "/etc/FreeRDP/FreeRDP/HKLM.reg" in provisioning
    assert "[HKEY_LOCAL_MACHINE\\Software\\FreeRDP\\FreeRDP\\Server]" in provisioning
    assert '"ExtSecurity"=dword:00000000' in provisioning
    assert "JAILBEE_RDP_" not in provisioning


def test_down_stops_a_running_display(mocker):
    incus = MagicMock()
    incus.list_containers.return_value = _running()
    stop = mocker.patch("jailbee.remote_display.stop_container")

    rd.display_down(incus)

    stop.assert_called_once()


@pytest.mark.parametrize("listing", [[], [{"name": rd.DISPLAY_CONTAINER, "status": "Stopped"}]])
def test_down_leaves_a_display_that_is_not_running_alone(mocker, listing):
    incus = MagicMock()
    incus.list_containers.return_value = listing
    stop = mocker.patch("jailbee.remote_display.stop_container")

    rd.display_down(incus)

    stop.assert_not_called()


def test_client_connected_reads_established_connections():
    incus = MagicMock()
    incus.exec.return_value = "1\n"
    assert rd.client_connected(incus) is True

    incus.exec.return_value = "0\n"
    assert rd.client_connected(incus) is False

    incus.exec.side_effect = IncusError("boom")
    assert rd.client_connected(incus) is False


class _Clock:
    """A fake clock that only moves when the injected sleep is called."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def __call__(self):
        return self.now


def test_wait_for_client_polls_until_connected_and_settled():
    incus = MagicMock()
    incus.exec.side_effect = ["0\n", "0\n", "1\n", "1\n"]
    clock = _Clock()

    assert rd.wait_for_client(incus, timeout_s=10, poll_s=1, sleep_fn=clock.sleep, clock=clock)
    assert clock.sleeps == [1, 1, rd.SEAT_SETTLE_SECONDS]


def test_a_connection_that_drops_during_the_settle_is_not_ready():
    incus = MagicMock()
    incus.exec.side_effect = ["1\n", "0\n"]
    clock = _Clock()

    assert rd.client_ready(incus, clock.sleep) is False
    assert clock.sleeps == [rd.SEAT_SETTLE_SECONDS]


def test_a_stable_connection_is_ready_after_the_settle():
    incus = MagicMock()
    incus.exec.return_value = "1\n"
    clock = _Clock()

    assert rd.client_ready(incus, clock.sleep) is True
    assert clock.sleeps == [rd.SEAT_SETTLE_SECONDS]


def test_wait_for_client_continues_after_a_dropped_connection():
    incus = MagicMock()
    incus.exec.side_effect = ["1\n", "0\n", "1\n", "1\n"]
    clock = _Clock()

    assert rd.wait_for_client(incus, timeout_s=30, poll_s=1, sleep_fn=clock.sleep, clock=clock)


def test_wait_for_client_gives_up_at_the_deadline():
    incus = MagicMock()
    incus.exec.return_value = "0\n"
    clock = _Clock()

    assert not rd.wait_for_client(incus, timeout_s=3, poll_s=1, sleep_fn=clock.sleep, clock=clock)
    assert clock.sleeps == [1, 1, 1]


def test_the_recipe_is_two_steps_with_a_host_placeholder():
    lines = rd.format_connection_info(rd.connection_info(8022))
    text = "\n".join(lines)

    assert "ssh -N -L 3389:127.0.0.1:13389 -p 8022 jailbee@<host>" in text
    assert "localhost:3389" in text


def test_the_recipe_says_any_login_works():
    """Clients prompt for credentials; weston's TLS-only mode ignores them."""
    text = "\n".join(rd.format_connection_info(rd.connection_info(8022)))

    assert "any user name and password" in text


def test_ensure_display_mount_tolerates_an_existing_device():
    incus = MagicMock()
    incus.config_device_add.side_effect = IncusError("Device already exists")

    rd.ensure_display_mount(incus, "feat-1")  # must not raise


def test_ensure_display_mount_is_read_only(tmp_path):
    """A writable client mount lets one container replace the socket for all."""
    incus = MagicMock()

    rd.ensure_display_mount(incus, "feat-1")

    args = incus.config_device_add.call_args.args
    assert args[:3] == ("feat-1", "display-socket", "disk")
    assert args[3]["readonly"] == "true"
    assert args[3]["source"] == str(tmp_path / "jailbee" / "display")


def test_prepare_mounts_the_display_and_returns_when_a_client_is_connected():
    incus = MagicMock()
    incus.list_containers.return_value = _running()
    incus.exec.side_effect = ["active\n", "1\n", "1\n"]  # service, client, settled
    said = []

    rd.prepare_shared_display(
        incus,
        "feat-1",
        ssh_port=8022,
        say=said.append,
        sleep_fn=lambda _s: None,
    )

    incus.config_device_add.assert_called()  # the display mount
    assert said == []  # nothing to explain: the client is already there


def test_prepare_without_a_client_prints_the_recipe_waits_and_fails():
    """Review focus 3."""
    incus = MagicMock()
    incus.list_containers.return_value = _running()
    incus.exec.side_effect = lambda *a, **k: "active\n" if "systemctl" in a[1] else "0\n"
    said = []
    clock = _Clock()

    with pytest.raises(rd.DisplayError, match="RDP client"):
        rd.prepare_shared_display(
            incus,
            "feat-1",
            ssh_port=8022,
            say=said.append,
            sleep_fn=clock.sleep,
            wait_seconds=3,
            clock=clock,
        )

    assert any("ssh -N -L" in line for line in said)
    assert clock.sleeps == [rd.CLIENT_POLL_SECONDS, rd.CLIENT_POLL_SECONDS]


def test_provisioning_installs_waypipe():
    script = rd._read_provision_text("install.sh")

    assert "waypipe" in script.split("apt-get install", 1)[1].splitlines()[0]


def test_a_display_without_waypipe_counts_as_incompletely_provisioned():
    incus = MagicMock()
    incus.exec.return_value = "absent\n"
    assert rd._provisioning_incomplete(incus) is True

    incus.exec.return_value = "present\n"
    assert rd._provisioning_incomplete(incus) is False
    script = incus.exec.call_args.args[1][-1]
    assert "command -v waypipe" in script
    assert rd._UNIT_PATH in script


def test_up_mounts_the_links_directory_writable_and_creates_it_privately(tmp_path):
    incus = MagicMock()
    incus.list_containers.return_value = []
    incus.profile_exists.return_value = False
    incus.exec.return_value = "active\n"

    rd.display_up(incus, sleep_fn=lambda _s: None)

    from jailbee.remote_ssh.waypipe import links_dir

    devices = {c.args[1]: c.args[3] for c in incus.config_device_add.call_args_list}
    assert devices[rd.LINKS_DEVICE] == {
        "source": str(links_dir()),
        "path": rd.WAYPIPE_LINKS_CONTAINER_DIR,
    }
    assert links_dir().stat().st_mode & 0o777 == 0o700


def test_ensure_links_device_tolerates_an_existing_device():
    incus = MagicMock()
    incus.config_device_add.side_effect = IncusError("Device already exists")

    rd.ensure_links_device(incus)


def test_ensure_waypipe_display_starts_a_stopped_display_and_never_waits_for_rdp(mocker):
    incus = MagicMock()
    up = mocker.patch.object(rd, "display_up")
    mocker.patch.object(rd, "display_status", return_value=rd.DisplayStatus.STOPPED)
    waited = mocker.patch.object(rd, "wait_for_client")

    rd.ensure_waypipe_display(incus)

    up.assert_called_once()
    waited.assert_not_called()
    assert incus.config_device_add.call_args.args[1] == rd.LINKS_DEVICE


def test_ensure_waypipe_display_leaves_a_running_display_alone(mocker):
    incus = MagicMock()
    up = mocker.patch.object(rd, "display_up")
    mocker.patch.object(rd, "display_status", return_value=rd.DisplayStatus.RUNNING)

    rd.ensure_waypipe_display(incus)

    up.assert_not_called()
    assert incus.config_device_add.call_args.args[1] == rd.LINKS_DEVICE


def test_remove_waypipe_sockets_for_one_session_or_all(tmp_path):
    from jailbee.gui import display_state_dir
    from jailbee.remote_ssh.waypipe import links_dir

    display_state_dir().mkdir(parents=True)
    links_dir().mkdir(parents=True)
    for name in ("wp-aaaaaaaa-c1", "wp-aaaaaaaa-c2", "wp-bbbbbbbb-c1", "wayland-0"):
        (display_state_dir() / name).touch()
    for name in ("aaaaaaaa.sock", "bbbbbbbb.sock"):
        (links_dir() / name).touch()

    rd.remove_waypipe_sockets("aaaaaaaa")
    assert sorted(p.name for p in display_state_dir().iterdir()) == ["wayland-0", "wp-bbbbbbbb-c1"]
    assert [p.name for p in links_dir().iterdir()] == ["bbbbbbbb.sock"]

    rd.remove_waypipe_sockets()
    assert [p.name for p in display_state_dir().iterdir()] == ["wayland-0"]
    assert list(links_dir().iterdir()) == []


def test_remove_waypipe_sockets_can_leave_the_links_alone():
    from jailbee.gui import display_state_dir
    from jailbee.remote_ssh.waypipe import links_dir

    display_state_dir().mkdir(parents=True)
    links_dir().mkdir(parents=True)
    (display_state_dir() / "wp-aaaaaaaa-c1").touch()
    (links_dir() / "aaaaaaaa.sock").touch()

    rd.remove_waypipe_sockets(links=False)

    assert list(display_state_dir().iterdir()) == []
    assert [p.name for p in links_dir().iterdir()] == ["aaaaaaaa.sock"]


def test_remove_waypipe_sockets_survives_a_path_that_cannot_be_removed(mocker):
    from pathlib import Path

    from jailbee.gui import display_state_dir

    display_state_dir().mkdir(parents=True)
    for name in ("wp-aaaaaaaa-c1", "wp-aaaaaaaa-c2"):
        (display_state_dir() / name).touch()
    real = Path.unlink

    def unlink(self, missing_ok=False):
        if self.name == "wp-aaaaaaaa-c1":
            raise PermissionError("denied")
        real(self, missing_ok=missing_ok)

    mocker.patch.object(Path, "unlink", unlink)

    rd.remove_waypipe_sockets()  # must not raise

    assert [p.name for p in display_state_dir().iterdir()] == ["wp-aaaaaaaa-c1"]


def test_remove_waypipe_sockets_without_directories_is_a_no_op():
    rd.remove_waypipe_sockets()


def test_down_removes_the_display_sockets_but_not_the_ssh_servers_links(mocker):
    incus = MagicMock()
    incus.list_containers.return_value = _running()
    mocker.patch.object(rd, "stop_container")
    removed = mocker.patch.object(rd, "remove_waypipe_sockets")

    rd.display_down(incus)

    removed.assert_called_once_with(links=False)


def _existing(status, exec_out):
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": rd.DISPLAY_CONTAINER, "status": status}]
    incus.profile_exists.return_value = True
    incus.exec.return_value = exec_out
    return incus


@pytest.mark.parametrize("status", ["Running", "Stopped"])
def test_up_adds_the_links_device_to_an_existing_display(mocker, status):
    incus = _existing(status, "active\n")
    mocker.patch.object(rd, "_provisioning_incomplete", return_value=False)

    rd.display_up(incus, sleep_fn=lambda _s: None)

    incus.init.assert_not_called()
    assert [c.args[1] for c in incus.config_device_add.call_args_list] == [rd.LINKS_DEVICE]


def _provisioning_scripts(incus):
    scripts = [c.args[1][2] for c in incus.exec.call_args_list if c.args[1][:2] == ["bash", "-c"]]
    return [s for s in scripts if "JAILBEE_INSTALL_EOF" in s]


def test_up_reprovisions_an_existing_display_that_lacks_waypipe(mocker):
    incus = _existing("Running", "active\n")
    mocker.patch.object(rd, "_provisioning_incomplete", return_value=True)

    rd.display_up(incus, sleep_fn=lambda _s: None)

    assert len(_provisioning_scripts(incus)) == 1
    incus.init.assert_not_called()


def test_up_leaves_a_fully_provisioned_existing_display_alone(mocker):
    incus = _existing("Running", "active\n")
    mocker.patch.object(rd, "_provisioning_incomplete", return_value=False)

    rd.display_up(incus, sleep_fn=lambda _s: None)

    assert _provisioning_scripts(incus) == []


def _running_display(mocker, *, incomplete):
    incus = MagicMock()
    mocker.patch.object(rd, "display_status", return_value=rd.DisplayStatus.RUNNING)
    mocker.patch.object(rd, "_provisioning_incomplete", return_value=incomplete)
    return incus, mocker.patch.object(rd, "_provision"), mocker.patch.object(rd, "display_up")


def test_ensure_waypipe_display_reprovisions_a_running_display_lacking_waypipe(mocker):
    incus, provision, up = _running_display(mocker, incomplete=True)

    rd.ensure_waypipe_display(incus)

    provision.assert_called_once_with(incus)
    up.assert_not_called()
    assert incus.config_device_add.call_args.args[1] == rd.LINKS_DEVICE


def test_ensure_waypipe_display_does_not_reprovision_a_complete_running_display(mocker):
    incus, provision, up = _running_display(mocker, incomplete=False)

    rd.ensure_waypipe_display(incus)

    provision.assert_not_called()
    up.assert_not_called()
    assert incus.config_device_add.call_args.args[1] == rd.LINKS_DEVICE


def test_ensure_waypipe_display_stopped_path_goes_through_display_up_only(mocker):
    incus, provision, up = _running_display(mocker, incomplete=True)
    mocker.patch.object(rd, "display_status", return_value=rd.DisplayStatus.STOPPED)

    rd.ensure_waypipe_display(incus)

    up.assert_called_once()
    provision.assert_not_called()
    assert incus.config_device_add.call_args.args[1] == rd.LINKS_DEVICE
