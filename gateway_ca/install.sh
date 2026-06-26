#!/usr/bin/env bash
# Agent Engine BUILD-TIME install script (runs as root in the container build).
# Installs the Agent Gateway's private root CA into BOTH trust stacks so the
# agent can verify the Secure-Web-Proxy-re-signed certs it sees on egress.
set -euo pipefail
HERE="$(dirname "$0")"
# 1) OpenSSL / requests / httpx (and aiohttp via the OS default path):
cp "$HERE/gateway-root.crt" /usr/local/share/ca-certificates/gateway-root.crt
update-ca-certificates
# 2) gRPC C-core ignores the OS store -> build a combined PEM it can be pointed at:
cat /etc/ssl/certs/ca-certificates.crt "$HERE/gateway-root.crt" > /etc/ssl/certs/combined-ca.pem
echo "gateway root CA installed; combined bundle at /etc/ssl/certs/combined-ca.pem"
