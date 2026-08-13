#!/bin/bash
# 45-uv — install Astral `uv` as the dev user.
# Env: CONTAINER_USER, JAILBEE_USER_HOME
# Installs: ${JAILBEE_USER_HOME}/.local/bin/uv (+ ${JAILBEE_USER_HOME}/.local/bin/uvx)
#
# Repo-local snippet (dogfood): this repo is itself a uv-managed project,
# so baking uv into the golden image removes the per-container install-uv
# autostart step. sync-deps (uv sync) stays in autostart — it needs the
# cloned source tree.
set -euo pipefail

echo "==> Installing uv (Astral) as ${CONTAINER_USER}"
runuser -l "${CONTAINER_USER}" -c \
    "curl -LsSf https://astral.sh/uv/install.sh | sh"

test -x "${JAILBEE_USER_HOME}/.local/bin/uv"
