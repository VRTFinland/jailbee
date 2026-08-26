"""Tests for network ACL generation."""

import socket as socket_mod
from pathlib import Path

import pytest
import yaml

from jailbee.config import load_config
from jailbee.egress import EgressEntry, build_egress_entries
from jailbee.network import acl_name, allowlist_acl_yaml, entries_from_acl_yaml

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def mock_resolve(mocker):
    """Mock getaddrinfo for fixtures that contain hostnames.

    Returns one canned IPv4 per name so tests can assert structure
    without hitting the network.
    """
    canned = {
        "github.com": ["140.82.121.4"],
        "api.anthropic.com": ["160.79.104.10"],
        # Auto-added by Config.effective_egress_allow() when
        # jetbrains.enabled=true (full_config fixture has this set).
        "account.jetbrains.com": ["13.248.188.196"],
        "oauth.account.jetbrains.com": ["99.80.51.179"],
        "cloudconfig.jetbrains.com": ["52.85.132.10"],
        "plugins.jetbrains.com": ["13.33.235.106"],
        "downloads.marketplace.jetbrains.com": ["3.164.68.53"],
        "www.jetbrains.com": ["13.33.235.20"],
        "resources.jetbrains.com": ["13.33.235.23"],
        "download.jetbrains.com": ["13.33.235.19"],
        "download-cf.jetbrains.com": ["104.18.10.11"],
        "download-cdn.jetbrains.com": ["13.33.235.21"],
        "frameworks.jetbrains.com": ["13.33.235.22"],
        "data.services.jetbrains.com": ["176.34.130.55"],
        "api.jetbrains.cloud": ["13.33.235.24"],
        # Auto-added by Config.effective_egress_allow() when
        # claude.enabled=true (full_config fixture has this set).
        "code.claude.com": ["3.5.6.7"],
        "claude.ai": ["3.5.6.9"],
        "downloads.claude.ai": ["3.5.6.8"],
        # Auto-added by Config.effective_egress_allow() when
        # claude.enabled and claude.plugins_enabled are both true
        # (plugins_enabled defaults to true).
        "api.github.com": ["140.82.121.5"],
        "raw.githubusercontent.com": ["185.199.108.133"],
        "objects.githubusercontent.com": ["185.199.108.134"],
        "codeload.github.com": ["140.82.121.10"],
        "registry.npmjs.org": ["104.16.0.35"],
    }

    def fake_getaddrinfo(name, _port, **kwargs):
        if name not in canned:
            raise socket_mod.gaierror(f"unmocked hostname: {name}")
        return [(socket_mod.AF_INET, socket_mod.SOCK_STREAM, 6, "", (ip, 0)) for ip in canned[name]]

    mocker.patch("socket.getaddrinfo", side_effect=fake_getaddrinfo)


def test_acl_name_uses_container_prefix(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    assert acl_name(make_cfg(repo)) == "myrepo-allowlist"


def test_allowlist_acl_yaml_has_per_repo_name(make_cfg, tmp_path, mock_resolve):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    yaml_text = allowlist_acl_yaml(cfg, build_egress_entries(cfg.effective_egress_allow()))
    assert "name: myrepo-allowlist" in yaml_text


def test_acl_has_no_explicit_reject_egress(mock_resolve):
    """ACL must NOT contain an explicit `action: reject` egress rule.

    Incus prioritises ACL rules by action type (drop > reject > allow >
    default). An explicit reject would be evaluated before allow rules
    and block DHCP/DNS. Default-deny is provided by Incus' implicit
    per-NIC default action, rendered at the tail of the generated chain.
    """
    cfg = load_config(FIXTURES / "full_config.yaml")
    acl_yaml = allowlist_acl_yaml(cfg, build_egress_entries(cfg.effective_egress_allow()))
    parsed = yaml.safe_load(acl_yaml)
    rejects = [r for r in parsed["egress"] if r["action"] == "reject"]
    assert rejects == [], f"unexpected explicit reject in egress: {rejects}"


def test_acl_has_no_explicit_reject_ingress(mock_resolve):
    """Same as above for the ingress side."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    acl_yaml = allowlist_acl_yaml(cfg, build_egress_entries(cfg.effective_egress_allow()))
    parsed = yaml.safe_load(acl_yaml)
    rejects = [r for r in parsed["ingress"] if r["action"] == "reject"]
    assert rejects == [], f"unexpected explicit reject in ingress: {rejects}"


def test_acl_rules_all_have_state_enabled(mock_resolve):
    cfg = load_config(FIXTURES / "full_config.yaml")
    out = allowlist_acl_yaml(cfg, build_egress_entries(cfg.effective_egress_allow()))
    parsed = yaml.safe_load(out)
    for rule in parsed["egress"]:
        assert rule.get("state") == "enabled", f"missing state: {rule}"
    for rule in parsed["ingress"]:
        assert rule.get("state") == "enabled", f"missing state: {rule}"


def test_acl_includes_dns_allow(mock_resolve):
    cfg = load_config(FIXTURES / "full_config.yaml")
    out = allowlist_acl_yaml(cfg, build_egress_entries(cfg.effective_egress_allow()))
    parsed = yaml.safe_load(out)
    rules = parsed["egress"]
    dns = [r for r in rules if r.get("destination_port") == "53"]
    assert len(dns) >= 1


def test_acl_resolves_hostnames_to_ips(mock_resolve):
    """Hostnames in egress_allow are resolved; the ACL contains IPs, not names."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    out = allowlist_acl_yaml(cfg, build_egress_entries(cfg.effective_egress_allow()))
    parsed = yaml.safe_load(out)
    destinations = [r.get("destination", "") for r in parsed["egress"]]
    assert "140.82.121.4" in destinations
    assert "160.79.104.10" in destinations
    # Hostnames themselves are NOT destinations.
    assert "github.com" not in destinations
    assert "api.anthropic.com" not in destinations


def test_acl_rule_description_preserves_original_entry(mock_resolve):
    cfg = load_config(FIXTURES / "full_config.yaml")
    out = allowlist_acl_yaml(cfg, build_egress_entries(cfg.effective_egress_allow()))
    parsed = yaml.safe_load(out)
    descs = [r.get("description", "") for r in parsed["egress"]]
    assert any("github.com" in d for d in descs)
    assert any("api.anthropic.com" in d for d in descs)


def test_acl_port_specific_rule_has_tcp_protocol(make_cfg, tmp_path, mocker):
    """A `host:port` entry should produce a TCP rule with destination_port set."""
    mocker.patch(
        "socket.getaddrinfo",
        return_value=[(socket_mod.AF_INET, socket_mod.SOCK_STREAM, 6, "", ("1.2.3.4", 0))],
    )
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    # make_cfg() leaves jetbrains.enabled=False (its default), so no
    # auto-added JetBrains hosts can poison the "exactly one match"
    # assertion below by resolving to the same mock IP.
    cfg.egress_allow.append("example.com:22")
    out = allowlist_acl_yaml(cfg, build_egress_entries(cfg.effective_egress_allow()))
    parsed = yaml.safe_load(out)
    matching = [r for r in parsed["egress"] if r.get("destination") == "1.2.3.4"]
    assert len(matching) == 1
    assert matching[0]["protocol"] == "tcp"
    assert matching[0]["destination_port"] == "22"


def test_acl_bare_host_has_no_port_field(make_cfg, tmp_path, mocker):
    """A bare hostname (no port) produces a rule without destination_port."""
    mocker.patch(
        "socket.getaddrinfo",
        return_value=[(socket_mod.AF_INET, socket_mod.SOCK_STREAM, 6, "", ("5.6.7.8", 0))],
    )
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    # make_cfg() leaves jetbrains.enabled=False — see sibling test above.
    cfg.egress_allow.append("example.com")
    out = allowlist_acl_yaml(cfg, build_egress_entries(cfg.effective_egress_allow()))
    parsed = yaml.safe_load(out)
    matching = [r for r in parsed["egress"] if r.get("destination") == "5.6.7.8"]
    assert len(matching) == 1
    assert "destination_port" not in matching[0]
    assert "protocol" not in matching[0]


def test_acl_egress_all_allow(mock_resolve):
    """Every egress rule is `allow` — default-deny is implicit via NIC."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    acl_yaml = allowlist_acl_yaml(cfg, build_egress_entries(cfg.effective_egress_allow()))
    parsed = yaml.safe_load(acl_yaml)
    actions = [r["action"] for r in parsed["egress"]]
    assert actions and all(a == "allow" for a in actions), actions


def test_acl_egress_allows_dhcpv4(mock_resolve):
    """Default-deny ACL must allow DHCPv4 client→server."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    acl_yaml = allowlist_acl_yaml(cfg, build_egress_entries(cfg.effective_egress_allow()))
    parsed = yaml.safe_load(acl_yaml)
    dhcp = [
        r
        for r in parsed["egress"]
        if r.get("destination_port") == "67" and r.get("protocol") == "udp"
    ]
    assert len(dhcp) == 1
    assert dhcp[0]["action"] == "allow"


def test_acl_egress_allows_dhcpv6(mock_resolve):
    """Default-deny ACL must allow DHCPv6 client→server."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    acl_yaml = allowlist_acl_yaml(cfg, build_egress_entries(cfg.effective_egress_allow()))
    parsed = yaml.safe_load(acl_yaml)
    dhcp = [
        r
        for r in parsed["egress"]
        if r.get("destination_port") == "547" and r.get("protocol") == "udp"
    ]
    assert len(dhcp) == 1
    assert dhcp[0]["action"] == "allow"


def test_acl_ingress_allows_dhcpv4(mock_resolve):
    """Default-deny ingress must allow DHCPv4 server→client reply."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    acl_yaml = allowlist_acl_yaml(cfg, build_egress_entries(cfg.effective_egress_allow()))
    parsed = yaml.safe_load(acl_yaml)
    dhcp = [
        r
        for r in parsed["ingress"]
        if r.get("destination_port") == "68" and r.get("protocol") == "udp"
    ]
    assert len(dhcp) == 1
    assert dhcp[0]["action"] == "allow"


def test_acl_ingress_allows_dhcpv6(mock_resolve):
    """Default-deny ingress must allow DHCPv6 server→client reply."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    acl_yaml = allowlist_acl_yaml(cfg, build_egress_entries(cfg.effective_egress_allow()))
    parsed = yaml.safe_load(acl_yaml)
    dhcp = [
        r
        for r in parsed["ingress"]
        if r.get("destination_port") == "546" and r.get("protocol") == "udp"
    ]
    assert len(dhcp) == 1
    assert dhcp[0]["action"] == "allow"


def test_acl_ingress_all_allow(mock_resolve):
    """Every ingress rule is `allow` — default-deny is implicit via NIC."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    acl_yaml = allowlist_acl_yaml(cfg, build_egress_entries(cfg.effective_egress_allow()))
    parsed = yaml.safe_load(acl_yaml)
    actions = [r["action"] for r in parsed["ingress"]]
    assert actions and all(a == "allow" for a in actions), actions


# ---- entries_from_acl_yaml: reverse direction (ACL → EgressEntry list) -----


def test_entries_from_acl_yaml_empty():
    """ACL with no `allowlisted:` rules yields no entries."""
    acl = yaml.safe_dump(
        {
            "egress": [
                {"action": "allow", "destination_port": "53", "description": "DNS"},
            ],
        }
    )
    assert entries_from_acl_yaml(acl) == []


def test_entries_from_acl_yaml_single_host_single_ip():
    acl = yaml.safe_dump(
        {
            "egress": [
                {
                    "action": "allow",
                    "destination_port": "53",
                    "description": "DNS",
                },
                {
                    "action": "allow",
                    "destination": "140.82.121.4",
                    "description": "allowlisted: github.com",
                    "state": "enabled",
                },
            ],
        }
    )
    assert entries_from_acl_yaml(acl) == [
        EgressEntry(destinations=["140.82.121.4"], port=None, description="github.com"),
    ]


def test_entries_from_acl_yaml_single_host_multiple_ips_preserve_order():
    """Multiple rules for the same description coalesce into one entry."""
    acl = yaml.safe_dump(
        {
            "egress": [
                {
                    "action": "allow",
                    "destination": "140.82.121.4",
                    "description": "allowlisted: github.com",
                    "state": "enabled",
                },
                {
                    "action": "allow",
                    "destination": "140.82.121.3",
                    "description": "allowlisted: github.com",
                    "state": "enabled",
                },
            ],
        }
    )
    entries = entries_from_acl_yaml(acl)
    assert len(entries) == 1
    assert entries[0].description == "github.com"
    # Preserve the order they appear in the ACL.
    assert entries[0].destinations == ["140.82.121.4", "140.82.121.3"]


def test_entries_from_acl_yaml_with_port():
    acl = yaml.safe_dump(
        {
            "egress": [
                {
                    "action": "allow",
                    "destination": "140.82.121.4",
                    "protocol": "tcp",
                    "destination_port": "22",
                    "description": "allowlisted: github.com:22",
                    "state": "enabled",
                },
            ],
        }
    )
    assert entries_from_acl_yaml(acl) == [
        EgressEntry(destinations=["140.82.121.4"], port=22, description="github.com:22"),
    ]


def test_entries_from_acl_yaml_multiple_hosts_preserve_order():
    acl = yaml.safe_dump(
        {
            "egress": [
                {
                    "action": "allow",
                    "destination": "140.82.121.4",
                    "description": "allowlisted: github.com",
                    "state": "enabled",
                },
                {
                    "action": "allow",
                    "destination": "160.79.104.10",
                    "description": "allowlisted: api.anthropic.com",
                    "state": "enabled",
                },
            ],
        }
    )
    entries = entries_from_acl_yaml(acl)
    assert [e.description for e in entries] == ["github.com", "api.anthropic.com"]


def test_entries_from_acl_yaml_skips_rules_without_description():
    """Rules without an `allowlisted: ` description (DNS, DHCP) are ignored."""
    acl = yaml.safe_dump(
        {
            "egress": [
                {"action": "allow", "destination_port": "53", "description": "DNS"},
                {
                    "action": "allow",
                    "destination_port": "67",
                    "protocol": "udp",
                    "description": "DHCPv4 client → server",
                },
            ],
        }
    )
    assert entries_from_acl_yaml(acl) == []


def test_entries_from_acl_yaml_roundtrip(mock_resolve):
    """allowlist_acl_yaml → entries_from_acl_yaml round-trips.

    All hostnames from the full_config.yaml fixture surface as entries,
    with ports preserved on `host:port` rules.
    """
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg.egress_allow.append("github.com:22")  # add a port entry too
    acl_text = allowlist_acl_yaml(cfg, build_egress_entries(cfg.effective_egress_allow()))
    entries = entries_from_acl_yaml(acl_text)
    descs = [e.description for e in entries]
    assert "github.com" in descs
    # api.anthropic.com:443 is auto-added via CLAUDE_API_HOSTS because the
    # fixture sets `claude.enabled: true` (the host name carries the :443
    # port suffix as configured in CLAUDE_API_HOSTS).
    assert "api.anthropic.com:443" in descs
    assert "github.com:22" in descs
    port_entry = next(e for e in entries if e.description == "github.com:22")
    assert port_entry.port == 22


def test_entries_from_acl_yaml_empty_input():
    """Empty or None ACL YAML is handled gracefully."""
    assert entries_from_acl_yaml("") == []
    assert entries_from_acl_yaml("egress: []\n") == []


def test_acl_includes_mirror_rule_when_mirror_endpoint_supplied(make_cfg, tmp_path, mock_resolve):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    out = allowlist_acl_yaml(
        cfg,
        build_egress_entries(cfg.effective_egress_allow()),
        mirror_endpoint=("10.234.216.1", 15000),
    )
    parsed = yaml.safe_load(out)
    mirror = [r for r in parsed["egress"] if r.get("destination") == "10.234.216.1"]
    assert len(mirror) == 1
    rule = mirror[0]
    assert rule["destination_port"] == "15000"
    assert rule["protocol"] == "tcp"
    assert rule["action"] == "allow"
    assert rule["state"] == "enabled"
    # Description deliberately lacks the "allowlisted: " prefix so the
    # /etc/hosts pinning code ignores this rule.
    assert not rule["description"].startswith("allowlisted: ")
    assert "registry mirror" in rule["description"].lower()


def test_acl_omits_mirror_rule_when_mirror_endpoint_is_none(make_cfg, tmp_path, mock_resolve):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    out = allowlist_acl_yaml(cfg, build_egress_entries(cfg.effective_egress_allow()))
    parsed = yaml.safe_load(out)
    mirror = [r for r in parsed["egress"] if r.get("destination") == "10.234.216.1"]
    assert mirror == []


def test_entries_from_acl_yaml_ignores_mirror_rule(make_cfg, tmp_path, mock_resolve):
    """The mirror rule must not surface as an EgressEntry — otherwise
    /etc/hosts pinning would try to write a host line for the gateway IP.
    """
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    out = allowlist_acl_yaml(cfg, [], mirror_endpoint=("10.234.216.1", 15000))
    entries = entries_from_acl_yaml(out)
    destinations = [d for e in entries for d in e.destinations]
    assert "10.234.216.1" not in destinations


def test_extra_acl_yaml_has_allow_rules_only(make_cfg, tmp_path):
    import yaml

    from jailbee.egress import EgressEntry
    from jailbee.network import extra_acl_yaml

    parsed = yaml.safe_load(
        extra_acl_yaml(
            "myrepo-feat-extra",
            [EgressEntry(destinations=["10.0.5.7"], port=443, description="nexus.corp:443")],
        )
    )

    assert parsed["name"] == "myrepo-feat-extra"
    descriptions = [r["description"] for r in parsed["egress"]]
    # DHCP/DNS/mirror rules belong to the repo ACL applied to the same NIC.
    assert descriptions == ["allowlisted: nexus.corp:443"]
    assert parsed["egress"][0]["action"] == "allow"
    assert parsed["egress"][0]["destination"] == "10.0.5.7"
    assert parsed["egress"][0]["destination_port"] == "443"
    assert parsed["ingress"] == []


def test_extra_acl_yaml_bare_host_has_no_port_field():
    import yaml

    from jailbee.egress import EgressEntry
    from jailbee.network import extra_acl_yaml

    parsed = yaml.safe_load(
        extra_acl_yaml(
            "myrepo-feat-extra",
            [EgressEntry(destinations=["10.0.5.7"], port=None, description="nexus.corp")],
        )
    )
    assert "destination_port" not in parsed["egress"][0]
    assert "protocol" not in parsed["egress"][0]


def test_extra_acl_yaml_round_trips_through_entries_from_acl_yaml():
    from jailbee.egress import EgressEntry
    from jailbee.network import entries_from_acl_yaml, extra_acl_yaml

    entries = [
        EgressEntry(destinations=["10.0.5.7", "10.0.5.8"], port=443, description="nexus.corp:443")
    ]
    assert entries_from_acl_yaml(extra_acl_yaml("x-extra", entries)) == entries


def test_allowlist_acl_yaml_requires_entries(make_cfg, tmp_path):
    import pytest

    from jailbee.network import allowlist_acl_yaml

    cfg = make_cfg(tmp_path / "myrepo")
    with pytest.raises(TypeError):
        allowlist_acl_yaml(cfg)  # type: ignore[call-arg]
