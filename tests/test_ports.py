"""Tests for port forwarding via Incus proxy devices."""

import pytest

from jailbee.config import HostPort
from jailbee.incus import IncusError
from jailbee.ports import (
    ADHOC_TO_CONTAINER_PREFIX,
    ADHOC_TO_HOST_PREFIX,
    CONFIG_PREFIX,
    PortError,
    add_forward,
    adhoc_device_name,
    config_device_name,
    entry_device,
    forwards_for,
    list_forwards,
    parse_device,
    remove_forward,
    render_device,
    resolve_handle,
)


def test_device_name_prefixes():
    assert CONFIG_PREFIX == "port-cfg-"
    assert ADHOC_TO_CONTAINER_PREFIX == "port-tc-"
    assert ADHOC_TO_HOST_PREFIX == "port-th-"


def test_config_device_name():
    assert config_device_name("adb") == "port-cfg-adb"


def test_adhoc_device_names_use_the_container_port():
    assert adhoc_device_name("to-container", "tcp", 5037) == "port-tc-tcp-5037"
    assert adhoc_device_name("to-host", "udp", 8080) == "port-th-udp-8080"


def test_render_to_container_listens_in_the_instance():
    props = render_device(
        "to-container",
        proto="tcp",
        container_port=5037,
        host_port=6037,
        container_address="127.0.0.1",
        host_address="127.0.0.1",
    )
    assert props == {
        "listen": "tcp:127.0.0.1:5037",
        "connect": "tcp:127.0.0.1:6037",
        "bind": "instance",
    }


def test_render_to_host_listens_on_the_host():
    props = render_device(
        "to-host",
        proto="tcp",
        container_port=8080,
        host_port=18080,
        container_address="127.0.0.1",
        host_address="127.0.0.1",
    )
    assert props == {
        "listen": "tcp:127.0.0.1:18080",
        "connect": "tcp:127.0.0.1:8080",
        "bind": "host",
    }


def test_render_brackets_ipv6_addresses():
    props = render_device(
        "to-container",
        proto="udp",
        container_port=5353,
        host_port=53,
        container_address="::1",
        host_address="fd00::5",
    )
    assert props == {
        "listen": "udp:[::1]:5353",
        "connect": "udp:[fd00::5]:53",
        "bind": "instance",
    }


def test_render_never_sets_nat_or_unix_only_options():
    props = render_device(
        "to-host",
        proto="tcp",
        container_port=1,
        host_port=2,
        container_address="127.0.0.1",
        host_address="127.0.0.1",
    )
    assert set(props) == {"listen", "connect", "bind"}


def test_entry_device_uses_the_entry_name_and_defaults():
    name, props = entry_device(HostPort(name="adb", port=5037))
    assert name == "port-cfg-adb"
    assert props == {
        "listen": "tcp:127.0.0.1:5037",
        "connect": "tcp:127.0.0.1:5037",
        "bind": "instance",
    }


def test_entry_device_honours_explicit_host_port():
    _, props = entry_device(HostPort(name="db", port=5432, host_port=15432))
    assert props["connect"] == "tcp:127.0.0.1:15432"
    assert props["listen"] == "tcp:127.0.0.1:5432"


def test_parse_device_round_trips_to_container():
    fwd = parse_device(
        "port-cfg-adb",
        {"type": "proxy", "bind": "instance", "listen": "tcp:127.0.0.1:5037",
         "connect": "tcp:127.0.0.1:5038"},
    )
    assert fwd is not None
    assert fwd.direction == "to-container"
    assert fwd.source == "config"
    assert fwd.proto == "tcp"
    assert fwd.container.port == 5037
    assert fwd.host.port == 5038


def test_parse_device_treats_lxd_bind_container_as_to_container():
    fwd = parse_device(
        "whatever",
        {"type": "proxy", "bind": "container", "listen": "tcp:127.0.0.1:5037",
         "connect": "tcp:127.0.0.1:5037"},
    )
    assert fwd is not None
    assert fwd.direction == "to-container"
    assert fwd.source == "other"


def test_parse_device_defaults_bind_to_host():
    fwd = parse_device(
        "port-th-tcp-8080",
        {"type": "proxy", "listen": "tcp:127.0.0.1:18080",
         "connect": "tcp:127.0.0.1:8080"},
    )
    assert fwd is not None
    assert fwd.direction == "to-host"
    assert fwd.source == "ad-hoc"
    assert fwd.container.port == 8080
    assert fwd.host.port == 18080


def test_parse_device_ignores_non_proxy_devices():
    assert parse_device("root", {"type": "disk", "path": "/"}) is None


def test_parse_device_handles_ipv6_and_ranges():
    fwd = parse_device(
        "port-tc-tcp-6000",
        {"type": "proxy", "bind": "instance", "listen": "tcp:[fd00::1]:6000-6002",
         "connect": "tcp:127.0.0.1:5037"},
    )
    assert fwd is not None
    assert fwd.container.address == "fd00::1"
    assert fwd.container.port is None
    assert fwd.container.display == "[fd00::1]:6000-6002"


def test_parse_device_handles_unix_endpoints():
    fwd = parse_device(
        "sock",
        {"type": "proxy", "bind": "instance", "listen": "unix:/run/x.sock",
         "connect": "tcp:127.0.0.1:5037"},
    )
    assert fwd is not None
    assert fwd.container.proto == "unix"
    assert fwd.container.port is None
    assert fwd.container.display == "/run/x.sock"


def _raw(name: str, devices: dict) -> dict:
    return {"name": name, "devices": devices}


def _proxy(listen: str, connect: str, bind: str = "instance") -> dict:
    return {"type": "proxy", "listen": listen, "connect": connect, "bind": bind}


def test_list_forwards_indexes_by_container_and_skips_others(mocker):
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        _raw("app-a", {
            "port-cfg-adb": _proxy("tcp:127.0.0.1:5037", "tcp:127.0.0.1:5037"),
            "root": {"type": "disk", "path": "/"},
        }),
        _raw("app-b", {}),
        _raw("other-repo", {"port-cfg-x": _proxy("tcp:127.0.0.1:1", "tcp:127.0.0.1:2")}),
    ]
    got = list_forwards(incus, ["app-a", "app-b"])
    assert sorted(got) == ["app-a", "app-b"]
    assert [f.device for f in got["app-a"]] == ["port-cfg-adb"]
    assert got["app-b"] == []


def test_forwards_for_sorts_by_device_name(mocker):
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        _raw("app-a", {
            "port-th-tcp-8080": _proxy("tcp:127.0.0.1:8080", "tcp:127.0.0.1:8080", "host"),
            "port-cfg-adb": _proxy("tcp:127.0.0.1:5037", "tcp:127.0.0.1:5037"),
        })
    ]
    assert [f.device for f in forwards_for(incus, "app-a")] == [
        "port-cfg-adb",
        "port-th-tcp-8080",
    ]


def test_add_forward_writes_the_device_and_returns_it(mocker):
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [_raw("app-a", {})]
    fwd = add_forward(
        incus,
        "app-a",
        direction="to-container",
        proto="tcp",
        container_port=5037,
        host_port=5037,
        container_address="127.0.0.1",
        host_address="127.0.0.1",
    )
    assert incus.config_device_add.call_args.args == (
        "app-a",
        "port-tc-tcp-5037",
        "proxy",
        {"listen": "tcp:127.0.0.1:5037", "connect": "tcp:127.0.0.1:5037", "bind": "instance"},
    )
    assert fwd.device == "port-tc-tcp-5037"
    assert fwd.direction == "to-container"


def test_add_forward_refuses_a_duplicate_before_calling_incus(mocker):
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        _raw("app-a", {"port-tc-tcp-5037": _proxy("tcp:127.0.0.2:5037", "tcp:127.0.0.1:9999")})
    ]
    with pytest.raises(PortError) as excinfo:
        add_forward(
            incus,
            "app-a",
            direction="to-container",
            proto="tcp",
            container_port=5037,
            host_port=5037,
            container_address="127.0.0.1",
            host_address="127.0.0.1",
        )
    # The existing forward's endpoints are named, so a collision that differs
    # only by address is legible.
    assert "127.0.0.2:5037" in str(excinfo.value)
    assert "127.0.0.1:9999" in str(excinfo.value)
    incus.config_device_add.assert_not_called()


@pytest.mark.parametrize(
    ("incus_message", "expected"),
    [
        ("Error: The device already exists", "already forwarded"),
        (
            'Error: Failed to start device "x": Error occurred when starting '
            "proxy device: Error: Failed to listen on 127.0.0.1:5037: listen "
            "tcp 127.0.0.1:5037: bind: address already in use",
            "host port 5037 is already in use",
        ),
        (
            'Error: Failed to start device "x": Error occurred when starting '
            "proxy device: Error: Failed to receive fd from listener process: "
            "Failed to receive file descriptor via abstract unix socket",
            "something is already listening on port 5037 inside",
        ),
    ],
)
def test_add_forward_translates_incus_errors(mocker, incus_message, expected):
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [_raw("app-a", {})]
    incus.config_device_add.side_effect = IncusError(incus_message)
    with pytest.raises(PortError, match=expected):
        add_forward(
            incus,
            "app-a",
            direction="to-container",
            proto="tcp",
            container_port=5037,
            host_port=5037,
            container_address="127.0.0.1",
            host_address="127.0.0.1",
        )


def test_add_forward_passes_through_an_unrecognised_error(mocker):
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [_raw("app-a", {})]
    incus.config_device_add.side_effect = IncusError("Error: the daemon is on fire")
    with pytest.raises(PortError, match="the daemon is on fire"):
        add_forward(
            incus,
            "app-a",
            direction="to-host",
            proto="tcp",
            container_port=8080,
            host_port=8080,
            container_address="127.0.0.1",
            host_address="127.0.0.1",
        )


def test_resolve_handle_by_device_name_config_name_and_port():
    cfg_fwd = parse_device(
        "port-cfg-adb", _proxy("tcp:127.0.0.1:5037", "tcp:127.0.0.1:5037")
    )
    adhoc = parse_device(
        "port-th-tcp-8080", _proxy("tcp:127.0.0.1:8080", "tcp:127.0.0.1:8080", "host")
    )
    rows = [cfg_fwd, adhoc]
    assert resolve_handle(rows, "port-cfg-adb") is cfg_fwd
    assert resolve_handle(rows, "adb") is cfg_fwd
    assert resolve_handle(rows, "8080") is adhoc


def test_resolve_handle_reports_an_ambiguous_port():
    both = [
        parse_device("port-tc-tcp-5037", _proxy("tcp:127.0.0.1:5037", "tcp:127.0.0.1:1")),
        parse_device("port-th-tcp-5037", _proxy("tcp:127.0.0.1:2", "tcp:127.0.0.1:5037", "host")),
    ]
    with pytest.raises(PortError) as excinfo:
        resolve_handle(both, "5037")
    assert "port-tc-tcp-5037" in str(excinfo.value)
    assert "port-th-tcp-5037" in str(excinfo.value)


def test_resolve_handle_reports_an_unknown_handle():
    with pytest.raises(PortError, match="no forward"):
        resolve_handle([], "nope")


def test_remove_forward_removes_the_resolved_device(mocker):
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        _raw("app-a", {"port-cfg-adb": _proxy("tcp:127.0.0.1:5037", "tcp:127.0.0.1:5037")})
    ]
    fwd = remove_forward(incus, "app-a", "adb")
    assert incus.config_device_remove.call_args.args == ("app-a", "port-cfg-adb")
    assert fwd.device == "port-cfg-adb"
