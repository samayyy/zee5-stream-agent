# CERTIFICATE_VERIFY_FAILED on a gateway-bound agent — diagnose before you touch certs

> **Correction (June 26, 2026).** An earlier version of this file claimed the Agent
> Gateway always does TLS interception and that you must install its private root CA.
> **That was wrong.** The official codelab
> (codelabs.developers.google.com/cloudnet-agent-gateway) and the official
> [`terraform-google-agent-gateway`](https://github.com/GoogleCloudPlatform/terraform-google-agent-gateway)
> module install **no** custom CA, set **none** of the SSL trust env vars, and the
> `agentGateway` resource has **no TLS-inspection field at all** — yet their agent
> egresses fine. A correctly-configured agent-to-anywhere gateway does **not** re-sign
> your egress. **The CA install at the bottom of this file is NOT the normal fix.**
> The normal fix is `DEPLOY.md` (Agent Registry + IAP authz + `iap.egressor`).

## What the self-signed error almost always is
`self signed certificate in certificate chain` inside a gateway-bound container is
the gateway's **pre-IAP deny artifact for an UNREGISTERED destination**. The gateway
refuses to proxy any host that isn't in its **regional** Agent Registry, and on that
refusal the egress hop fails TLS. It surfaces first at **startup on the gRPC
`cloudresourcemanager` call** (`AdkApp.set_up` → `project_id`) because that is the
first Google API the agent dials.

It **persists under Audit-only / DRY_RUN** *not* because it's a TLS problem, but
because Audit-only relaxes only the **IAP** layer — the registry-layer block fires
**before** IAP (which is also why there is no IAP audit-log row for it). "Survives
Audit-only" therefore does **not** prove it's a cert problem.

## Diagnose (run these first — they decide everything)
```bash
PROJECT_ID=gm-test-337806; REGION=asia-southeast1; GATEWAY_ID=agentgw-publicmcp

# A) Is TLS inspection even configured?  EMPTY = no interception = do NOT install a CA.
gcloud network-security tls-inspection-policies list --location="$REGION" --project="$PROJECT_ID"

# B) Does the gateway point at the REGIONAL registry?  (/locations/global = everything denied)
gcloud alpha network-services agent-gateways describe "$GATEWAY_ID" \
  --location="$REGION" --project="$PROJECT_ID" \
  --format="yaml(registries,googleManaged,protocols)"
#    registries: MUST end in /locations/asia-southeast1   (NOT /locations/global)

# C) Are the Google APIs the agent itself calls actually registered?
gcloud alpha agent-registry services list --project="$PROJECT_ID" --location="$REGION" \
  --format="value(name)" | grep -E "cloudresourcemanager|aiplatform" || echo "NOT REGISTERED"
```

## Fix (the real one — full runbook in DEPLOY.md)
- **B shows `/locations/global`** → re-import the gateway pointing at the regional
  registry (DEPLOY.md Step 1). This alone makes every regional registration visible.
- **C prints `NOT REGISTERED`** → `./scripts/register_gateway_endpoints.sh $PROJECT_ID $REGION`
  (registers ~15 Google APIs × 5 hostname permutations + the MCPs), then attach the IAP
  authz policy (DRY_RUN, Step 3), grant `roles/iap.egressor` registry-wide (Step 5), test
  under DRY_RUN reading the IAP audit log (Step 6), then flip to ENFORCE (Step 7).

The agent dying at startup on `cloudresourcemanager` is the classic signature of **C**:
that Google API was never registered (or the registry is global, **B**).

## Only if A returns a TLS-inspection policy
Then — and only then — the gateway's egress Secure Web Proxy really is re-signing with
a private CA and the container must trust it. The scaffolding in this repo
(`gateway_ca/deploy_with_gateway_ca.py` + `installation_scripts/install.sh`) handles
that case: it installs the CA from the TLS-inspection policy's CA-Service pool into both
trust stacks and points `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` /
`GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` at a combined bundle. **This is in addition to
registration (DEPLOY.md), never instead of it** — even with the CA trusted, an
unregistered destination is still blocked before IAP.

```bash
# (conditional) fetch the CA from the TLS-inspection policy's CA pool:
POOL=$(gcloud network-security tls-inspection-policies list --location="$REGION" --format='value(caPool)' | head -1)
gcloud privateca roots list --pool="${POOL##*/}" --location="$REGION" --format='value(name)'   # -> ROOT_CA
gcloud privateca roots describe ROOT_CA --pool="${POOL##*/}" --location="$REGION" \
  --format="value(pemCaCertificates[0])" > gateway-root.crt
python gateway_ca/deploy_with_gateway_ca.py
```

### gRPC caveat (only relevant in the CA case)
`GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` is read once at first channel creation and Python
gRPC sometimes ignores it (grpc/grpc#27549). Merging the root into the compiled-in
bundle (`SSL_CERT_FILE`) as `install.sh` does is the reliable path; aiohttp doesn't
honor `SSL_CERT_FILE` (aiohttp#3180) but `update-ca-certificates` covers it.
