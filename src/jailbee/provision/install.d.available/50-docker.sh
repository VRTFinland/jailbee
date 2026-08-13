#!/bin/bash
# 50-docker — install Docker Engine and disable its AppArmor mediation.
# Adds the dev user to the docker group.
# Env: CONTAINER_USER
# Installs: docker-ce, docker-buildx-plugin, docker-compose-plugin,
#           /etc/systemd/system/docker.service.d/10-container-env.conf
set -euo pipefail

echo "==> Installing Docker Engine"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
    gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Tell dockerd to skip its AppArmor profile loader. Without this, the very
# first `docker run` fails with:
#
#   Could not check if docker-default AppArmor profile was loaded:
#   open /sys/kernel/security/apparmor/profiles: permission denied
#
# dockerd/runc probes /sys/kernel/security/apparmor/profiles to detect
# AppArmor support. That file is owned by host root; inside jailbee's
# unprivileged user namespace the container's mapped uid can't read it,
# so the open() fails with EPERM and Docker treats that as fatal. This is
# an unprivileged-userns ownership issue, independent of any AppArmor
# profile setting — there's no profile-level knob that fixes it.
#
# runc/dockerd has a documented escape hatch: when the `container` env var
# is set, AppArmor support is reported as disabled and the daemon-default
# profile is skipped. systemd's drop-in is the cleanest way to inject it.
echo "==> Configuring dockerd to skip AppArmor (container=lxc env)"
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/10-container-env.conf <<'EOF'
[Service]
Environment="container=lxc"
EOF

# Add the dev user to the docker group so `docker` works without sudo.
usermod -aG docker "${CONTAINER_USER}"

docker --version
