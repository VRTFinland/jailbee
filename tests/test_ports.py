"""Tests for port forwarding via Incus proxy devices."""

from jailbee.config import HostPort
from jailbee.ports import (
    ADHOC_TO_CONTAINER_PREFIX,
    ADHOC_TO_HOST_PREFIX,
    CONFIG_PREFIX,
    adhoc_device_name,
    config_device_name,
    entry_device,
    parse_device,
    render_device,
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
    name, props = entry_device(HostPort(name="adb", port=5037, host_port=6037))
    assert name == "port-cfg-adb"
    assert props == {
        "listen": "tcp:127.0.0.1:5037",
        "connect": "tcp:127.0.0.1:6037",
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
