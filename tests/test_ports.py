"""Tests for port forwarding via Incus proxy devices."""

import pytest

from jailbee.config import HostPort
from jailbee.incus import IncusError
from jailbee.ports import (
    ADHOC_TO_CONTAINER_PREFIX,
    ADHOC_TO_HOST_PREFIX,
    CONFIG_PREFIX,
    PortError,
    ReconcileResult,
    add_forward,
    adhoc_device_name,
    allocate_host_port,
    attach_config_ports,
    check_host_port,
    config_device_name,
    declared_host_ports,
    entry_device,
    forwards_for,
    host_port_free,
    list_forwards,
    parse_device,
    reconcile_config_ports,
    remove_forward,
    render_device,
    resolve_handle,
)
from tests.conftest import make_config


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
        {
            "type": "proxy",
            "bind": "instance",
            "listen": "tcp:127.0.0.1:5037",
            "connect": "tcp:127.0.0.1:5038",
        },
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
        {
            "type": "proxy",
            "bind": "container",
            "listen": "tcp:127.0.0.1:5037",
            "connect": "tcp:127.0.0.1:5037",
        },
    )
    assert fwd is not None
    assert fwd.direction == "to-container"
    assert fwd.source == "other"


def test_parse_device_defaults_bind_to_host():
    fwd = parse_device(
        "port-th-tcp-8080",
        {"type": "proxy", "listen": "tcp:127.0.0.1:18080", "connect": "tcp:127.0.0.1:8080"},
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
        {
            "type": "proxy",
            "bind": "instance",
            "listen": "tcp:[fd00::1]:6000-6002",
            "connect": "tcp:127.0.0.1:5037",
        },
    )
    assert fwd is not None
    assert fwd.container.address == "fd00::1"
    assert fwd.container.port is None
    assert fwd.container.display == "[fd00::1]:6000-6002"


def test_parse_device_handles_unix_endpoints():
    fwd = parse_device(
        "sock",
        {
            "type": "proxy",
            "bind": "instance",
            "listen": "unix:/run/x.sock",
            "connect": "tcp:127.0.0.1:5037",
        },
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
        _raw(
            "app-a",
            {
                "port-cfg-adb": _proxy("tcp:127.0.0.1:5037", "tcp:127.0.0.1:5037"),
                "root": {"type": "disk", "path": "/"},
            },
        ),
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
        _raw(
            "app-a",
            {
                "port-th-tcp-8080": _proxy("tcp:127.0.0.1:8080", "tcp:127.0.0.1:8080", "host"),
                "port-cfg-adb": _proxy("tcp:127.0.0.1:5037", "tcp:127.0.0.1:5037"),
            },
        )
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
            "Host port 5037 is already in use",
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
    cfg_fwd = parse_device("port-cfg-adb", _proxy("tcp:127.0.0.1:5037", "tcp:127.0.0.1:5037"))
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


def test_add_forward_returns_correct_endpoints_to_container(mocker):
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [_raw("app-a", {})]
    fwd = add_forward(
        incus,
        "app-a",
        direction="to-container",
        proto="udp",
        container_port=5353,
        host_port=53,
        container_address="127.0.0.1",
        host_address="192.168.1.100",
    )
    assert fwd.container.raw == "udp:127.0.0.1:5353"
    assert fwd.container.address == "127.0.0.1"
    assert fwd.container.port == 5353
    assert fwd.host.raw == "udp:192.168.1.100:53"
    assert fwd.host.address == "192.168.1.100"
    assert fwd.host.port == 53


def test_add_forward_returns_correct_endpoints_to_host(mocker):
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [_raw("app-a", {})]
    fwd = add_forward(
        incus,
        "app-a",
        direction="to-host",
        proto="tcp",
        container_port=8080,
        host_port=18080,
        container_address="127.0.0.1",
        host_address="192.168.1.50",
    )
    assert fwd.container.raw == "tcp:127.0.0.1:8080"
    assert fwd.container.address == "127.0.0.1"
    assert fwd.container.port == 8080
    assert fwd.host.raw == "tcp:192.168.1.50:18080"
    assert fwd.host.address == "192.168.1.50"
    assert fwd.host.port == 18080


def test_add_forward_brackets_ipv6_in_raw_but_not_address(mocker):
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [_raw("app-a", {})]
    fwd = add_forward(
        incus,
        "app-a",
        direction="to-container",
        proto="tcp",
        container_port=5037,
        host_port=6037,
        container_address="fd00::1",
        host_address="fd00::2",
    )
    assert fwd.container.raw == "tcp:[fd00::1]:5037"
    assert fwd.container.address == "fd00::1"
    assert fwd.container.port == 5037
    assert fwd.host.raw == "tcp:[fd00::2]:6037"
    assert fwd.host.address == "fd00::2"
    assert fwd.host.port == 6037


def test_host_port_free_is_true_for_an_unbound_port():
    # Port 0 asks the OS for a free one, so binding it always succeeds; use a
    # real bind to find a port that is definitely free right now.
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert host_port_free("127.0.0.1", port) is True


def test_host_port_free_is_false_while_something_listens():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        assert host_port_free("127.0.0.1", port) is False


def test_declared_host_ports_maps_port_to_container(mocker):
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        _raw(
            "app-a",
            {
                "port-th-tcp-8080": _proxy("tcp:127.0.0.1:18080", "tcp:127.0.0.1:8080", "host"),
                "port-cfg-adb": _proxy("tcp:127.0.0.1:5037", "tcp:127.0.0.1:5037"),
            },
        ),
        _raw(
            "app-b",
            {
                "port-th-tcp-3000": _proxy("tcp:127.0.0.1:13000", "tcp:127.0.0.1:3000", "host"),
            },
        ),
    ]
    # Only host-bound forwards occupy a host port; a to-container forward's
    # host side is a connect target, not a listener.
    assert declared_host_ports(incus) == {18080: "app-a", 13000: "app-b"}


def test_declared_host_ports_can_exclude_one_container(mocker):
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        _raw(
            "app-a",
            {
                "port-th-tcp-8080": _proxy("tcp:127.0.0.1:18080", "tcp:127.0.0.1:8080", "host"),
            },
        ),
    ]
    assert declared_host_ports(incus, exclude="app-a") == {}


def test_allocate_host_port_skips_taken_ports(mocker):
    calls = iter([5000, 5001, 5002])
    mocker.patch("jailbee.ports._probe_free_port", side_effect=lambda addr: next(calls))
    assert allocate_host_port("127.0.0.1", {5000, 5001}) == 5002


def test_allocate_host_port_gives_up_with_a_clear_error(mocker):
    mocker.patch("jailbee.ports._probe_free_port", return_value=5000)
    with pytest.raises(PortError, match="could not find a free host port"):
        allocate_host_port("127.0.0.1", {5000})


def test_check_host_port_names_the_other_container(mocker):
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        _raw(
            "app-b",
            {
                "port-th-tcp-8080": _proxy("tcp:127.0.0.1:18080", "tcp:127.0.0.1:8080", "host"),
            },
        ),
    ]
    mocker.patch("jailbee.ports.host_port_free", return_value=True)
    with pytest.raises(PortError, match="app-b"):
        check_host_port(incus, "127.0.0.1", 18080, container="app-a")


def test_check_host_port_reports_a_foreign_listener(mocker):
    incus = mocker.MagicMock()
    incus.list_containers.return_value = []
    mocker.patch("jailbee.ports.host_port_free", return_value=False)
    with pytest.raises(PortError, match="already in use on the host"):
        check_host_port(incus, "127.0.0.1", 18080, container="app-a")


def test_check_host_port_accepts_a_free_port(mocker):
    incus = mocker.MagicMock()
    incus.list_containers.return_value = []
    mocker.patch("jailbee.ports.host_port_free", return_value=True)
    check_host_port(incus, "127.0.0.1", 18080, container="app-a")


def _cfg_with_ports(tmp_path, *entries):
    return make_config(tmp_path, host_ports=list(entries))


def test_attach_config_ports_adds_every_entry(tmp_path, mocker):
    cfg = _cfg_with_ports(
        tmp_path,
        {"name": "adb", "port": 5037},
        {"name": "db", "port": 5432, "host_port": 15432},
    )
    incus = mocker.MagicMock()
    added = attach_config_ports(cfg, incus, "app-a")
    assert added == ["port-cfg-adb", "port-cfg-db"]
    assert [c.args[1] for c in incus.config_device_add.call_args_list] == [
        "port-cfg-adb",
        "port-cfg-db",
    ]
    assert incus.config_device_add.call_args_list[1].args[3] == {
        "listen": "tcp:127.0.0.1:5432",
        "connect": "tcp:127.0.0.1:15432",
        "bind": "instance",
    }


def test_attach_config_ports_is_a_noop_without_entries(tmp_path, mocker):
    incus = mocker.MagicMock()
    assert attach_config_ports(make_config(tmp_path), incus, "app-a") == []
    incus.config_device_add.assert_not_called()
    incus.list_containers.assert_not_called()


def test_attach_config_ports_tolerates_an_existing_device(tmp_path, mocker):
    cfg = _cfg_with_ports(tmp_path, {"name": "adb", "port": 5037})
    incus = mocker.MagicMock()
    incus.config_device_add.side_effect = IncusError("Error: The device already exists")
    assert attach_config_ports(cfg, incus, "app-a") == []


def test_attach_config_ports_reraises_other_errors(tmp_path, mocker):
    cfg = _cfg_with_ports(tmp_path, {"name": "adb", "port": 5037})
    incus = mocker.MagicMock()
    incus.config_device_add.side_effect = IncusError("Error: the daemon is on fire")
    with pytest.raises(PortError, match="the daemon is on fire"):
        attach_config_ports(cfg, incus, "app-a")


def test_reconcile_adds_missing_replaces_changed_and_removes_dropped(tmp_path, mocker):
    cfg = _cfg_with_ports(
        tmp_path,
        {"name": "adb", "port": 5037},
        {"name": "db", "port": 5432, "host_port": 15432},
    )
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        _raw(
            "app-a",
            {
                # matches config — must be left alone
                "port-cfg-adb": _proxy("tcp:127.0.0.1:5037", "tcp:127.0.0.1:5037"),
                # host_port changed in config — must be replaced
                "port-cfg-db": _proxy("tcp:127.0.0.1:5432", "tcp:127.0.0.1:5432"),
                # no longer in config — must be removed
                "port-cfg-gone": _proxy("tcp:127.0.0.1:1", "tcp:127.0.0.1:1"),
                # ad hoc — must never be touched
                "port-th-tcp-8080": _proxy("tcp:127.0.0.1:8080", "tcp:127.0.0.1:8080", "host"),
                # someone else's proxy device — must never be touched
                "hand-made": _proxy("tcp:127.0.0.1:9", "tcp:127.0.0.1:9"),
            },
        )
    ]
    result = reconcile_config_ports(cfg, incus, "app-a")
    assert result == ReconcileResult(added=[], replaced=["port-cfg-db"], removed=["port-cfg-gone"])
    assert result.changed is True
    removed = [c.args[1] for c in incus.config_device_remove.call_args_list]
    assert removed == ["port-cfg-db", "port-cfg-gone"]
    assert [c.args[1] for c in incus.config_device_add.call_args_list] == ["port-cfg-db"]
    # Verify the properties written for the replaced device are correct
    assert incus.config_device_add.call_args_list[0].args[3] == {
        "listen": "tcp:127.0.0.1:5432",
        "connect": "tcp:127.0.0.1:15432",
        "bind": "instance",
    }


def test_reconcile_adds_a_missing_entry(tmp_path, mocker):
    cfg = _cfg_with_ports(tmp_path, {"name": "adb", "port": 5037})
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [_raw("app-a", {})]
    result = reconcile_config_ports(cfg, incus, "app-a")
    assert result == ReconcileResult(added=["port-cfg-adb"], replaced=[], removed=[])
    assert result.changed is True


def test_reconcile_is_quiet_when_everything_matches(tmp_path, mocker):
    cfg = _cfg_with_ports(tmp_path, {"name": "adb", "port": 5037})
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        _raw("app-a", {"port-cfg-adb": _proxy("tcp:127.0.0.1:5037", "tcp:127.0.0.1:5037")})
    ]
    result = reconcile_config_ports(cfg, incus, "app-a")
    assert result == ReconcileResult(added=[], replaced=[], removed=[])
    assert result.changed is False
    incus.config_device_add.assert_not_called()
    incus.config_device_remove.assert_not_called()


def test_reconcile_treats_bind_container_alias_as_matching(tmp_path, mocker):
    cfg = _cfg_with_ports(tmp_path, {"name": "adb", "port": 5037})
    incus = mocker.MagicMock()
    # Incus stored bind="container" (an alias for "instance"), with matching endpoints
    incus.list_containers.return_value = [
        _raw(
            "app-a",
            {"port-cfg-adb": _proxy("tcp:127.0.0.1:5037", "tcp:127.0.0.1:5037", "container")},
        )
    ]
    result = reconcile_config_ports(cfg, incus, "app-a")
    # The device should not be considered as needing replacement
    assert result == ReconcileResult(added=[], replaced=[], removed=[])
    assert result.changed is False
    incus.config_device_add.assert_not_called()
    incus.config_device_remove.assert_not_called()


def test_reconcile_uses_prefetched_forwards_without_querying_incus(tmp_path, mocker):
    """`apply` prefetches forwards for every container in one `incus list`
    call; passing them in must skip `forwards_for`'s own query entirely."""
    cfg = _cfg_with_ports(tmp_path, {"name": "adb", "port": 5037})
    incus = mocker.MagicMock()
    fwd = parse_device("port-cfg-adb", _proxy("tcp:127.0.0.1:5037", "tcp:127.0.0.1:5037"))
    assert fwd is not None
    result = reconcile_config_ports(cfg, incus, "app-a", forwards=[fwd])
    assert result == ReconcileResult(added=[], replaced=[], removed=[])
    incus.list_containers.assert_not_called()


def test_reconcile_translates_incus_errors_on_add(tmp_path, mocker):
    """A device-add failure while adding a missing entry must not leak a raw
    Incus error — `reconcile_config_ports` is on `jailbee apply`'s path."""
    cfg = _cfg_with_ports(tmp_path, {"name": "adb", "port": 5037})
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [_raw("app-a", {})]
    incus.config_device_add.side_effect = IncusError(
        'Error: Failed to start device "x": Error occurred when starting proxy '
        "device: Error: Failed to receive fd from listener process: Failed to "
        "receive file descriptor via abstract unix socket"
    )
    with pytest.raises(PortError, match="something is already listening on port 5037"):
        reconcile_config_ports(cfg, incus, "app-a")


def test_reconcile_translates_incus_errors_on_replace(tmp_path, mocker):
    cfg = _cfg_with_ports(tmp_path, {"name": "adb", "port": 5037, "host_port": 15037})
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        _raw("app-a", {"port-cfg-adb": _proxy("tcp:127.0.0.1:5037", "tcp:127.0.0.1:5037")})
    ]
    incus.config_device_add.side_effect = IncusError("Error: the daemon is on fire")
    with pytest.raises(PortError, match="the daemon is on fire"):
        reconcile_config_ports(cfg, incus, "app-a")


def test_reconcile_translates_incus_errors_on_remove(tmp_path, mocker):
    """A stale `port-cfg-*` device (its entry was deleted from config) that
    Incus refuses to remove must also come back translated."""
    cfg = _cfg_with_ports(tmp_path)  # no host_ports entries left
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        _raw("app-a", {"port-cfg-gone": _proxy("tcp:127.0.0.1:1", "tcp:127.0.0.1:1")})
    ]
    incus.config_device_remove.side_effect = IncusError("Error: the daemon is on fire")
    with pytest.raises(PortError, match="the daemon is on fire"):
        reconcile_config_ports(cfg, incus, "app-a")


def test_attach_config_ports_translates_incus_errors(tmp_path, mocker):
    """Mirrors `test_attach_config_ports_reraises_other_errors`, but pins the
    translated (not raw) message for a recognised Incus failure."""
    cfg = _cfg_with_ports(tmp_path, {"name": "adb", "port": 5037})
    incus = mocker.MagicMock()
    incus.config_device_add.side_effect = IncusError(
        'Error: Failed to start device "x": Error occurred when starting proxy '
        "device: Error: Failed to receive fd from listener process: Failed to "
        "receive file descriptor via abstract unix socket"
    )
    with pytest.raises(PortError, match="something is already listening on port 5037"):
        attach_config_ports(cfg, incus, "app-a")


def test_list_forwards_passes_the_timeout_through(mocker):
    """`complete_port_handle` relies on this to bound a wedged daemon."""
    incus = mocker.MagicMock()
    incus.list_containers.return_value = []
    list_forwards(incus, ["app-a"], timeout=2)
    incus.list_containers.assert_called_once_with(timeout=2)


def test_add_forward_duplicate_message_keeps_ipv6_brackets(mocker):
    """The duplicate-forward message embeds both endpoints verbatim; an IPv6
    endpoint's brackets must survive intact (see `error_plain`, the CLI's
    remedy for the surrounding Rich-markup hazard — this test only pins that
    `ports.py` itself hands back the real, bracketed text)."""
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        _raw(
            "app-a",
            {"port-tc-tcp-5037": _proxy("tcp:[fd00::1]:5037", "tcp:127.0.0.1:9999")},
        )
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
    assert "[fd00::1]:5037" in str(excinfo.value)
