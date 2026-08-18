"""Tests for the `jailbee port` command group."""

from pathlib import Path

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
    _, incus = repo
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
    _, incus = repo
    check = mocker.patch("jailbee.ports.check_host_port")
    mocker.patch("jailbee.ports.add_forward")
    result = runner.invoke(app, ["port", "to-host", "8080", "--host-port", "18080"])
    assert result.exit_code == 0, result.output
    assert check.call_args.args[1:] == ("127.0.0.1", 18080)


def test_to_host_auto_allocates_and_prints_the_port(repo, mocker):
    mocker.patch("jailbee.ports.declared_host_ports", return_value={18080: "app-other"})
    allocate = mocker.patch("jailbee.ports.allocate_host_port", return_value=20001)
    add = mocker.patch("jailbee.ports.add_forward")
    result = runner.invoke(app, ["port", "to-host", "8080", "--host-port", "auto"])
    assert result.exit_code == 0, result.output
    assert allocate.call_args.args[1] == {18080}
    assert add.call_args.kwargs["host_port"] == 20001
    assert "20001" in result.output


def test_to_host_rejects_a_non_numeric_host_port(repo):
    result = runner.invoke(app, ["port", "to-host", "8080", "--host-port", "nope"])
    assert result.exit_code == 2
    assert "auto" in result.stdout + (result.stderr or "")


def test_port_out_of_range_is_rejected_before_incus(repo, mocker):
    add = mocker.patch("jailbee.ports.add_forward")
    result = runner.invoke(app, ["port", "to-container", "70000"])
    assert result.exit_code == 2
    # typer.BadParameter goes to stderr; tui.error uses a stderr Console too,
    # so every error assertion in this file reads both streams.
    assert "1..65535" in result.stdout + (result.stderr or "")
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
        {"type": "proxy", "bind": "instance", "listen": "tcp:127.0.0.1:5037",
         "connect": "tcp:127.0.0.1:5037"},
    )
    mocker.patch("jailbee.ports.forwards_for", return_value=[fwd])
    result = runner.invoke(app, ["port", "ls", "feat-x"])
    assert result.exit_code == 0, result.output
    assert "port-cfg-adb" in result.output
    assert "to-container" in result.output
    assert "config" in result.output


def test_ls_lists_a_hand_made_proxy_device_as_other(repo, mocker):
    from jailbee.ports import parse_device

    fwd = parse_device(
        "hand-made",
        {"type": "proxy", "bind": "host", "listen": "tcp:127.0.0.1:9000",
         "connect": "tcp:127.0.0.1:9000"},
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
            ContainerInfo(name="app-a", state="Running", network="strict", ip=None,
                          memory_limit=None, repo="app"),
            ContainerInfo(name="app-b", state="Stopped", network="strict", ip=None,
                          memory_limit=None, repo="app"),
        ],
    )
    mocker.patch(
        "jailbee.ports.list_forwards",
        return_value={
            "app-a": [
                parse_device(
                    "port-cfg-adb",
                    {"type": "proxy", "bind": "instance",
                     "listen": "tcp:127.0.0.1:5037", "connect": "tcp:127.0.0.1:5037"},
                )
            ],
            "app-b": [],
        },
    )
    result = runner.invoke(app, ["port", "ls"])
    assert result.exit_code == 0, result.output
    assert "port-cfg-adb" in result.output


def test_ls_json_format(repo, mocker):
    from jailbee.ports import parse_device

    fwd = parse_device(
        "port-cfg-adb",
        {"type": "proxy", "bind": "instance", "listen": "tcp:127.0.0.1:5037",
         "connect": "tcp:127.0.0.1:5037"},
    )
    mocker.patch("jailbee.ports.forwards_for", return_value=[fwd])
    result = runner.invoke(app, ["port", "ls", "feat-x", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert '"device": "port-cfg-adb"' in result.output
