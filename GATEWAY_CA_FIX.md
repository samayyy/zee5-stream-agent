# Fixing CERTIFICATE_VERIFY_FAILED on a gateway-bound agent

## What's wrong
Agent Gateway egress runs on a **Secure Web Proxy that does TLS interception** — it
re-signs every outbound TLS connection with a **private CA** the Agent Runtime
image doesn't trust. So the agent fails on *every* egress hop (gRPC
`cloudresourcemanager` at startup, aiohttp `aiplatform`, the run.app MCPs) with
`self-signed certificate in certificate chain`.

It is **NOT** an authorization problem — it persists even with the gateway in
**Audit only** (TLS re-signing happens at the handshake, before any IAP decision).
More registry entries / `iap.egressor` grants will not fix it. The container must
**trust the gateway's private root CA**.

## The fix (run in Cloud Shell on the gateway project)

```bash
export PROJECT_ID="gm-test-337806"
export REGION="asia-southeast1"
export GATEWAY_ID="agentgw-publicmcp"
cd zee5-stream-agent

# 1) Get the gateway's private root CA. Try the gateway resource first:
gcloud alpha network-services agent-gateways describe "$GATEWAY_ID" --location="$REGION" \
  --format="value(agentGatewayCard.rootCertificates)" > gateway_ca/gateway-root.crt
head -1 gateway_ca/gateway-root.crt   # must be: -----BEGIN CERTIFICATE-----

# If that is empty, get it from the TLS-inspection policy's CA pool instead:
#   POOL=$(gcloud network-security tls-inspection-policies list --location="$REGION" --format='value(caPool)' | head -1)
#   gcloud privateca roots list --pool="${POOL##*/}" --location="$REGION" --format='value(name)'   # -> ROOT_CA
#   gcloud privateca roots describe ROOT_CA --pool="${POOL##*/}" --location="$REGION" \
#     --format="value(pemCaCertificates[0])" > gateway_ca/gateway-root.crt

# 2) Deploy bound to the gateway WITH the CA installed + trust env vars set:
python3 -m venv .venv && source .venv/bin/activate
pip install -q "google-adk[a2a,mcp]==2.2.0" "google-cloud-aiplatform[agent_engines]>=1.148.1"
python gateway_ca/deploy_with_gateway_ca.py     # prints the new engine resource name
```

`deploy_with_gateway_ca.py` ships `gateway_ca/install.sh` into the build (it runs
as root: `update-ca-certificates` for OpenSSL/aiohttp/requests, and builds a
combined PEM for gRPC), and sets the three trust env vars on the deployed agent:

```
SSL_CERT_FILE=/etc/ssl/certs/combined-ca.pem
REQUESTS_CA_BUNDLE=/etc/ssl/certs/combined-ca.pem
GRPC_DEFAULT_SSL_ROOTS_FILE_PATH=/etc/ssl/certs/combined-ca.pem   # gRPC C-core ignores the OS store; this is non-negotiable
```

## After deploy
Grant the new engine's identity `iap.egressor` (registry-wide) + baseline roles
(see DEPLOY.md Step 5) and smoke-test. With the CA trusted, the handshake
succeeds and the IAP allow/deny layer takes over (which is what the registry +
egressor were always for).

## Caveats
- **gRPC ordering:** `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` is read once at first
  channel creation. Deploy-time env_vars (as here) is correct; don't try to set
  it in code after `import vertexai`.
- **aiohttp:** doesn't honor `SSL_CERT_FILE` automatically, but
  `update-ca-certificates` into the OS store usually covers it. If the
  `aiplatform` leg still throws after this, that call needs an explicit
  `ssl.SSLContext` (ADK has no custom-CA knob yet — adk-python#2881).
- **Escalation (if the handshake still fails after installing the named-pool
  root):** ask Google Cloud, *"How do we obtain the root CA the Agent Gateway's
  Secure Web Proxy presents on the container egress leg, to add to the Agent
  Runtime trust store?"* — attach the failing `:authority` hostnames and the cert
  chain the container received.
