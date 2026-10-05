"""waypipe's remote command, as `waypipe ssh` 0.11 sends it, and the names derived from it."""

from __future__ import annotations

import pytest

from jailbee.remote_ssh import waypipe as wp
from jailbee.remote_ssh.router import RouteError

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
