#!/bin/bash
# Prepare the machine's Linux host for JailBee's Incus. Runs as root inside the
# machine, idempotent: safe to run again after an upgrade. DEV_USER is the Mac
# login the machine created; PACKAGES (optional) lists packages that must exist.
set -euo pipefail

DEV_USER=${DEV_USER:?DEV_USER is required}
DEV_UID=$(id -u "$DEV_USER")
DEV_GID=$(id -g "$DEV_USER")
changed=0

# Packages added to the image after the machine was created arrive here.
if [ -n "${PACKAGES:-}" ]; then
  missing=""
  for pkg in $PACKAGES; do dpkg -s "$pkg" >/dev/null 2>&1 || missing="$missing $pkg"; done
  if [ -n "$missing" ]; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends $missing
    changed=1
  fi
fi

# write_file PATH: replace PATH with stdin when it differs.
write_file() {
  local path=$1 new
  new=$(cat)
  if [ "$(cat "$path" 2>/dev/null || true)" != "$new" ]; then
    printf '%s\n' "$new" > "$path"
    changed=1
    return 0
  fi
  return 1
}

# UID/GID delegation for Incus and for host-file access (docs/installation.md).
for mapping in "root:1000000:1000000000" "root:$DEV_UID:1"; do
  grep -qxF "$mapping" /etc/subuid || { printf '%s\n' "$mapping" >> /etc/subuid; changed=1; }
done
for mapping in "root:1000000:1000000000" "root:$DEV_GID:1"; do
  grep -qxF "$mapping" /etc/subgid || { printf '%s\n' "$mapping" >> /etc/subgid; changed=1; }
done

# Keyring limits: many nested containers exhaust the defaults.
write_file /etc/sysctl.d/99-jailbee-keys.conf <<'CONF' && sysctl -q -p /etc/sysctl.d/99-jailbee-keys.conf || true
kernel.keys.maxkeys=2000
kernel.keys.maxbytes=2000000
kernel.keys.root_maxkeys=2000
kernel.keys.root_maxbytes=2000000
CONF

usermod --append --groups incus-admin "$DEV_USER"
loginctl enable-linger "$DEV_USER"

# The machine does not mount securityfs; /etc/fstab fails in systemd-remount-fs,
# so an early unit does it. AppArmor needs it before Incus starts.
write_file /etc/systemd/system/jailbee-securityfs.service <<'UNIT' || true
[Unit]
Description=Mount securityfs for JailBee's Incus host
DefaultDependencies=no
Before=sysinit.target apparmor.service incus.service
ConditionPathIsMountPoint=!/sys/kernel/security

[Service]
Type=oneshot
ExecStart=/bin/mount -t securityfs securityfs /sys/kernel/security
RemainAfterExit=yes

[Install]
WantedBy=sysinit.target
UNIT

# Freed space must travel back to the Mac: discard on the root filesystem, and a
# weekly fstrim as a safety net (measured: both shrink the Mac's disk image).
write_file /etc/systemd/system/jailbee-discard.service <<'UNIT' || true
[Unit]
Description=Mount the root filesystem with discard so freed space returns to the Mac
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/bin/mount -o remount,discard /
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now jailbee-securityfs.service jailbee-discard.service fstrim.timer
if [ "$changed" = 1 ]; then
  systemctl restart apparmor
  systemctl restart incus
fi
echo "root-prepare: done (changed=$changed)"
