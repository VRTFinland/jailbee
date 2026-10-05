#!/usr/bin/env bash
# Run inside the jailbee-egress-proxy Incus container by `jailbee apply` /
# egress_proxy.proxy_up. Installs Squid and prepares the fragment directory.
# squid.conf itself is written by jailbee afterwards (BASE_SQUID_CONF).
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends squid

# squid.conf includes /etc/squid/jailbee.d/*.conf; the glob must match
# something or `squid -k parse` fails on a proxy that has no fragments yet.
install -d -m 0755 /etc/squid/jailbee.d
cat > /etc/squid/jailbee.d/00-empty.conf <<'JAILBEE_EMPTY_EOF'
# placeholder so the include glob always matches
JAILBEE_EMPTY_EOF

# Hardening: the proxy never routes. Client NICs carry no route out and
# upstream traffic leaves only through eth0, so forwarding stays off even if
# the host or image would turn it on. Rewriting the drop-in is idempotent.
cat > /etc/sysctl.d/60-jailbee-egress-proxy.conf <<'JAILBEE_SYSCTL_EOF'
net.ipv4.ip_forward=0
JAILBEE_SYSCTL_EOF
sysctl -q -w net.ipv4.ip_forward=0 || echo "warning: could not set net.ipv4.ip_forward=0 now" >&2
