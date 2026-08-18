"""Tests for the `jailbee port` command group."""

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.ports import PortError

runner = CliRunner()


@pytest.fixture
def repo(tmp_path, mocker, make_cfg):
    """A loaded repo config plus a MagicMock Incus, wired into the CLI."""
    cfg = make_cfg(tmp_path / "app")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "app-feat-x"))
    return cfg, incus


def test_to_container_defaults_the_host_port(repo, mocker):
    _, _ = repo
    add = mocker.patch("jailbee.ports.add_forward")
    result = runner.invoke(app, ["port", "to-container", "5037"])
    assert result.exit_code == 0, result.output
    assert add.call_args.kwargs == {
        "direction": "to-container",
        "proto": "tcp",
        "container_port": 5037,
        "host_port": 5037,
        "container_address": "127.0.0.1",
        "host_address": "127.0.0.1",
    }


def test_to_container_accepts_an_explicit_host_port_and_udp(repo, mocker):
    add = mocker.patch("jailbee.ports.add_forward")
    result = runner.invoke(
        app,
        ["port", "to-container", "5353", "--host-port", "53", "--proto", "udp"],
    )
    assert result.exit_code == 0, result.output
    assert add.call_args.kwargs["host_port"] == 53
    assert add.call_args.kwargs["proto"] == "udp"


def test_to_host_pre_flights_an_explicit_host_port(repo, mocker):
    _, _ = repo
    check = mocker.patch("jailbee.ports.check_host_port")
    mocker.patch("jailbee.ports.add_forward")
    result = runner.invoke(app, ["port", "to-host", "8080", "--host-port", "18080"])
    assert result.exit_code == 0, result.output
    assert check.call_args.args[1:] == ("127.0.0.1", 18080)


def test_to_host_auto_allocates_and_prints_the_port(repo, mocker):
    from jailbee.ports import Endpoint, Forward

    mocker.patch("jailbee.ports.declared_host_ports", return_value={18080: "app-other"})
    allocate = mocker.patch("jailbee.ports.allocate_host_port", return_value=20001)
    fwd = Forward(
        device="port-th-tcp-8080",
        direction="to-host",
        proto="tcp",
        container=Endpoint(proto="tcp", address="127.0.0.1", port=8080, raw="tcp:127.0.0.1:8080"),
        host=Endpoint(proto="tcp", address="127.0.0.1", port=20001, raw="tcp:127.0.0.1:20001"),
        source="ad-hoc",
    )
    add = mocker.patch("jailbee.ports.add_forward", return_value=fwd)
    result = runner.invoke(app, ["port", "to-host", "8080", "--host-port", "auto"])
    assert result.exit_code == 0, result.output
    assert allocate.call_args.args[1] == {18080}
    assert add.call_args.kwargs["host_port"] == 20001
    # The port appears via the success message's real endpoint, not a
    # separate announcement that could contradict a later failure.
    assert "20001" in result.output


def test_to_container_success_line_keeps_ipv6_brackets(repo, mocker):
    """Rich reads a bracketed IPv6 endpoint (`[fd00::1]:5037`) as a style tag
    and silently deletes it from a plain `console.print` — pinned here so a
    future refactor away from `success_plain` loses this the loud way (a
    failing test), not the silent way (a vanished address in the terminal).
    """
    from jailbee.ports import Endpoint, Forward

    fwd = Forward(
        device="port-tc-tcp-5037",
        direction="to-container",
        proto="tcp",
        container=Endpoint(proto="tcp", address="fd00::1", port=5037, raw="tcp:[fd00::1]:5037"),
        host=Endpoint(proto="tcp", address="127.0.0.1", port=5037, raw="tcp:127.0.0.1:5037"),
        source="ad-hoc",
    )
    mocker.patch("jailbee.ports.add_forward", return_value=fwd)
    result = runner.invoke(app, ["port", "to-container", "5037"])
    assert result.exit_code == 0, result.output
    assert "[fd00::1]:5037" in result.output


def test_to_host_success_line_keeps_ipv6_brackets(repo, mocker):
    from jailbee.ports import Endpoint, Forward

    mocker.patch("jailbee.ports.check_host_port")
    fwd = Forward(
        device="port-th-tcp-5037",
        direction="to-host",
        proto="tcp",
        container=Endpoint(proto="tcp", address="127.0.0.1", port=5037, raw="tcp:127.0.0.1:5037"),
        host=Endpoint(proto="tcp", address="fd00::2", port=5037, raw="tcp:[fd00::2]:5037"),
        source="ad-hoc",
    )
    mocker.patch("jailbee.ports.add_forward", return_value=fwd)
    result = runner.invoke(app, ["port", "to-host", "5037"])
    assert result.exit_code == 0, result.output
    assert "[fd00::2]:5037" in result.output


def test_to_container_duplicate_error_keeps_ipv6_brackets(repo, mocker):
    """The duplicate-forward `PortError` message embeds both endpoints; it
    must survive to the terminal via `error_plain`, not `error` (which
    would silently drop the bracketed side)."""
    mocker.patch(
        "jailbee.ports.add_forward",
        side_effect=PortError(
            "Port 5037/tcp is already forwarded in app-feat-x: "
            "[fd00::1]:5037 ↔ 127.0.0.1:9999 (device port-tc-tcp-5037). "
            "Remove it first with `jailbee port rm port-tc-tcp-5037`."
        ),
    )
    result = runner.invoke(app, ["port", "to-container", "5037"])
    assert result.exit_code == 1
    assert "[fd00::1]:5037" in (result.stdout + (result.stderr or ""))


def test_to_host_rejects_a_non_numeric_host_port(repo):
    result = runner.invoke(app, ["port", "to-host", "8080", "--host-port", "nope"])
    assert result.exit_code == 2
    assert "auto" in result.stdout + (result.stderr or "")


def test_to_host_negative_host_port_gets_the_range_message(repo, mocker):
    """A numeric-but-out-of-range --host-port (e.g. `-1`) must report the
    same "1..65535" range message as `to-container`, not the "or 'auto'"
    message — which is misleading once the value has already parsed as a
    number. `"-1".isdigit()` is False, so this used to fall into the wrong
    branch entirely."""
    add = mocker.patch("jailbee.ports.add_forward")
    result = runner.invoke(app, ["port", "to-host", "8080", "--host-port", "-1"])
    assert result.exit_code == 2
    output = result.stdout + (result.stderr or "")
    assert "1..65535" in output
    assert "or 'auto'" not in output
    add.assert_not_called()


def test_port_out_of_range_is_rejected_before_incus(repo, mocker):
    add = mocker.patch("jailbee.ports.add_forward")
    result = runner.invoke(app, ["port", "to-container", "70000"])
    assert result.exit_code == 2
    # typer.BadParameter goes to stderr; tui.error uses a stderr Console too,
    # so every error assertion in this file reads both streams.
    assert "1..65535" in result.stdout + (result.stderr or "")
    add.assert_not_called()


@pytest.mark.parametrize("command", ["to-container", "to-host"])
def test_invalid_proto_is_rejected_before_incus(repo, mocker, command):
    """The config schema restricts `host_ports.proto` to tcp/udp; the ad-hoc
    commands must enforce the same restriction rather than letting an
    unsupported value (e.g. `sctp`) reach Incus."""
    add = mocker.patch("jailbee.ports.add_forward")
    result = runner.invoke(app, ["port", command, "5037", "--proto", "sctp"])
    assert result.exit_code == 2
    output = result.stdout + (result.stderr or "")
    assert "tcp" in output and "udp" in output
    add.assert_not_called()


@pytest.mark.parametrize(
    ("command", "option"),
    [
        ("to-container", "--host-address"),
        ("to-container", "--container-address"),
        ("to-host", "--host-address"),
        ("to-host", "--container-address"),
    ],
)
def test_hostname_address_is_rejected_before_incus(repo, mocker, command, option):
    """A hostname (rather than an IP literal) must be rejected here, before
    it reaches `ports._probe_free_port`/`ports.host_port_free` — which open a
    raw socket and either let an uncaught `OSError`/`gaierror` escape
    (`--host-port auto`) or swallow it into a confidently wrong "already in
    use" diagnosis (an explicit `--host-port N`, via `host_port_free`'s
    `except OSError: return False`)."""
    add = mocker.patch("jailbee.ports.add_forward")
    result = runner.invoke(app, ["port", command, "5037", option, "example.invalid"])
    assert result.exit_code == 2
    output = result.stdout + (result.stderr or "")
    assert "example.invalid" in output
    add.assert_not_called()


def test_port_error_exits_with_code_one(repo, mocker):
    mocker.patch("jailbee.ports.add_forward", side_effect=PortError("boom"))
    result = runner.invoke(app, ["port", "to-container", "5037"])
    assert result.exit_code == 1
    assert "boom" in result.stdout + (result.stderr or "")


def test_rm_delegates_to_remove_forward(repo, mocker):
    remove = mocker.patch("jailbee.ports.remove_forward")
    remove.return_value = mocker.Mock(device="port-cfg-adb")
    result = runner.invoke(app, ["port", "rm", "adb"])
    assert result.exit_code == 0, result.output
    assert remove.call_args.args[2] == "adb"


def test_ls_one_container_shows_direction_and_source(repo, mocker):
    from jailbee.ports import parse_device

    fwd = parse_device(
        "port-cfg-adb",
        {
            "type": "proxy",
            "bind": "instance",
            "listen": "tcp:127.0.0.1:5037",
            "connect": "tcp:127.0.0.1:5037",
        },
    )
    mocker.patch("jailbee.ports.forwards_for", return_value=[fwd])
    result = runner.invoke(app, ["port", "ls", "feat-x"])
    assert result.exit_code == 0, result.output
    assert "port-cfg-adb" in result.output
    assert "to-container" in result.output
    assert "config" in result.output


def test_ls_ipv6_endpoint_survives_the_table(repo, mocker):
    """Rich's Table.add_row reads `[fd00::1]:5037` as a style tag and
    silently drops it, rendering the cell as `:5037` — verified empirically
    against a bare Table before this fix. `port ls`'s endpoint cells must
    escape the value so the real address survives."""
    from jailbee.ports import parse_device

    fwd = parse_device(
        "port-cfg-adb",
        {
            "type": "proxy",
            "bind": "instance",
            "listen": "tcp:[fd00::1]:5037",
            "connect": "tcp:[fd00::2]:6037",
        },
    )
    mocker.patch("jailbee.ports.forwards_for", return_value=[fwd])
    result = runner.invoke(app, ["port", "ls", "feat-x"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "[fd00::1]:5037" in result.output
    assert "[fd00::2]:6037" in result.output


def test_ls_lists_a_hand_made_proxy_device_as_other(repo, mocker):
    from jailbee.ports import parse_device

    fwd = parse_device(
        "hand-made",
        {
            "type": "proxy",
            "bind": "host",
            "listen": "tcp:127.0.0.1:9000",
            "connect": "tcp:127.0.0.1:9000",
        },
    )
    mocker.patch("jailbee.ports.forwards_for", return_value=[fwd])
    result = runner.invoke(app, ["port", "ls", "feat-x"])
    assert result.exit_code == 0, result.output
    assert "other" in result.output


def test_ls_without_a_container_lists_the_repo(repo, mocker):
    from jailbee.lifecycle import ContainerInfo
    from jailbee.ports import parse_device

    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[
            ContainerInfo(
                name="app-a",
                state="Running",
                network="strict",
                ip=None,
                memory_limit=None,
                repo="app",
            ),
            ContainerInfo(
                name="app-b",
                state="Stopped",
                network="strict",
                ip=None,
                memory_limit=None,
                repo="app",
            ),
        ],
    )
    mocker.patch(
        "jailbee.ports.list_forwards",
        return_value={
            "app-a": [
                parse_device(
                    "port-cfg-adb",
                    {
                        "type": "proxy",
                        "bind": "instance",
                        "listen": "tcp:127.0.0.1:5037",
                        "connect": "tcp:127.0.0.1:5037",
                    },
                )
            ],
            "app-b": [],
        },
    )
    result = runner.invoke(app, ["port", "ls"])
    assert result.exit_code == 0, result.output
    assert "port-cfg-adb" in result.output


def test_ls_multi_container_headers_are_distinct(repo, mocker):
    """Regression test: `container` and `container_endpoint` both used to
    render the header CONTAINER, so a multi-container listing showed two
    identically-labelled columns — one holding the container's name, the
    other its in-container endpoint. This is exactly the misread the
    to-container/to-host vocabulary exists to prevent.
    """
    from jailbee.lifecycle import ContainerInfo
    from jailbee.ports import parse_device

    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[
            ContainerInfo(
                name="app-a",
                state="Running",
                network="strict",
                ip=None,
                memory_limit=None,
                repo="app",
            ),
            ContainerInfo(
                name="app-b",
                state="Running",
                network="strict",
                ip=None,
                memory_limit=None,
                repo="app",
            ),
        ],
    )
    mocker.patch(
        "jailbee.ports.list_forwards",
        return_value={
            "app-a": [
                parse_device(
                    "port-cfg-adb",
                    {
                        "type": "proxy",
                        "bind": "instance",
                        "listen": "tcp:127.0.0.1:5037",
                        "connect": "tcp:127.0.0.1:5037",
                    },
                )
            ],
            "app-b": [
                parse_device(
                    "port-th-tcp-8080",
                    {
                        "type": "proxy",
                        "bind": "host",
                        "listen": "tcp:127.0.0.1:20001",
                        "connect": "tcp:127.0.0.1:8080",
                    },
                )
            ],
        },
    )
    result = runner.invoke(app, ["port", "ls"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    header_line = next(line for line in result.output.splitlines() if "HANDLE" in line)
    headers = [cell.strip() for cell in header_line.strip("┃").split("┃")]
    assert headers == [
        "CONTAINER",
        "HANDLE",
        "DIRECTION",
        "PROTO",
        "IN CONTAINER",
        "ON HOST",
        "SOURCE",
    ]
    assert len(headers) == len(set(headers)), f"duplicate header in {headers}"


def test_ls_json_format(repo, mocker):
    from jailbee.ports import parse_device

    fwd = parse_device(
        "port-cfg-adb",
        {
            "type": "proxy",
            "bind": "instance",
            "listen": "tcp:127.0.0.1:5037",
            "connect": "tcp:127.0.0.1:5037",
        },
    )
    mocker.patch("jailbee.ports.forwards_for", return_value=[fwd])
    result = runner.invoke(app, ["port", "ls", "feat-x", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert '"device": "port-cfg-adb"' in result.output
