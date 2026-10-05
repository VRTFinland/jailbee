#!/usr/bin/env bash
# Provision the jailbee-display container: weston, waypipe (the per-session
# servers of `waypipe ssh`), a self-signed TLS pair and the systemd unit.
# Idempotent. Expects JAILBEE_UID, JAILBEE_GID and JAILBEE_USER.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
: "${JAILBEE_UID:?}" "${JAILBEE_GID:?}" "${JAILBEE_USER:?}"

# The container was started moments ago: DHCP and the bridge's dnsmasq may not
# have answered yet. Wait for name resolution instead of failing the first
# apt-get on a boot race.
network_up=""
for _ in $(seq 1 60); do
  if getent hosts archive.ubuntu.com >/dev/null 2>&1; then
    network_up=1
    break
  fi
  sleep 1
done
if [ -z "$network_up" ]; then
  echo "jailbee-display has no working DNS after 60s on the jailbee-loose bridge." >&2
  echo "If DHCP/DNS from the bridge is dropped by a host firewall, run 'jailbee doctor'." >&2
  exit 1
fi

apt-get update -qq
apt-get install -y -qq weston openssl waypipe

if ! getent passwd "$JAILBEE_UID" >/dev/null; then
  getent group "$JAILBEE_GID" >/dev/null || groupadd -g "$JAILBEE_GID" "$JAILBEE_USER"
  useradd -m -u "$JAILBEE_UID" -g "$JAILBEE_GID" -s /bin/bash "$JAILBEE_USER"
fi
RUN_USER="$(getent passwd "$JAILBEE_UID" | cut -d: -f1)"

install -d -m 0700 -o "$RUN_USER" /etc/jailbee-display
if [ ! -f /etc/jailbee-display/tls.key ] || [ ! -f /etc/jailbee-display/tls.crt ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj /CN=jailbee-display \
    -keyout /etc/jailbee-display/tls.key -out /etc/jailbee-display/tls.crt 2>/dev/null
  chown "$RUN_USER" /etc/jailbee-display/tls.key /etc/jailbee-display/tls.crt
  chmod 0600 /etc/jailbee-display/tls.key
fi

# weston 14 turns NLA off but leaves FreeRDP 3's extended NLA (HYBRID_EX) on,
# and gives FreeRDP no SAM file, so a client that asks for NLA (Windows App
# always does) is picked HYBRID_EX and then refused with "Could not find user
# in SAM database" whatever the login. FreeRDP's server reads ExtSecurity from
# WinPR's registry file, /etc/<vendor>/<product>/HKLM.reg; turning it off
# leaves TLS, which every client also offers. No login is checked: the port is
# loopback-only in this container, reached through the SSH tunnel.
install -d -m 0755 /etc/FreeRDP /etc/FreeRDP/FreeRDP
cat > /etc/FreeRDP/FreeRDP/HKLM.reg <<'REG'
[HKEY_LOCAL_MACHINE\Software\FreeRDP\FreeRDP\Server]
"ExtSecurity"=dword:00000000
REG
chmod 0644 /etc/FreeRDP/FreeRDP/HKLM.reg

sed "s/__USER__/$RUN_USER/" /root/jailbee-display.service \
  > /etc/systemd/system/jailbee-display.service
systemctl daemon-reload
systemctl enable --now jailbee-display.service
