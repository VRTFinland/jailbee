#!/usr/bin/env bash
# Installed inside jailbee-registry-mirror Incus container at first 'jailbee registry up'.
# Installs podman + Quadlet, drops the Quadlet unit, enables the service.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update
# `nftables` provides /usr/sbin/nft, which podman's default networking
# backend (netavark) shells out to. Without it the proxy container exits
# 127 with "netavark: nftables error: unable to execute nft".
apt-get install -y --no-install-recommends podman uidmap nftables

# Quadlet ships with podman ≥ 4.4 on Ubuntu 24.04+. Verify the path so
# install fails fast if the apt resolution gave us an unexpected layout.
test -f /usr/lib/systemd/system-generators/podman-system-generator || \
  test -f /usr/libexec/podman/quadlet || \
  { echo "Quadlet generator missing — podman too old?"; exit 1; }

# Drop the Quadlet unit. The systemd generator turns this into
# jailbee-registry-proxy.service on the next daemon-reload.
# Source path matches registry.py:_provision_mirror — `/root/` instead of
# `/tmp/` because something (dpkg postinst? cloud-init?) wipes /tmp during
# `apt-get install` in this image, mid-script, even though no tmpfiles.d
# rule mandates it. /root persists.
install -D -m 0644 /root/jailbee-registry-proxy.container \
  /etc/containers/systemd/jailbee-registry-proxy.container

# Quadlet's EnvironmentFile= becomes `podman run --env-file=`; the file
# must exist or podman exits 125 before pulling. Create an empty one now
# so the first start succeeds; apply_mirror_registries() rewrites the
# contents (REGISTRIES=...) on each `jailbee new` / `jailbee apply`.
#
# Guarded by `test -f` because this script is also re-run to repair a
# half-provisioned mirror — truncating the file would silently drop every
# per-repo upstream from the host-global mirror.
test -f /etc/jailbee-registry-proxy.env || \
  install -D -m 0644 /dev/null /etc/jailbee-registry-proxy.env

systemctl daemon-reload
systemctl start jailbee-registry-proxy.service

echo "==> jailbee-registry-proxy installed and started"
