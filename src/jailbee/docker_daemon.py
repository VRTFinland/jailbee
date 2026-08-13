"""Configure user containers to use the jailbee registry mirror via HTTPS_PROXY.

The mirror runs in an Incus container `jailbee-registry-mirror` (see registry.py)
and exposes rpardini/docker-registry-proxy on port 3128. User containers get:

* `/usr/local/share/ca-certificates/jailbee-registry-mirror.crt` — rpardini's
  generated CA cert (also bind-mounted out to the host as ca.crt).
* `/etc/systemd/system/docker.service.d/http-proxy.conf` — sets dockerd's
  `HTTPS_PROXY` / `HTTP_PROXY` env to the mirror.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus

# DNS name served by `jailbee-loose`'s dnsmasq — that's the bridge the mirror
# lives on. Strict containers can't resolve it via incusbr0's dnsmasq,
# so they get an /etc/hosts pin instead (see hosts.py).
# Stable across mirror restarts, so http-proxy.conf doesn't need
# rewriting on every `jailbee apply`. Incus 6.x defaults the local DNS
# suffix to `.incus` (was `.lxd` under LXD pre-fork).
MIRROR_DNS_NAME = "jailbee-registry-mirror.incus"
_MIRROR_CONTAINER_NAME = "jailbee-registry-mirror"


def compute_mirror_endpoint(incus: Incus, gcfg: GlobalConfig) -> tuple[str, int]:
    """Resolve the mirror container's IPv4 + port for the per-repo ACL rule.

    Returns ``(ip, port)``. Raises ``ValueError`` (caught and printed by CLI
    callers) when the mirror is not in a usable state.
    """
    for c in incus.list_containers():
        if c.get("name") != _MIRROR_CONTAINER_NAME:
            continue
        if c.get("status") != "Running":
            raise ValueError(
                f"{_MIRROR_CONTAINER_NAME} is not running. Run 'jailbee registry up' first."
            )
        state = c.get("state") or {}
        eth0 = state.get("network", {}).get("eth0", {})
        for addr in eth0.get("addresses") or []:
            if addr.get("family") == "inet" and addr.get("scope") == "global":
                return addr["address"], gcfg.docker_registry_mirror.port
        raise ValueError(
            f"{_MIRROR_CONTAINER_NAME} has no IPv4 address yet. "
            f"Wait a few seconds and retry, or run 'jailbee registry up' again."
        )
    raise ValueError(
        f"{_MIRROR_CONTAINER_NAME} container not found. Run 'jailbee registry up' first."
    )


def render_proxy_conf(host: str, port: int) -> str:
    """Return /etc/systemd/system/docker.service.d/http-proxy.conf content."""
    proxy_url = f"http://{host}:{port}"
    return (
        "[Service]\n"
        f'Environment="HTTPS_PROXY={proxy_url}"\n'
        f'Environment="HTTP_PROXY={proxy_url}"\n'
        'Environment="NO_PROXY=localhost,127.0.0.1,incusbr0,*.incus"\n'
    )


def apply_docker_proxy(
    incus: Incus,
    name: str,
    ca_cert_pem: str,
    port: int,
) -> None:
    """Install the mirror CA + dockerd HTTPS_PROXY in container `name`.

    Idempotent: re-running with the same inputs is a no-op other than the
    dockerd restart. Atomic writes (mktemp + mv) so a half-written conf
    file never gets read by systemd on reload.
    """
    proxy_conf = render_proxy_conf(MIRROR_DNS_NAME, port)
    script = f"""\
set -euo pipefail

# 1. CA cert into OS trust bundle.
mkdir -p /usr/local/share/ca-certificates
ca_tmp=$(mktemp)
cat > "$ca_tmp" <<'JAILBEE_CA_EOF'
{ca_cert_pem.rstrip()}
JAILBEE_CA_EOF
mv "$ca_tmp" /usr/local/share/ca-certificates/jailbee-registry-mirror.crt
# Drop the pre-1.0 copy: `jailbee migrate` deletes the gie-registry-mirror
# container, so leaving its cert in the trust bundle keeps a dangling anchor
# for a mirror that no longer exists.
rm -f /usr/local/share/ca-certificates/gie-registry-mirror.crt
update-ca-certificates >/dev/null

# 2. Java keystore — best effort. Container may not have a JDK installed
# (or keytool may live elsewhere); silently skip in that case so dockerd
# still gets its OS-level CA.
if command -v keytool >/dev/null 2>&1; then
  keytool -delete -noprompt -alias gie-registry-mirror \\
    -cacerts -storepass changeit 2>/dev/null || true
  keytool -delete -noprompt -alias jailbee-registry-mirror \\
    -cacerts -storepass changeit 2>/dev/null || true
  keytool -importcert -noprompt -alias jailbee-registry-mirror \\
    -file /usr/local/share/ca-certificates/jailbee-registry-mirror.crt \\
    -cacerts -storepass changeit || true
fi

# 3. dockerd HTTPS_PROXY systemd drop-in (atomic).
mkdir -p /etc/systemd/system/docker.service.d
proxy_tmp=$(mktemp)
cat > "$proxy_tmp" <<'JAILBEE_PROXY_EOF'
{proxy_conf.rstrip()}
JAILBEE_PROXY_EOF
mv "$proxy_tmp" /etc/systemd/system/docker.service.d/http-proxy.conf

# 4. Remove stale registry-mirrors daemon.json (left by pre-rpardini installs).
rm -f /etc/docker/daemon.json

# 5. Restart dockerd — best effort. A container without Docker installed has
# no `docker.service`, so an unconditional restart would fail (exit 5) and
# abort the whole exec under `set -e`. The CA + proxy drop-in above stay
# installed harmlessly; only the restart is Docker-specific. Mirrors the
# keytool guard above.
if command -v docker >/dev/null 2>&1; then
  systemctl daemon-reload
  systemctl restart docker
fi
"""
    incus.exec(name, ["bash", "-c", script], timeout=120)
