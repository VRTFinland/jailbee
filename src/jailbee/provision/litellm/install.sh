#!/bin/bash
# Provisions the jailbee-litellm container. Idempotent; re-run by
# `jailbee litellm up --reinstall`. Package hosts only; no auth mount yet.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-venv ca-certificates
python3 -m venv /opt/litellm
# Use the pip bundled by venv; upgrading it from an unlocked index would
# bypass the hashed lock even when the LiteLLM package is pinned.
if [ "${JAILBEE_LITELLM_UNLOCKED_VERSION:-}" != "" ]; then
    /opt/litellm/bin/pip install "litellm[proxy]==${JAILBEE_LITELLM_UNLOCKED_VERSION}"
else
    /opt/litellm/bin/pip install --require-hashes --no-deps -r /root/litellm-requirements.lock
fi
# Temporary: backport of litellm#41235 for 1.104.0 (a no-op on any other version).
/opt/litellm/bin/python /root/litellm-chatgpt-stream-fix.py
install -m 0644 /root/jailbee-litellm@.service /etc/systemd/system/jailbee-litellm@.service
systemctl daemon-reload
