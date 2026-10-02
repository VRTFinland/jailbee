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
