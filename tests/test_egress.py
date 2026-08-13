"""Tests for egress allowlist parser and resolver."""

from __future__ import annotations

import pytest

from jailbee.egress import (
    EgressSpec,
    NetworkResolveError,
    parse_egress_entry,
)


def test_parse_bare_hostname():
    spec = parse_egress_entry("github.com")
    assert spec == EgressSpec(target="github.com", port=None, is_literal=False)


def test_parse_hostname_with_port():
    spec = parse_egress_entry("github.com:22")
    assert spec == EgressSpec(target="github.com", port=22, is_literal=False)


def test_parse_bare_ipv4():
    spec = parse_egress_entry("192.168.1.5")
    assert spec == EgressSpec(target="192.168.1.5", port=None, is_literal=True)


def test_parse_ipv4_with_port():
    spec = parse_egress_entry("192.168.1.5:5432")
    assert spec == EgressSpec(target="192.168.1.5", port=5432, is_literal=True)


def test_parse_bare_cidr():
    spec = parse_egress_entry("10.0.0.0/8")
    assert spec == EgressSpec(target="10.0.0.0/8", port=None, is_literal=True)


def test_parse_cidr_with_port():
    spec = parse_egress_entry("10.0.0.0/8:5432")
    assert spec == EgressSpec(target="10.0.0.0/8", port=5432, is_literal=True)


def test_parse_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        parse_egress_entry("")


def test_parse_rejects_port_out_of_range():
    with pytest.raises(ValueError, match="port"):
        parse_egress_entry("github.com:0")
    with pytest.raises(ValueError, match="port"):
        parse_egress_entry("github.com:65536")


def test_parse_rejects_non_numeric_port():
    with pytest.raises(ValueError, match="port"):
        parse_egress_entry("github.com:abc")


def test_parse_rejects_garbage_host():
    with pytest.raises(ValueError, match="host"):
        parse_egress_entry("not a valid host!")


# --- resolver tests ---

import socket as socket_mod  # noqa: E402

from jailbee.egress import resolve_hostnames  # noqa: E402


def _gai_result(ips: list[str]) -> list[tuple]:
    """Build a fake getaddrinfo() return value for the given IPv4 strings."""
    return [(socket_mod.AF_INET, socket_mod.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]


def test_resolve_single_ip(mocker):
    mocker.patch(
        "socket.getaddrinfo",
        return_value=_gai_result(["140.82.121.4"]),
    )
    result = resolve_hostnames(["github.com"])
    assert result == {"github.com": ["140.82.121.4"]}


def test_resolve_multiple_ips_sorted(mocker):
    mocker.patch(
        "socket.getaddrinfo",
        return_value=_gai_result(["140.82.121.4", "140.82.114.4"]),
    )
    result = resolve_hostnames(["github.com"])
    assert result == {"github.com": ["140.82.114.4", "140.82.121.4"]}


def test_resolve_deduplicates(mocker):
    mocker.patch(
        "socket.getaddrinfo",
        return_value=_gai_result(["1.2.3.4", "1.2.3.4", "5.6.7.8"]),
    )
    result = resolve_hostnames(["example.com"])
    assert result == {"example.com": ["1.2.3.4", "5.6.7.8"]}


def test_resolve_multiple_names(mocker):
    gai = mocker.patch("socket.getaddrinfo")
    gai.side_effect = [
        _gai_result(["1.1.1.1"]),
        _gai_result(["2.2.2.2"]),
    ]
    result = resolve_hostnames(["a.example", "b.example"])
    assert result == {"a.example": ["1.1.1.1"], "b.example": ["2.2.2.2"]}


def test_resolve_empty_input():
    assert resolve_hostnames([]) == {}


def test_resolve_gaierror_raises(mocker):
    mocker.patch("socket.getaddrinfo", side_effect=socket_mod.gaierror("no such host"))
    with pytest.raises(NetworkResolveError) as excinfo:
        resolve_hostnames(["nosuch.example"])
    assert excinfo.value.hostname == "nosuch.example"


def test_resolve_empty_result_raises(mocker):
    mocker.patch("socket.getaddrinfo", return_value=[])
    with pytest.raises(NetworkResolveError) as excinfo:
        resolve_hostnames(["aaaa-only.example"])
    assert excinfo.value.hostname == "aaaa-only.example"


# --- build_egress_entries tests ---

from jailbee.egress import EgressEntry, build_egress_entries  # noqa: E402


def test_build_entries_literal_only():
    entries = build_egress_entries(["10.0.0.0/8", "192.168.1.5:5432"])
    assert entries == [
        EgressEntry(destinations=["10.0.0.0/8"], port=None, description="10.0.0.0/8"),
        EgressEntry(destinations=["192.168.1.5"], port=5432, description="192.168.1.5:5432"),
    ]


def test_build_entries_resolves_hostnames(mocker):
    mocker.patch(
        "socket.getaddrinfo",
        return_value=_gai_result(["140.82.121.4", "140.82.114.4"]),
    )
    entries = build_egress_entries(["github.com:22"])
    assert entries == [
        EgressEntry(
            destinations=["140.82.114.4", "140.82.121.4"],
            port=22,
            description="github.com:22",
        ),
    ]


def test_build_entries_mixed(mocker):
    canned = {
        "github.com": ["1.1.1.1"],
        "api.anthropic.com": ["2.2.2.2"],
    }

    def fake(name, _port, **kwargs):
        return _gai_result(canned[name])

    mocker.patch("socket.getaddrinfo", side_effect=fake)
    entries = build_egress_entries(
        [
            "10.0.0.0/8",
            "github.com",
            "api.anthropic.com:443",
        ]
    )
    assert [e.description for e in entries] == [
        "10.0.0.0/8",
        "github.com",
        "api.anthropic.com:443",
    ]
    assert entries[0].destinations == ["10.0.0.0/8"]
    assert entries[1].destinations == ["1.1.1.1"]
    assert entries[2].destinations == ["2.2.2.2"]
    assert entries[2].port == 443


def test_build_entries_same_hostname_twice_resolves_once(mocker):
    gai = mocker.patch("socket.getaddrinfo", return_value=_gai_result(["1.1.1.1"]))
    build_egress_entries(["github.com", "github.com:22"])
    assert gai.call_count == 1


def test_build_entries_empty():
    assert build_egress_entries([]) == []


def test_build_entries_propagates_resolve_error(mocker):
    mocker.patch("socket.getaddrinfo", side_effect=socket_mod.gaierror("nope"))
    with pytest.raises(NetworkResolveError):
        build_egress_entries(["nosuch.example"])


def test_build_entries_propagates_parse_error():
    with pytest.raises(ValueError, match="port"):
        build_egress_entries(["github.com:99999"])
