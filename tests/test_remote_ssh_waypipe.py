"""waypipe's remote command, as `waypipe ssh` 0.11 sends it, and the names derived from it."""

from __future__ import annotations

import os
import socket
from unittest.mock import MagicMock

import pytest

from jailbee.incus import IncusError
from jailbee.remote_display import DisplayError
from jailbee.remote_ssh import waypipe as wp
from jailbee.remote_ssh.router import RouteError
from jailbee.remote_ssh.session import WaypipeSession

SOCK = "/tmp/waypipe-server-6dCPSslHnp.sock"
# Captured from waypipe 0.11.0 `waypipe ssh` (spec, spike item 1).
OBSERVED = (
    f"waypipe --unlink-socket --threads 0 --compress lz4 --socket {SOCK} "
    "--display wayland-6dCPSslHnp server dashboard"
)


def test_the_observed_dashboard_command_parses():
    assert wp.parse_server_command(OBSERVED) == wp.WaypipeRequest(
        socket=SOCK, compress="lz4", command="dashboard"
    )


@pytest.mark.parametrize(
    ("flags", "compress"),
    [
        ("--no-gpu --unlink-socket --threads 0 --compress zstd", "zstd"),
        ("--unlink-socket --threads 2 --compress lz4 --video=h264", "lz4"),
        ("--unlink-socket --compress zstd=5", "zstd=5"),
        ("--unlink-socket --compress=none", "none"),
        ("--unlink-socket", "lz4"),
    ],
)
def test_observed_flags_parse_and_keep_the_compression_exact(flags, compress):
    raw = f"waypipe {flags} --socket {SOCK} --display wayland-x server dashboard"

    assert wp.parse_server_command(raw).compress == compress


def test_a_direct_command_keeps_its_arguments_quoted():
    raw = (
        f"waypipe --compress lz4 --socket {SOCK} --display wayland-x server "
        "--repo app chrome 'my c'"
    )

    assert wp.parse_server_command(raw).command == "--repo app chrome 'my c'"


def test_an_optional_double_dash_before_the_command_is_accepted():
    raw = f"waypipe --compress lz4 --socket {SOCK} --display wayland-x server -- dashboard"

    assert wp.parse_server_command(raw).command == "dashboard"


def test_no_command_is_none_whatever_login_shell_says():
    raw = f"waypipe --login-shell --unlink-socket --compress lz4 --socket {SOCK} --display w server"

    assert wp.parse_server_command(raw).command is None


@pytest.mark.parametrize("raw", [None, "", "dashboard", "--repo app ls", "waypipex server"])
def test_anything_not_waypipe_shaped_is_not_a_waypipe_request(raw):
    assert wp.parse_server_command(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        f"waypipe --compress lz4 --socket {SOCK} --display w client",
        "waypipe --compress lz4 --display w server dashboard",
        f"waypipe --compress lz4 --socket {SOCK} --display w --oneshot server dashboard",
        f"waypipe --compress lz4 --socket {SOCK} --display w --xwls server dashboard",
        f"waypipe --compress brotli --socket {SOCK} --display w server dashboard",
        f"waypipe --compress lz4=x --socket {SOCK} --display w server dashboard",
        f"waypipe --compress lz4 --socket {SOCK} --display w --remote-bin /bin/sh server x",
        "waypipe --compress lz4 --socket relative.sock --display w server dashboard",
        "waypipe --compress lz4 --socket 'unterminated",
        f"waypipe --threads many --compress lz4 --socket {SOCK} --display w server dashboard",
    ],
)
def test_every_other_shape_is_refused_naming_the_supported_form(raw):
    with pytest.raises(RouteError, match="waypipe ssh"):
        wp.parse_server_command(raw)


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        (SOCK, True),
        ("/x/y-server-fKlbZULuGl.sock", True),
        ("/tmp/waypipe-client-6dCPSslHnp.sock", False),
        ("relative-server-abcdef.sock", False),
        ("/tmp/../etc/x-server-abcdef.sock", False),
        ("/tmp/waypipe-server-.sock", False),
    ],
)
def test_client_socket_path_shape(path, ok):
    assert wp.is_client_socket_path(path) is ok


def test_session_ids_are_short_hex_and_distinct():
    ids = {wp.new_session_id() for _ in range(50)}

    assert len(ids) == 50
    assert all(len(i) == 8 and int(i, 16) >= 0 for i in ids)


def test_links_live_outside_the_shared_display_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from jailbee.gui import display_state_dir

    path = wp.links_socket("0a1b2c3d")

    assert path.name == "0a1b2c3d.sock"
    assert display_state_dir() not in path.parents


def test_names():
    assert wp.server_name("0a1b2c3d", "app-main") == "wp-0a1b2c3d-app-main"
    assert wp.unit_name("0a1b2c3d", "app-main") == "jailbee-wp-0a1b2c3d-app-main"


WPS = WaypipeSession(id="0a1b2c3d", compress="zstd=5")


@pytest.fixture(autouse=True)
def _live_session():
    """The session's forward listener exists, as it does while the client is connected."""
    wp.links_dir().mkdir(parents=True, exist_ok=True)
    wp.links_socket(WPS.id).touch()


def _socket_appears(name="wp-0a1b2c3d-app-main"):
    from jailbee.gui import display_state_dir

    def run(container, cmd, **kwargs):
        if cmd[0] == "systemd-run":
            display_state_dir().mkdir(parents=True, exist_ok=True)
            (display_state_dir() / name).touch()
            return ""
        return "inactive\n"

    return run


def test_start_runs_one_unit_with_the_clients_compression_and_the_container_title():
    incus = MagicMock()
    incus.exec.side_effect = _socket_appears()

    path = wp.start_container_server(incus, WPS, "app-main", sleep_fn=lambda _s: None)

    assert path == "/run/jailbee-display/wp-0a1b2c3d-app-main"
    run = next(c for c in incus.exec.call_args_list if c.args[1][0] == "systemd-run")
    assert run.args[0] == "jailbee-display"
    argv = run.args[1]
    assert "--unit=jailbee-wp-0a1b2c3d-app-main" in argv
    assert f"--uid={os.getuid()}" in argv and f"--gid={os.getgid()}" in argv
    assert "--collect" in argv
    tail = argv[argv.index("_") :]
    assert argv[argv.index("sh") + 1 : argv.index("_")] == [
        "-c",
        'rm -f "$1"; shift; exec waypipe "$@"',
    ]
    assert tail == [
        "_",
        "/srv/jailbee-display/wp-0a1b2c3d-app-main",
        "--no-gpu",
        "--compress",
        "zstd=5",
        "--title-prefix",
        "[app-main] ",
        "--socket",
        "/srv/jailbee-waypipe-links/0a1b2c3d.sock",
        "--display",
        "/srv/jailbee-display/wp-0a1b2c3d-app-main",
        "server",
        "--",
        "sleep",
        "infinity",
    ]


def test_start_mounts_the_shared_directory_into_the_client_container(mocker):
    incus = MagicMock()
    incus.exec.side_effect = _socket_appears()
    mount = mocker.patch("jailbee.remote_display.ensure_display_mount")

    wp.start_container_server(incus, WPS, "app-main", sleep_fn=lambda _s: None)

    mount.assert_called_once_with(incus, "app-main")


def test_a_running_server_is_reused():
    from jailbee.gui import display_state_dir

    display_state_dir().mkdir(parents=True, exist_ok=True)
    (display_state_dir() / "wp-0a1b2c3d-app-main").touch()
    incus = MagicMock()
    incus.exec.return_value = "active\n"

    wp.start_container_server(incus, WPS, "app-main", sleep_fn=lambda _s: None)

    assert all(c.args[1][0] != "systemd-run" for c in incus.exec.call_args_list)


def test_a_concurrent_start_that_lost_the_race_is_success():
    from jailbee.gui import display_state_dir

    incus = MagicMock()

    def run(container, cmd, **kwargs):
        if cmd[0] == "systemd-run":
            display_state_dir().mkdir(parents=True, exist_ok=True)
            (display_state_dir() / "wp-0a1b2c3d-app-main").touch()
            raise IncusError(
                "Failed to start transient service unit: Unit "
                "jailbee-wp-0a1b2c3d-app-main.service already exists."
            )
        return "inactive\n"

    incus.exec.side_effect = run

    wp.start_container_server(incus, WPS, "app-main", sleep_fn=lambda _s: None)


def test_a_socket_that_never_appears_fails_and_stops_the_unit():
    incus = MagicMock()
    incus.exec.return_value = "inactive\n"

    with pytest.raises(DisplayError, match="waypipe"):
        wp.start_container_server(incus, WPS, "app-main", sleep_fn=lambda _s: None, wait_seconds=1)

    stops = [c.args[1] for c in incus.exec.call_args_list if c.args[1][:2] == ["systemctl", "stop"]]
    assert stops == [["systemctl", "stop", "jailbee-wp-0a1b2c3d-app-main.service"]]


@pytest.mark.parametrize("container", ["../x", "a b", "A", "", "x" * 64, "-x", "a;b"])
def test_an_unsafe_container_name_never_reaches_systemd(container):
    incus = MagicMock()

    with pytest.raises(DisplayError):
        wp.start_container_server(incus, WPS, container, sleep_fn=lambda _s: None)

    assert incus.mock_calls == []


@pytest.mark.parametrize("compress", ["", "zstd=5 --x", "gzip", "lz4=123"])
def test_an_invalid_compress_value_never_reaches_incus(compress):
    incus = MagicMock()
    session = WaypipeSession(id="0a1b2c3d", compress=compress)

    with pytest.raises(DisplayError):
        wp.start_container_server(incus, session, "app-main", sleep_fn=lambda _s: None)

    assert incus.mock_calls == []


def test_stop_session_stops_by_glob_so_a_unit_still_starting_is_caught(mocker):
    incus = MagicMock()
    removed = mocker.patch("jailbee.remote_display.remove_waypipe_sockets")

    wp.stop_session(incus, "0a1b2c3d")

    incus.exec.assert_called_once_with(
        "jailbee-display", ["systemctl", "stop", "jailbee-wp-0a1b2c3d-*.service"], timeout=30
    )
    removed.assert_called_once_with("0a1b2c3d")


def test_stop_session_never_raises(mocker):
    incus = MagicMock()
    incus.exec.side_effect = IncusError("display container is gone")
    removed = mocker.patch("jailbee.remote_display.remove_waypipe_sockets")

    wp.stop_session(incus, "0a1b2c3d")

    removed.assert_called_once_with("0a1b2c3d")


def test_an_ended_session_starts_nothing():
    wp.links_socket(WPS.id).unlink()
    incus = MagicMock()

    with pytest.raises(DisplayError, match="ended"):
        wp.start_container_server(incus, WPS, "app-main", sleep_fn=lambda _s: None)

    assert incus.mock_calls == []


def test_the_unit_runs_as_the_displays_own_user_not_the_repos(mocker):
    mocker.patch("jailbee.remote_ssh.waypipe.os.getuid", return_value=4242)
    mocker.patch("jailbee.remote_ssh.waypipe.os.getgid", return_value=4343)
    incus = MagicMock()
    incus.exec.side_effect = _socket_appears()

    wp.start_container_server(incus, WPS, "app-main", sleep_fn=lambda _s: None)

    argv = next(c.args[1] for c in incus.exec.call_args_list if c.args[1][0] == "systemd-run")
    assert "--uid=4242" in argv and "--gid=4343" in argv


def test_stop_session_survives_a_socket_that_cannot_be_removed(mocker):
    incus = MagicMock()
    mocker.patch("jailbee.remote_display.remove_waypipe_sockets", side_effect=PermissionError("x"))

    wp.stop_session(incus, "0a1b2c3d")  # must not raise


# Sessions ``aaaaaaaa`` (live listener) and ``bbbbbbbb`` (dead) are mixed on
# disk and in systemd below.
LIVE, DEAD = "aaaaaaaa", "bbbbbbbb"


@pytest.fixture
def listening():
    """A live listener on the LIVE session's links socket, a stale file for DEAD's."""
    from jailbee.gui import display_state_dir

    wp.links_socket(WPS.id).unlink()  # the autouse fixture's file is not part of this scene
    display_state_dir().mkdir(parents=True, exist_ok=True)
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(wp.links_socket(LIVE)))
    server.listen()
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(wp.links_socket(DEAD)))
    stale.close()  # the file stays, nothing listens
    for name in (f"wp-{LIVE}-c1", f"wp-{DEAD}-c1"):
        (display_state_dir() / name).touch()
    yield
    server.close()


def _units_listing(incus):
    def run(container, cmd, **kwargs):
        if cmd[:2] == ["systemctl", "list-units"]:
            return (
                f"jailbee-wp-{LIVE}-c1.service loaded active running waypipe\n"
                f"jailbee-wp-{DEAD}-c1.service loaded active running waypipe\n"
                "jailbee-wp-cccccccc-c9.service loaded failed failed waypipe\n"
            )
        return ""

    incus.exec.side_effect = run


def test_prune_dead_keeps_a_session_with_a_live_listener(listening):
    from jailbee.gui import display_state_dir

    incus = MagicMock()
    _units_listing(incus)

    wp.prune_dead(incus)

    stops = [
        c.args[1][2] for c in incus.exec.call_args_list if c.args[1][:2] == ["systemctl", "stop"]
    ]
    assert sorted(stops) == [f"jailbee-wp-{DEAD}-*.service", "jailbee-wp-cccccccc-*.service"]
    assert wp.links_socket(LIVE).exists()
    assert (display_state_dir() / f"wp-{LIVE}-c1").exists()
    assert not wp.links_socket(DEAD).exists()
    assert not (display_state_dir() / f"wp-{DEAD}-c1").exists()


def test_prune_dead_never_raises(listening):
    incus = MagicMock()
    incus.exec.side_effect = RuntimeError("boom")

    wp.prune_dead(incus)


def test_prune_dead_without_anything_does_no_stop():
    wp.links_socket(WPS.id).unlink()
    incus = MagicMock()
    incus.exec.return_value = ""

    wp.prune_dead(incus)

    assert all(c.args[1][:2] != ["systemctl", "stop"] for c in incus.exec.call_args_list)


def test_a_listener_that_cannot_be_probed_counts_as_alive(mocker, tmp_path):
    probe = mocker.patch("jailbee.remote_ssh.waypipe.socket.socket").return_value
    probe.connect.side_effect = PermissionError("denied")

    assert wp._listener_alive(tmp_path / "x.sock") is True
