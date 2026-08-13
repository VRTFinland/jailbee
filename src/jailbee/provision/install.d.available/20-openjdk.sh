#!/bin/bash
# 20-openjdk — install OpenJDK from the Ubuntu archive (no external repo).
# Env: JAVA_PACKAGE (apt package name, e.g. openjdk-21-jdk or default-jdk)
# Installs: OpenJDK JDK
set -euo pipefail

echo "==> Installing OpenJDK (${JAVA_PACKAGE})"
DEBIAN_FRONTEND=noninteractive apt-get install -y "${JAVA_PACKAGE}"

java -version
javac -version
