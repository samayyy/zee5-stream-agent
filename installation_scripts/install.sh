#!/usr/bin/env bash
# Agent Engine BUILD-TIME install script (runs as root in the container build).
# Installs the Agent Gateway's private root CA into BOTH trust stacks so the
# agent can verify the Secure-Web-Proxy-re-signed certs it sees on egress.
#
# The CA file (gateway-root.crt) is shipped as an extra_package at the app root
# (NOT inside installation_scripts/, which the SDK reserves for scripts only).
set -euo pipefail

# Locate gateway-root.crt across candidate build layouts.
CERT=""
for c in "./gateway-root.crt" "/code/gateway-root.crt" \
         "$(dirname "$0")/../gateway-root.crt" "$(dirname "$0")/gateway-root.crt"; do
  if [ -f "$c" ]; then CERT="$c"; break; fi
done
if [ -z "$CERT" ]; then
  echo "ERROR: gateway-root.crt not found (looked in . /code ../ and script dir)"; exit 1
fi
echo "using CA: $CERT"

# 1) OpenSSL / requests / httpx (and aiohttp via the OS default path):
cp "$CERT" /usr/local/share/ca-certificates/gateway-root.crt
update-ca-certificates
# 2) gRPC C-core ignores the OS store -> build a combined PEM it can be pointed at:
cat /etc/ssl/certs/ca-certificates.crt "$CERT" > /etc/ssl/certs/combined-ca.pem
echo "gateway root CA installed; combined bundle at /etc/ssl/certs/combined-ca.pem"
