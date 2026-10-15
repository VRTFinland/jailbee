"""The packaged nginx.conf the registry mirror runs instead of rpardini's own.

Text-level checks: nginx itself is not available in the unit suite, so these
pin that each lockdown change is present. The rig recipe in
docs/manual-testing.md proves they work.
"""

import re

from jailbee.registry import _read_provision_text


def _conf() -> str:
    return _read_provision_text("nginx.conf")


def _server_block(conf: str, server_name: str) -> str:
    start = conf.index(f"server_name {server_name};")
    nxt = conf.find("server_name ", start + 1)
    return conf[start : nxt if nxt != -1 else len(conf)]


def test_header_names_the_upstream_tag_the_quadlet_runs():
    """An image bump without re-deriving the config must fail here."""
    header = _conf().splitlines()[0]
    m = re.fullmatch(r"# derived from (rpardini/docker-registry-proxy:\S+)", header)
    assert m, f"unexpected header: {header!r}"
    quadlet = _read_provision_text("jailbee-registry-proxy.container")
    assert f"Image=docker.io/{m.group(1)}\n" in quadlet


def test_director_never_tunnels_to_the_requested_address():
    """rpardini's `default "$connect_addr"` is the blind tunnel."""
    conf = _conf()
    intercept_map = conf[conf.index("map $connect_host $interceptedHost") :]
    intercept_map = intercept_map[: intercept_map.index("}")]
    assert "$connect_addr" not in intercept_map
    assert "include /etc/nginx/docker.intercept.map;" in intercept_map


def test_director_allows_no_ports_beyond_what_it_intercepts():
    """`proxy_connect_allow all` stays harmless only while every target is
    127.0.0.1:443 or refused; pin that the address comes from the map."""
    director = _server_block(_conf(), "proxy_director_")
    assert "proxy_connect_address $interceptedHost;" in director


def test_caching_layer_checks_host_against_the_intercept_map():
    conf = _conf()
    allowed_map = conf[conf.index("map $host $jailbee_allowed") :]
    allowed_map = allowed_map[: allowed_map.index("}")]
    assert "include /etc/nginx/docker.intercept.map;" in allowed_map
    assert 'default "";' in allowed_map
    caching = _server_block(conf, "proxy_caching_")
    assert re.search(r'if \(\$jailbee_allowed = ""\)\s*\{\s*return 403 "jailbee-refused', caching)


def test_caching_layer_allows_only_get_and_head():
    caching = _server_block(_conf(), "proxy_caching_")
    assert re.search(
        r'if \(\$request_method !~ \^\(GET\|HEAD\)\$\)\s*\{\s*return 405 "jailbee-refused',
        caching,
    )


def test_every_change_is_marked():
    """Re-deriving from a new upstream starts by grepping these."""
    assert _conf().count("# jailbee:") >= 3
