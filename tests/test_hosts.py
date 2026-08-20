"""Tests for /etc/hosts pinning helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

import yaml

from jailbee.docker_daemon import MIRROR_DNS_NAME
from jailbee.egress import EgressEntry
from jailbee.hosts import (
    BEGIN_SENTINEL,
    END_SENTINEL,
    apply_hosts,
    clear_hosts,
    render_hosts_block,
)


def _make_cfg(allow: list[str]):
    """Build a Config-like stub exposing what apply_hosts needs.

    container_prefix is consulted by `acl_name()` when apply_hosts needs
    to look up the live ACL.
    """
    cfg = MagicMock()
    cfg.egress_allow = allow
    cfg.container_prefix = "myrepo"
    return cfg


def _acl_yaml(rules: list[dict]) -> str:
    return yaml.safe_dump({"egress": rules})


def _allowlisted(raw: str, ips: list[str], port: int | None = None) -> list[dict]:
    rules: list[dict] = []
    for ip in ips:
        rule: dict = {
            "action": "allow",
            "destination": ip,
            "description": f"allowlisted: {raw}",
            "state": "enabled",
        }
        if port is not None:
            rule["protocol"] = "tcp"
            rule["destination_port"] = str(port)
        rules.append(rule)
    return rules


# ---- render_hosts_block (pure) ---------------------------------------------


def test_render_empty_when_entries_empty():
    assert render_hosts_block([]) == ""


def test_render_skips_literal_entries():
    entries = [
        EgressEntry(destinations=["10.0.0.0/8"], port=None, description="10.0.0.0/8"),
        EgressEntry(destinations=["192.168.1.5"], port=5432, description="192.168.1.5:5432"),
    ]
    assert render_hosts_block(entries) == ""


def test_render_single_hostname_single_ip():
    entries = [EgressEntry(destinations=["140.82.121.4"], port=None, description="github.com")]
    out = render_hosts_block(entries)

    assert out.startswith(BEGIN_SENTINEL)
    assert out.rstrip().endswith(END_SENTINEL)
    assert "140.82.121.4 github.com" in out


def test_render_hostname_with_port_treated_as_bare_hostname():
    entries = [EgressEntry(destinations=["140.82.121.4"], port=22, description="github.com:22")]
    out = render_hosts_block(entries)

    assert "140.82.121.4 github.com" in out
    assert ":22" not in out


def test_render_deduplicates_same_hostname():
    """Multiple entries for the same hostname produce one set of lines."""
    entries = [
        EgressEntry(destinations=["140.82.121.4"], port=None, description="github.com"),
        EgressEntry(destinations=["140.82.121.4"], port=22, description="github.com:22"),
        EgressEntry(destinations=["140.82.121.4"], port=443, description="github.com:443"),
    ]
    out = render_hosts_block(entries)

    assert out.count("github.com") == 1


def test_render_multi_ip_per_hostname():
    entries = [
        EgressEntry(
            destinations=["104.16.132.229", "104.16.133.229"],
            port=None,
            description="cloudflare.com",
        ),
    ]
    out = render_hosts_block(entries)

    assert "104.16.132.229 cloudflare.com" in out
    assert "104.16.133.229 cloudflare.com" in out


def test_render_skips_ip_and_cidr_literals_mixed():
    entries = [
        EgressEntry(destinations=["140.82.121.4"], port=None, description="github.com"),
        EgressEntry(destinations=["192.168.1.5"], port=None, description="192.168.1.5"),
        EgressEntry(destinations=["10.0.0.0/8"], port=None, description="10.0.0.0/8"),
    ]
    out = render_hosts_block(entries)

    assert "github.com" in out
    assert "192.168.1.5" not in out
    assert "10.0.0.0/8" not in out


def test_render_preserves_ip_order():
    entries = [
        EgressEntry(
            destinations=["1.2.3.4", "5.6.7.8", "9.10.11.12"],
            port=None,
            description="x.example",
        ),
    ]
    out = render_hosts_block(entries)

    idx = [out.index(ip) for ip in ("1.2.3.4", "5.6.7.8", "9.10.11.12")]
    assert idx == sorted(idx)


# ---- render_hosts_block: mirror_endpoint ------------------------------------


def test_render_omits_mirror_when_endpoint_is_none():
    entries = [EgressEntry(destinations=["140.82.121.4"], port=None, description="github.com")]
    out = render_hosts_block(entries, mirror_endpoint=None)

    assert MIRROR_DNS_NAME not in out


def test_render_includes_mirror_when_endpoint_given():
    """Strict containers query incusbr0's dnsmasq, which can't see the mirror
    on jailbee-loose — so /etc/hosts must pin the IPv4."""
    entries = [EgressEntry(destinations=["140.82.121.4"], port=None, description="github.com")]
    out = render_hosts_block(entries, mirror_endpoint=("10.42.0.7", 3128))

    assert f"10.42.0.7 {MIRROR_DNS_NAME}" in out
    assert "140.82.121.4 github.com" in out


def test_render_mirror_only_when_no_hostname_entries():
    """If the ACL has no hostname entries but the mirror endpoint is given,
    the block is still emitted with just the mirror row."""
    out = render_hosts_block([], mirror_endpoint=("10.42.0.7", 3128))

    assert out.startswith(BEGIN_SENTINEL)
    assert out.rstrip().endswith(END_SENTINEL)
    assert f"10.42.0.7 {MIRROR_DNS_NAME}" in out


def test_render_mirror_uses_ipv4_not_port():
    """The /etc/hosts row is hostname→IP only; the port stays in the URL."""
    out = render_hosts_block([], mirror_endpoint=("10.42.0.7", 3128))

    assert ":3128" not in out


# ---- apply_hosts: reads ACL by default --------------------------------------


def test_apply_hosts_reads_live_acl_when_no_entries():
    """Default path: query incus.network_acl_show, mirror its destinations."""
    cfg = _make_cfg(["github.com"])
    incus = MagicMock()
    incus.network_acl_show.return_value = _acl_yaml(_allowlisted("github.com", ["140.82.121.4"]))

    apply_hosts(cfg, incus, "myrepo-feat-x")

    incus.network_acl_show.assert_called_once_with("myrepo-allowlist")
    args, _ = incus.exec.call_args
    script = args[1][2]
    assert "140.82.121.4 github.com" in script


def test_apply_hosts_uses_passed_entries_and_skips_acl_read():
    """When `entries` is provided, no ACL lookup happens — same data feeds both."""
    cfg = _make_cfg(["github.com"])
    incus = MagicMock()
    entries = [EgressEntry(destinations=["5.5.5.5"], port=None, description="github.com")]

    apply_hosts(cfg, incus, "myrepo-feat-x", entries=entries)

    incus.network_acl_show.assert_not_called()
    args, _ = incus.exec.call_args
    script = args[1][2]
    assert "5.5.5.5 github.com" in script


def test_apply_hosts_does_not_resolve_dns(mocker):
    """The default path must NOT call socket.getaddrinfo (ACL is the source of truth)."""
    cfg = _make_cfg(["github.com"])
    incus = MagicMock()
    incus.network_acl_show.return_value = _acl_yaml(_allowlisted("github.com", ["140.82.121.4"]))
    gai = mocker.patch("socket.getaddrinfo")

    apply_hosts(cfg, incus, "myrepo-feat-x")

    gai.assert_not_called()


def test_apply_hosts_skips_exec_when_acl_has_no_hostnames():
    """ACL with only literal/CIDR rules → empty block → no incus.exec call."""
    cfg = _make_cfg(["192.168.1.5"])
    incus = MagicMock()
    incus.network_acl_show.return_value = _acl_yaml(_allowlisted("192.168.1.5", ["192.168.1.5"]))

    apply_hosts(cfg, incus, "myrepo-feat-x")

    incus.exec.assert_not_called()


def test_apply_hosts_invokes_incus_exec_with_bash_script():
    cfg = _make_cfg(["github.com"])
    incus = MagicMock()
    incus.network_acl_show.return_value = _acl_yaml(_allowlisted("github.com", ["140.82.121.4"]))

    apply_hosts(cfg, incus, "myrepo-feat-x")

    assert incus.exec.call_count == 1
    args, _kwargs = incus.exec.call_args
    container = args[0]
    cmd = args[1]
    assert container == "myrepo-feat-x"
    assert cmd[0] == "bash"
    assert cmd[1] == "-c"
    script = cmd[2]
    assert "140.82.121.4 github.com" in script
    assert BEGIN_SENTINEL in script
    assert END_SENTINEL in script
    assert "mktemp" in script
    assert "mv " in script


def test_apply_hosts_script_strips_existing_block():
    cfg = _make_cfg(["github.com"])
    incus = MagicMock()
    incus.network_acl_show.return_value = _acl_yaml(_allowlisted("github.com", ["140.82.121.4"]))

    apply_hosts(cfg, incus, "myrepo-feat-x")

    script = incus.exec.call_args.args[1][2]
    assert "BEGIN jailbee-managed allowlist" in script
    assert "END jailbee-managed allowlist" in script
    assert "awk" in script
    # Anchored: an unanchored pattern would also match a sentinel quoted
    # inside a user-written comment further down /etc/hosts.
    assert "^# BEGIN jailbee-managed allowlist" in script
    assert "^# END jailbee-managed allowlist" in script


# ---- apply_hosts: mirror_endpoint -------------------------------------------


def test_apply_hosts_pins_mirror_when_endpoint_passed():
    cfg = _make_cfg(["github.com"])
    incus = MagicMock()
    incus.network_acl_show.return_value = _acl_yaml(_allowlisted("github.com", ["140.82.121.4"]))

    apply_hosts(cfg, incus, "myrepo-feat-x", mirror_endpoint=("10.42.0.7", 3128))

    script = incus.exec.call_args.args[1][2]
    assert f"10.42.0.7 {MIRROR_DNS_NAME}" in script


def test_apply_hosts_does_not_pin_mirror_when_endpoint_omitted():
    cfg = _make_cfg(["github.com"])
    incus = MagicMock()
    incus.network_acl_show.return_value = _acl_yaml(_allowlisted("github.com", ["140.82.121.4"]))

    apply_hosts(cfg, incus, "myrepo-feat-x")

    script = incus.exec.call_args.args[1][2]
    assert MIRROR_DNS_NAME not in script


def test_apply_hosts_emits_block_for_mirror_only_acl():
    """ACL with only literal entries + mirror_endpoint → block written
    (only mirror row), not skipped as `no hostname entries → no-op`."""
    cfg = _make_cfg(["192.168.1.5"])
    incus = MagicMock()
    incus.network_acl_show.return_value = _acl_yaml(_allowlisted("192.168.1.5", ["192.168.1.5"]))

    apply_hosts(cfg, incus, "myrepo-feat-x", mirror_endpoint=("10.42.0.7", 3128))

    assert incus.exec.call_count == 1
    script = incus.exec.call_args.args[1][2]
    assert f"10.42.0.7 {MIRROR_DNS_NAME}" in script


# ---- clear_hosts ------------------------------------------------------------


def test_clear_hosts_runs_strip_only_script():
    cfg = _make_cfg(["github.com"])
    incus = MagicMock()

    clear_hosts(cfg, incus, "myrepo-feat-x")

    assert incus.exec.call_count == 1
    args, _ = incus.exec.call_args
    assert args[0] == "myrepo-feat-x"
    cmd = args[1]
    assert cmd[0] == "bash"
    script = cmd[2]

    assert "BEGIN jailbee-managed allowlist" in script
    assert "END jailbee-managed allowlist" in script
    assert "awk" in script
    assert "mv " in script
    # No rendered block content — clear only.
    assert "github.com" not in script


def test_clear_hosts_does_not_resolve_hostnames(mocker):
    """clear_hosts must not touch DNS even if egress_allow has names."""
    gai = mocker.patch("socket.getaddrinfo")
    cfg = _make_cfg(["github.com"])
    incus = MagicMock()

    clear_hosts(cfg, incus, "myrepo-feat-x")

    gai.assert_not_called()


def test_clear_hosts_strips_the_managed_block():
    incus = MagicMock()

    clear_hosts(_make_cfg(["github.com"]), incus, "myrepo-feat-x")

    script = incus.exec.call_args.args[1][2]
    assert "^# BEGIN jailbee-managed allowlist" in script
    assert "^# END jailbee-managed allowlist" in script
