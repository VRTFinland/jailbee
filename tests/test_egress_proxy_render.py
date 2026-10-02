from jailbee.egress_proxy_render import (
    BASE_SQUID_CONF,
    ProxyScope,
    proxy_env,
    render_fragment,
)


def _scope(key="myrepo", sources=("10.1.0.5",), entries=("*.vendor.com",)):
    return ProxyScope(key=key, sources=tuple(sources), entries=tuple(entries))


def test_wildcard_defaults_to_80_and_443():
    out = render_fragment("myrepo", [_scope()])
    assert "acl jb_myrepo_src src 10.1.0.5/32" in out
    assert "acl jb_myrepo_d0 dstdomain -n .vendor.com" in out
    assert "acl jb_myrepo_p0 port 80 443" in out
    assert "http_access allow jb_myrepo_src jb_myrepo_d0 jb_myrepo_p0" in out


def test_hostname_without_port_allows_all_ports():
    out = render_fragment("myrepo", [_scope(entries=("github.com",))])
    assert "dstdomain -n github.com" in out
    assert " port " not in out
    assert "http_access allow jb_myrepo_src jb_myrepo_d0\n" in out


def test_explicit_ports_get_their_own_groups():
    out = render_fragment("r", [_scope(key="r", entries=("*.a.com:8443", "b.com:22", "*.c.com"))])
    assert "port 8443" in out and "port 22" in out and "port 80 443" in out
    assert out.count("http_access allow") == 3


def test_ip_and_cidr_entries_render_as_dst():
    out = render_fragment("r", [_scope(key="r", entries=("10.0.0.0/8", "192.168.1.5:5432"))])
    assert "dst 10.0.0.0/8" in out
    assert "dst 192.168.1.5/32" in out
    assert "port 5432" in out


def test_scope_without_sources_or_entries_is_omitted():
    out = render_fragment("r", [_scope(key="r", sources=()), _scope(key="r-ct", entries=())])
    assert "acl " not in out and "http_access" not in out


def test_sources_never_leak_between_scopes():
    out = render_fragment(
        "r",
        [
            _scope(key="r", sources=("10.1.0.5",), entries=("*.a.com",)),
            _scope(key="r-ct", sources=("10.1.0.9",), entries=("*.secret.com",)),
        ],
    )
    for line in out.splitlines():
        if ".secret.com" in line:
            assert line.startswith("acl jb_r-ct_")
        if line.startswith("http_access") and "jb_r-ct_d" in line:
            assert "jb_r-ct_src" in line and "jb_r_src" not in line


def test_hostname_covered_by_wildcard_in_same_acl_is_dropped():
    out = render_fragment(
        "r",
        [_scope(key="r", entries=("*.vendor.com:443", "api.vendor.com:443", "vendor.com:443"))],
    )
    dst_line = next(ln for ln in out.splitlines() if "dstdomain" in ln)
    assert dst_line.split("-n ", 1)[1] == ".vendor.com"


def test_hostname_in_other_group_is_kept():
    out = render_fragment("r", [_scope(key="r", entries=("*.vendor.com", "vendor.com"))])
    assert "dstdomain -n .vendor.com" in out
    assert "dstdomain -n vendor.com" in out


def test_lookalike_suffix_is_not_covered():
    out = render_fragment(
        "r", [_scope(key="r", entries=("*.vendor.com:443", "evilvendor.com:443"))]
    )
    assert "-n .vendor.com evilvendor.com" in out


def test_header_and_deterministic():
    scopes = [_scope(entries=("*.b.com", "a.com", "*.a.org"))]
    out = render_fragment("myrepo", scopes)
    assert out.startswith("# jailbee egress proxy rules for myrepo (generated, do not edit)")
    assert out == render_fragment("myrepo", scopes)


def test_base_conf():
    assert "http_port 3128\n" in BASE_SQUID_CONF
    assert "include /etc/squid/jailbee.d/*.conf\n" in BASE_SQUID_CONF
    assert BASE_SQUID_CONF.index("include") < BASE_SQUID_CONF.index("http_access deny all")


def test_proxy_env():
    env = proxy_env("10.1.0.2", ["*.a.com", "10.0.0.0/8", "1.2.3.4:5", "10.0.0.0/8", "x.com"])
    assert env["HTTPS_PROXY"] == env["https_proxy"] == "http://10.1.0.2:3128"
    assert env["HTTP_PROXY"] == env["http_proxy"] == "http://10.1.0.2:3128"
    assert env["NO_PROXY"] == env["no_proxy"] == "localhost,127.0.0.1,.incus,10.0.0.0/8,1.2.3.4"
