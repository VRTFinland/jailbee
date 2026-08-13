"""Tests for docker_daemon module (rpardini proxy era)."""

from unittest.mock import MagicMock

import pytest

from jailbee.docker_daemon import (
    apply_docker_proxy,
    compute_mirror_endpoint,
    render_proxy_conf,
)
from jailbee.global_config import DockerRegistryMirror, GlobalConfig


def test_compute_mirror_endpoint_returns_mirror_container_ip(tmp_path):
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": "jailbee-registry-mirror",
            "status": "Running",
            "state": {
                "network": {
                    "eth0": {
                        "addresses": [
                            {
                                "family": "inet",
                                "address": "10.234.216.42",
                                "netmask": "24",
                                "scope": "global",
                            },
                            {"family": "inet6", "address": "fe80::1", "scope": "link"},
                        ]
                    }
                }
            },
        }
    ]
    gcfg = GlobalConfig(
        docker_registry_mirror=DockerRegistryMirror(port=3128, data_dir=tmp_path),
    )

    ip, port = compute_mirror_endpoint(incus, gcfg)

    assert (ip, port) == ("10.234.216.42", 3128)


def test_compute_mirror_endpoint_uses_configured_port(tmp_path):
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": "jailbee-registry-mirror",
            "status": "Running",
            "state": {
                "network": {
                    "eth0": {
                        "addresses": [
                            {
                                "family": "inet",
                                "address": "10.234.216.42",
                                "scope": "global",
                            },
                        ]
                    }
                }
            },
        }
    ]
    gcfg = GlobalConfig(
        docker_registry_mirror=DockerRegistryMirror(port=4000, data_dir=tmp_path),
    )

    _, port = compute_mirror_endpoint(incus, gcfg)
    assert port == 4000


def test_compute_mirror_endpoint_raises_when_mirror_missing(tmp_path):
    incus = MagicMock()
    incus.list_containers.return_value = []
    gcfg = GlobalConfig(
        docker_registry_mirror=DockerRegistryMirror(data_dir=tmp_path),
    )

    with pytest.raises(ValueError, match=r"Run 'jailbee registry up'"):
        compute_mirror_endpoint(incus, gcfg)


def test_compute_mirror_endpoint_raises_when_mirror_stopped(tmp_path):
    incus = MagicMock()
    incus.list_containers.return_value = [
        {"name": "jailbee-registry-mirror", "status": "Stopped", "state": None}
    ]
    gcfg = GlobalConfig(
        docker_registry_mirror=DockerRegistryMirror(data_dir=tmp_path),
    )

    with pytest.raises(ValueError, match=r"Run 'jailbee registry up'"):
        compute_mirror_endpoint(incus, gcfg)


def test_compute_mirror_endpoint_raises_when_no_ipv4_assigned(tmp_path):
    """Just-started container with no IPv4 lease yet."""
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": "jailbee-registry-mirror",
            "status": "Running",
            "state": {"network": {"eth0": {"addresses": []}}},
        }
    ]
    gcfg = GlobalConfig(
        docker_registry_mirror=DockerRegistryMirror(data_dir=tmp_path),
    )

    with pytest.raises(ValueError, match=r"no IPv4 address"):
        compute_mirror_endpoint(incus, gcfg)


def test_render_proxy_conf_includes_systemd_section_header():
    out = render_proxy_conf("jailbee-registry-mirror.incus", 3128)
    assert out.startswith("[Service]\n")


def test_render_proxy_conf_sets_both_http_and_https_proxy():
    out = render_proxy_conf("jailbee-registry-mirror.incus", 3128)
    assert 'Environment="HTTPS_PROXY=http://jailbee-registry-mirror.incus:3128"' in out
    assert 'Environment="HTTP_PROXY=http://jailbee-registry-mirror.incus:3128"' in out


def test_render_proxy_conf_sets_no_proxy_for_local_traffic():
    """Local addresses + .incus zone must NOT route through the proxy."""
    out = render_proxy_conf("jailbee-registry-mirror.incus", 3128)
    assert 'Environment="NO_PROXY=localhost,127.0.0.1,incusbr0,*.incus"' in out


def test_render_proxy_conf_ends_with_newline():
    """systemd parses unit files line-by-line; trailing newline keeps diffs clean."""
    out = render_proxy_conf("jailbee-registry-mirror.incus", 3128)
    assert out.endswith("\n")


def test_apply_docker_proxy_runs_single_bash_invocation():
    incus = MagicMock()
    apply_docker_proxy(
        incus,
        "myrepo-feat-x",
        ca_cert_pem="-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n",
        port=3128,
    )

    assert incus.exec.call_count == 1
    name, argv = incus.exec.call_args.args
    assert name == "myrepo-feat-x"
    assert argv[:2] == ["bash", "-c"]


def test_apply_docker_proxy_script_writes_ca_cert_via_heredoc():
    incus = MagicMock()
    apply_docker_proxy(
        incus,
        "myrepo-feat-x",
        ca_cert_pem=("-----BEGIN CERTIFICATE-----\nfake-cert\n-----END CERTIFICATE-----\n"),
        port=3128,
    )

    script = incus.exec.call_args.args[1][2]
    assert "/usr/local/share/ca-certificates/jailbee-registry-mirror.crt" in script
    assert "fake-cert" in script
    assert "update-ca-certificates" in script


def test_apply_docker_proxy_removes_the_pre_1_0_trust_anchors():
    """`jailbee migrate` deletes the gie-registry-mirror it vouched for.

    Left behind, the old CA file and keytool alias are dangling anchors for
    a mirror that no longer exists — and this script is the only thing that
    rewrites container-side trust after the rename.
    """
    incus = MagicMock()
    apply_docker_proxy(incus, "myrepo-feat-x", ca_cert_pem="cert", port=3128)

    script = incus.exec.call_args.args[1][2]
    old_cert_rm = "rm -f /usr/local/share/ca-certificates/gie-registry-mirror.crt"
    assert old_cert_rm in script
    assert "-alias gie-registry-mirror" in script
    # The removals must precede the trust-store refresh / re-import they undo.
    assert script.index(old_cert_rm) < script.index("update-ca-certificates")
    assert script.index("-alias gie-registry-mirror") < script.index("-importcert")


def test_apply_docker_proxy_writes_systemd_dropin_atomically():
    incus = MagicMock()
    apply_docker_proxy(
        incus,
        "myrepo-feat-x",
        ca_cert_pem="cert",
        port=3128,
    )

    script = incus.exec.call_args.args[1][2]
    assert "/etc/systemd/system/docker.service.d/http-proxy.conf" in script
    assert "mktemp" in script  # atomic write pattern
    assert "mv " in script


def test_apply_docker_proxy_restarts_dockerd():
    incus = MagicMock()
    apply_docker_proxy(
        incus,
        "myrepo-feat-x",
        ca_cert_pem="cert",
        port=3128,
    )

    script = incus.exec.call_args.args[1][2]
    assert "systemctl daemon-reload" in script
    assert "systemctl restart docker" in script


def test_apply_docker_proxy_restart_is_guarded_for_non_docker_containers():
    """A container without Docker installed has no `docker.service`, so an
    unconditional `systemctl restart docker` fails (exit 5) and aborts the
    whole `incus exec` under `set -e`. The restart must be guarded so the CA
    + proxy drop-in still get installed harmlessly and the exec succeeds."""
    incus = MagicMock()
    apply_docker_proxy(
        incus,
        "myrepo-feat-x",
        ca_cert_pem="cert",
        port=3128,
    )

    script = incus.exec.call_args.args[1][2]
    # The restart must sit behind a `command -v docker` guard, mirroring the
    # keytool best-effort guard above it.
    assert "command -v docker" in script
    guard_pos = script.index("command -v docker")
    restart_pos = script.index("systemctl restart docker")
    assert guard_pos < restart_pos, "restart must be inside the docker guard"


def test_apply_docker_proxy_keytool_is_best_effort():
    """keytool failure (e.g. no JDK installed in the container) must NOT
    abort the proxy setup — dockerd's OS-level CA bundle is the primary
    consumer."""
    incus = MagicMock()
    apply_docker_proxy(
        incus,
        "myrepo-feat-x",
        ca_cert_pem="cert",
        port=3128,
    )

    script = incus.exec.call_args.args[1][2]
    # keytool line must be guarded so its failure doesn't kill `set -e`
    assert "keytool" in script
    assert "|| true" in script or "if command -v keytool" in script


def test_apply_docker_proxy_uses_dns_name_not_ip():
    """http-proxy.conf must point at the DNS name so it stays valid across
    mirror restarts (the mirror's DHCP-assigned IP can change)."""
    incus = MagicMock()
    apply_docker_proxy(
        incus,
        "myrepo-feat-x",
        ca_cert_pem="cert",
        port=3128,
    )

    script = incus.exec.call_args.args[1][2]
    assert "jailbee-registry-mirror.incus:3128" in script
