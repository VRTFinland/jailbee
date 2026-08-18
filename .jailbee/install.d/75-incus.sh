#!/bin/bash
# 75-incus — a nested Incus daemon, for testing jailbee's own Incus
# interactions from inside a dogfood container.
# Env: CONTAINER_USER, JAILBEE_USER_HOME
#
# Why this is worth baking in: jailbee's unit suite is fully mocked, so
# nothing in it proves that the device properties and command lines we
# generate are ones a real daemon accepts. Until now that gap could only be
# closed on the host, which made every "does Incus really behave this way?"
# question a context switch. A nested daemon closes it in the container: a
# throwaway instance is enough to verify device schemas, error strings and
# hotplug behaviour for real.
#
# It works because jailbee's own base profile sets `security.nesting: true`
# (and `security.syscalls.intercept.mknod: true`) on every container — see
# profiles.base_profile_yaml. No extra host-side config is needed.
#
# What this image ends up with: the daemon installed but its units disabled,
# a `default` dir storage pool already created, and an idmap range a nested
# instance can actually use. `systemctl start incus.service` then
# `incus launch images:alpine/edge probe1` works with no further setup.
# Networks are deliberately NOT created here — `jailbee init` creates the
# ones jailbee needs, and a probe that only exercises devices does not need
# any NIC at all (an instance still has its own loopback).
#
# Verified end to end inside a container on 2026-08-18 (Incus 6.0.5): daemon
# start, dir pool, instance start, and proxy devices in both bind directions.
# See docs/manual-testing.md for the recipe and the findings.
set -euo pipefail

# `incus-base` rather than `incus`: the metapackage depends on
# qemu-system-x86, swtpm and virtiofsd for VM support, which a nested probe
# will never use and which cost hundreds of MB in the image. `incus-base` is
# the same daemon built container-only, and pulls `incus-client` for the CLI.
#
# dnsmasq-base is one of incus-base's Recommends, dropped by
# --no-install-recommends and then added back explicitly: without it
# `incus network create` cannot serve DHCP or DNS on a nested bridge. Nothing
# here creates a network, but a probe that needs one should not have to
# apt-get inside a strict-mode container to get it. The bridge itself was
# verified working (L2 to the gateway, dnsmasq leases served, nft masquerade
# rules installed); egress out of a nested bridge was NOT verified, because
# the container it was tried in had no egress of its own at the time.
echo "==> Installing incus-base + incus-client (no VM support)"
apt-get install -y --no-install-recommends incus-base incus-client dnsmasq-base

# The daemon's socket is root:incus-admin 0660, so without this the dev user
# gets "You don't have the needed permissions to talk to the incus daemon" —
# and `jailbee port ...` against the nested daemon, which is the whole point of
# having it, would need sudo. The group is created by the package postinst, so
# this has to come after the install above. Group membership is per-session: a
# shell opened before this ran does not have it.
echo "==> Adding ${CONTAINER_USER} to incus-admin"
usermod -aG incus-admin "${CONTAINER_USER}"
id -nG "${CONTAINER_USER}" | grep -qw incus-admin

# The incus postinst writes `root:1000000:1000000000` into /etc/sub[ug]id.
# That range is larger than the *outer* container's own idmap can satisfy, and
# every nested instance then dies in forkstart with
# `newuidmap failed to write mapping ... Operation not permitted`. Capping the
# range here fixes it for good: instances start with no per-instance
# `security.idmap.size` override, which is what lets a nested `jailbee new`
# have a chance of working unmodified. 65536 is one full uid namespace per
# instance-set, which is all a probe needs.
echo "==> Capping root's subuid/subgid range for nested use"
sed -i 's/^root:1000000:1000000000$/root:1000000:65536/' /etc/subuid /etc/subgid
grep -q '^root:1000000:65536$' /etc/subuid
grep -q '^root:1000000:65536$' /etc/subgid

# Create the pool at build time so a fresh container is one command away from
# a working daemon. This needs the daemon running, hence systemd in the build
# container — which is how jailbee builds images (golden.py execs into a
# launched container). A failure here is fatal on purpose: an image carrying a
# half-initialised daemon would fail later, further from the cause.
#
# The pool is named `default` to match `defaults.storage_pool`, and `dir` is
# the only driver that needs nothing from the host.
echo "==> Initialising the daemon with a 'default' dir pool"
systemctl start incus.service
incus admin init --preseed <<'PRESEED'
config: {}
networks: []
storage_pools:
  - name: default
    driver: dir
profiles:
  - name: default
    devices:
      root:
        path: /
        pool: default
        type: disk
PRESEED
incus storage list | grep -q default

# Leave the daemon stopped and its units disabled: most containers never want
# a second Incus daemon running, and lxcfs plus the daemon's own state are not
# free. Starting it is one command. --now also makes sure the image is not
# published with a live daemon and its mounts in place.
echo "==> Disabling the incus units (opt-in per container)"
systemctl disable --now incus.socket incus.service || true

echo "==> incus client version: $(incus --version)"
