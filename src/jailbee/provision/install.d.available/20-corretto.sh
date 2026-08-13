#!/bin/bash
# 20-corretto — install Amazon Corretto JDK matching $JAVA_PACKAGE.
# Env: JAVA_PACKAGE (apt package name, e.g. java-17-amazon-corretto-jdk)
# Installs: Amazon Corretto JDK
set -euo pipefail

echo "==> Installing Amazon Corretto Java"
wget -qO- https://apt.corretto.aws/corretto.key | \
    gpg --dearmor -o /usr/share/keyrings/corretto-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/corretto-keyring.gpg] https://apt.corretto.aws stable main" \
    > /etc/apt/sources.list.d/corretto.list
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y "${JAVA_PACKAGE}"

java -version
javac -version
