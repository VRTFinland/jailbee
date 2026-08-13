#!/bin/bash
# 60-gui-libs — install JetBrains/Chrome runtime libraries and fonts.
# Env: (none)
# Installs: ~30 apt packages needed for JBR + Chrome to render via the
#           host's Wayland/X11 socket bind-mount.
set -euo pipefail

echo "==> Installing GUI client runtime libraries"
# Required for JetBrains IDEs (JBR) and Chrome to render via the host's
# Wayland/X11 socket bind-mount. Without these the base image only
# contains the libc shipped with Ubuntu minimal and JBR's libawt_xawt.so
# fails to load at startup with "libXi.so.6: cannot open shared object".
#
# libegl1 + libegl-mesa0 + libgles2 + libgl1-mesa-dri provide the
# system EGL loader and Mesa drivers needed for hardware-accelerated
# rendering. Without them Chrome falls back to software rendering with
# "libEGL.so.1: cannot open shared object" errors.
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    libxi6 libxtst6 libxrender1 libxrandr2 libxext6 libxxf86vm1 \
    libxcomposite1 libxdamage1 libxcursor1 libxss1 libxinerama1 \
    libxkbcommon0 libxkbfile1 libxv1 \
    libwayland-client0 libwayland-cursor0 libwayland-egl1 \
    libegl1 libegl-mesa0 libgles2 libgl1-mesa-dri \
    libgtk-3-0 libgbm1 libnss3 libasound2t64 libcups2 libpango-1.0-0 \
    fontconfig libfreetype6 fonts-dejavu fonts-noto fonts-noto-cjk
