#!/bin/bash
# 90-registry-mirror-ca — import the jailbee-registry-mirror CA into the
# system Java truststore, if the CA file was bind-mounted in by the
# golden build. No-op when /opt/jailbee-mirror-ca.crt is absent.
# Env: (none)
# Requires: 20-corretto.sh (needs `keytool` on PATH)
set -euo pipefail

if [ -r /opt/jailbee-mirror-ca.crt ]; then
  echo "==> Importing jailbee-registry-mirror CA into system Java truststore"
  keytool -delete -noprompt -alias jailbee-registry-mirror \
    -cacerts -storepass changeit 2>/dev/null || true
  keytool -importcert -noprompt -alias jailbee-registry-mirror \
    -file /opt/jailbee-mirror-ca.crt \
    -cacerts -storepass changeit
fi
