FROM ubuntu:26.04

ENV container=container
ENV DEBIAN_FRONTEND=noninteractive

COPY packages.txt /tmp/packages.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends $(grep -vE '^[[:space:]]*(#|$)' /tmp/packages.txt) \
    && apt-get clean && rm -rf /var/lib/apt/lists/* /tmp/packages.txt

# Apple's machine-image requirements (docs/container-machine.md): an init that
# boots without a console, empty machine ids, and no sshd AcceptEnv noise.
RUN truncate -s 0 /etc/machine-id \
    && rm -f /var/lib/dbus/machine-id \
    && systemctl set-default multi-user.target \
    && systemctl mask dev-hugepages.mount sys-fs-fuse-connections.mount \
       systemd-update-utmp.service systemd-tmpfiles-setup.service console-getty.service \
    && sed -i -e 's/^AcceptEnv LANG LC_\*$/#AcceptEnv LANG LC_*/' /etc/ssh/sshd_config
