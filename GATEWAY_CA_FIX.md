# Fixing CERTIFICATE_VERIFY_FAILED on a gateway-bound agent

> **Settled June 26, 2026 by decoding the live gateway cert.** A bound agent failing
> `CERTIFICATE_VERIFY_FAILED: self signed certificate in certificate chain` on every
> egress (gRPC `cloudresourcemanager` at startup, aiohttp `aiplatform`, the run.app
> MCPs) is **TLS interception by the gateway's managed Secure Web Proxy** — and the
> fix is to **trust the gateway's own root CA** in the container.

## What's actually happening (proven, not inferred)
`gcloud network-services agent-gateways describe <GW> --format=yaml` exposes:
```
agentGatewayCard:
  mtlsEndpoint: projects/<...>/regions/<region>/serviceAttachments/<...>-swp-mtls-psc-sa   # a Secure Web Proxy, over PSC
  rootCertificates:
  - |
    -----BEGIN CERTIFICATE-----  (decode it: openssl x509 -noout -subject -issuer)
```
Decoding that cert shows: `CN = Agent Gateway TLS Inspection CA (<region>)`, **self-signed**,
`CA:TRUE`, EKU `TLS Web Server Authentication`. So the agent-to-anywhere egress path is:

```
agent container  --mTLS/PSC-->  managed Secure Web Proxy  -->  destination
                                 (re-signs every TLS conn with the "Agent Gateway TLS Inspection CA")
```

The SWP **re-signs** the connection it presents back to the agent. The Agent Runtime
doesn't trust that self-signed CA → `self signed certificate in certificate chain`,
on the **first** egress hop (`AdkApp.set_up` → `project_id` → gRPC `cloudresourcemanager`).
It **persists under Audit-only** because re-signing happens at the TLS handshake, before
any IAP decision.

### Two traps that make this look like something else
- **`gcloud network-security tls-inspection-policies list` returns 0 — this is a RED HERRING.**
  That lists only *customer-created* TLS-inspection-policy resources. The agent gateway's
  interception is **intrinsic to the Google-managed SWP** and is exposed *only* via
  `agentGatewayCard.rootCertificates`. Zero policies ≠ no interception.
- **The codelab / `terraform-google-agent-gateway` install no CA** — because a correctly-bound
  runtime is meant to **auto-trust** the card root. If that auto-trust isn't reaching the
  failing leg (notably the gRPC C-core channel, which ignores the OS trust store), you must
  install it yourself.

## Diagnose (one command)
```bash
PROJECT_ID=...; REGION=...; GW=...
gcloud network-services agent-gateways describe "$GW" --location="$REGION" --project="$PROJECT_ID" \
  --format="value(agentGatewayCard.rootCertificates)" > gateway-root.crt
openssl x509 -in gateway-root.crt -noout -subject -issuer    # CN contains "Agent Gateway TLS Inspection CA" -> install it
```

## The fix — trust the gateway root in the container, then redeploy bound
```bash
cd zee5-stream-agent
# gateway-root.crt fetched above (agentGatewayCard.rootCertificates)
head -1 gateway-root.crt   # -----BEGIN CERTIFICATE-----
python3 -m venv .venv && source .venv/bin/activate
pip install -q "google-adk[a2a,mcp]==2.2.0" "google-cloud-aiplatform[agent_engines]>=1.148.1"
python gateway_ca/deploy_with_gateway_ca.py     # ships + installs the CA at build time, sets the trust env vars
```
`installation_scripts/install.sh` runs as root at build time: `update-ca-certificates`
(covers OpenSSL/requests/httpx/aiohttp) **and** builds a combined PEM, then the deploy sets:
```
SSL_CERT_FILE=/etc/ssl/certs/combined-ca.pem
REQUESTS_CA_BUNDLE=/etc/ssl/certs/combined-ca.pem
GRPC_DEFAULT_SSL_ROOTS_FILE_PATH=/etc/ssl/certs/combined-ca.pem   # gRPC C-core ignores the OS store; non-negotiable — this is the leg that fails first
```

## Try-first (cleaner, no container changes)
Since a correctly-bound runtime is *supposed* to auto-trust the card root, a **clean
rebind / redeploy** of the bound agent sometimes provisions the trust on its own. Try that
first; if startup still throws the self-signed error, install the CA as above.

## Notes
- **gRPC ordering:** `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` is read once at first channel
  creation — set it via deploy-time `env_vars` (as here), never in code after `import vertexai`.
- **aiohttp** doesn't honor `SSL_CERT_FILE` (aiohttp#3180), but `update-ca-certificates`
  into the OS store covers it.
- After deploy, ensure the registry/authz/`iap.egressor` layer is also correct (DEPLOY.md
  Steps 1–5) — trusting the CA fixes the handshake; IAP then governs allow/deny.
- **Escalate to Google** only if the handshake still fails after the CA is verifiably
  installed (cert from `agentGatewayCard.rootCertificates`) on both the OpenSSL and gRPC paths.
