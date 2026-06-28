# Deploy Playbook — Stream Discovery agent + 2 MCPs + Agent Gateway (from scratch)

End-to-end deploy into a **fresh, empty Google Cloud project**, run entirely from
**Cloud Shell**. Produces: two public MCP servers on Cloud Run, an Agent Gateway
governing egress, and an ADK agent on Agent Engine bound to that gateway, with
IAP enforcement.

This sequence is the one validated end-to-end (startup + query + ENFORCE all green).

> Repos (all public, clone all three):
> - Agent:   `https://github.com/samayyy/zee5-stream-agent`
> - MCP A:   `https://github.com/samayyy/zee5-mcp-public`   (catalog / public data)
> - MCP B:   `https://github.com/samayyy/zee5-mcp-private`  (subscriber / private data)

---

## 0. Set variables + clone

```bash
export PROJECT_ID="REPLACE-WITH-NEW-PROJECT-ID"
export REGION="asia-southeast1"          # gateway, MCPs and agent all live here
export GATEWAY_ID="agentgw"
gcloud config set project "$PROJECT_ID"

# Cloud Shell ships an up-to-date gcloud; agent-gateways / agent-registry need >= 570.
gcloud version | head -1            # if < 570: gcloud components update

git clone https://github.com/samayyy/zee5-stream-agent.git
git clone https://github.com/samayyy/zee5-mcp-public.git
git clone https://github.com/samayyy/zee5-mcp-private.git
```

## 1. Enable APIs

```bash
gcloud services enable \
  aiplatform.googleapis.com \
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  networkservices.googleapis.com networksecurity.googleapis.com \
  agentregistry.googleapis.com iap.googleapis.com \
  cloudresourcemanager.googleapis.com compute.googleapis.com \
  logging.googleapis.com monitoring.googleapis.com
```

## 2. Staging bucket (for the Agent Engine build)

```bash
export STAGING_BUCKET="gs://${PROJECT_ID}-agent-engine-staging"
gcloud storage buckets create "$STAGING_BUCKET" --location="$REGION"
```

## 3. Deploy the two MCP servers to Cloud Run (public)

```bash
( cd zee5-mcp-public  && gcloud run deploy zee5-catalog-mcp    --source . \
    --region="$REGION" --allow-unauthenticated --ingress=all --quiet )
( cd zee5-mcp-private && gcloud run deploy zee5-subscriber-mcp --source . \
    --region="$REGION" --allow-unauthenticated --ingress=all --quiet )

# Capture the /mcp URLs the agent + gateway will use:
export PUBLIC_MCP_URL="$(gcloud run services describe zee5-catalog-mcp    --region="$REGION" --format='value(status.url)')/mcp"
export PRIVATE_MCP_URL="$(gcloud run services describe zee5-subscriber-mcp --region="$REGION" --format='value(status.url)')/mcp"
echo "$PUBLIC_MCP_URL"; echo "$PRIVATE_MCP_URL"

# Smoke-test (a stateless tools/call needs no initialize; SSE-framed reply):
curl -s -m 15 -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -X POST "$PUBLIC_MCP_URL" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_trending","arguments":{"max_results":2}}}' | head -c 300; echo
```

> First Cloud Run deploy in a brand-new region occasionally returns "Resource
> readiness deadline exceeded" — just re-run the `gcloud run deploy` once.

## 4. Create the Agent Gateway (bound to the REGIONAL registry)

```bash
cd zee5-stream-agent
envsubst < gateway.yaml.tmpl > gateway.yaml
cat gateway.yaml          # registries MUST end in /locations/$REGION  (NOT /global)

gcloud alpha network-services agent-gateways import "$GATEWAY_ID" \
  --location="$REGION" --project="$PROJECT_ID" --source=gateway.yaml

# verify registry scope is regional:
gcloud alpha network-services agent-gateways describe "$GATEWAY_ID" \
  --location="$REGION" --format="value(registries)"
```

## 5. Register egress destinations in the Agent Registry

The gateway matches destination hostnames EXACTLY and default-denies anything
unregistered. Register (a) the Google APIs the runtime itself calls and (b) the two MCPs.

```bash
# (a) ~15 Google APIs × 5 hostname permutations (incl. cloudresourcemanager + aiplatform):
./scripts/register_gateway_endpoints.sh "$PROJECT_ID" "$REGION"

# (b) the two MCP services:
gcloud alpha agent-registry services create zee5-catalog-mcp \
  --project="$PROJECT_ID" --location="$REGION" --display-name="zee5 catalog mcp" \
  --endpoint-spec-type=no-spec --interfaces="url=${PUBLIC_MCP_URL},protocolBinding=JSONRPC"
gcloud alpha agent-registry services create zee5-subscriber-mcp \
  --project="$PROJECT_ID" --location="$REGION" --display-name="zee5 subscriber mcp" \
  --endpoint-spec-type=no-spec --interfaces="url=${PRIVATE_MCP_URL},protocolBinding=JSONRPC"
```

## 6. Attach the IAP authz extension + policy (DRY_RUN first)

```bash
TOKEN=$(gcloud auth print-access-token)

# 6a. authz extension — DRY_RUN (observe, don't block) + mandatory iapPolicyVersion V1
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "https://networkservices.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/authzExtensions?authzExtensionId=${GATEWAY_ID}-iap-authz" \
  -d '{"service":"iap.googleapis.com","failOpen":true,"timeout":"1s","metadata":{"iamEnforcementMode":"DRY_RUN","iapPolicyVersion":"V1"}}'
sleep 30

# 6b. policy that binds the extension to the gateway (REQUEST_AUTHZ, CUSTOM)
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "https://networksecurity.googleapis.com/v1alpha1/projects/${PROJECT_ID}/locations/${REGION}/authzPolicies?authz_policy_id=${GATEWAY_ID}-iap-policy" \
  -d '{
    "name":"'"${GATEWAY_ID}"'-iap-policy","policyProfile":"REQUEST_AUTHZ","action":"CUSTOM",
    "target":{"resources":["projects/'"${PROJECT_ID}"'/locations/'"${REGION}"'/agentGateways/'"${GATEWAY_ID}"'"]},
    "customProvider":{"authzExtension":{"resources":["projects/'"${PROJECT_ID}"'/locations/'"${REGION}"'/authzExtensions/'"${GATEWAY_ID}"'-iap-authz"]}}
  }'

# The policy create is an LRO that takes ~80s to populate target/customProvider.
# Wait, then verify the target is set (target:{} immediately after = just timing, not failure):
sleep 90
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://networksecurity.googleapis.com/v1alpha1/projects/${PROJECT_ID}/locations/${REGION}/authzPolicies/${GATEWAY_ID}-iap-policy" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('target:',d.get('target'))"
```

## 7. Deploy the agent (bound to the gateway)

```bash
cd zee5-stream-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -q "google-adk[a2a,mcp]==2.2.0" "google-cloud-aiplatform[agent_engines]>=1.148.1"

PROJECT_ID="$PROJECT_ID" REGION="$REGION" GATEWAY_ID="$GATEWAY_ID" \
PUBLIC_MCP_URL="$PUBLIC_MCP_URL" PRIVATE_MCP_URL="$PRIVATE_MCP_URL" \
STAGING_BUCKET="$STAGING_BUCKET" \
python deploy.py            # prints: Done: .../reasoningEngines/<ENGINE_ID>

export ENGINE_ID="<the numeric id printed above>"
```

> If this fails with `400 FAILED_PRECONDITION ... requires additional early-access
> activation`, the project lacks the Agent-Engine↔Gateway binding entitlement —
> ask your Google contact to enable it (it is GA as of mid-2026, so most projects
> already have it).

## 8. Grant the agent identity egress (roles/iap.egressor)

```bash
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
ORG_ID=$(gcloud projects get-ancestors "$PROJECT_ID" --format='csv[no-heading](id,type)' | awk -F, '$2=="organization"{print $1}')
PRINCIPAL="principal://agents.global.org-${ORG_ID}.system.id.goog/resources/aiplatform/projects/${PROJECT_NUMBER}/locations/${REGION}/reasoningEngines/${ENGINE_ID}"
echo "$PRINCIPAL"     # sanity: must end in reasoningEngines/<ENGINE_ID>, not be empty

TMP=$(mktemp)
gcloud beta iap web get-iam-policy --resource-type=agent-registry --region="$REGION" --format=json > "$TMP"
python3 - "$TMP" "$PRINCIPAL" <<'PY'
import json,sys
tmp,principal=sys.argv[1],sys.argv[2]
p=json.load(open(tmp)) or {}
if not isinstance(p,dict): p={}
b=next((x for x in p.setdefault("bindings",[]) if x.get("role")=="roles/iap.egressor" and not x.get("condition")),None)
if not b: b={"role":"roles/iap.egressor","members":[]}; p["bindings"].append(b)
if principal not in b["members"]: b["members"].append(principal)
json.dump(p,open(tmp,"w"),indent=2)
PY
gcloud beta iap web set-iam-policy "$TMP" --resource-type=agent-registry --region="$REGION"
```

## 9. Test under DRY_RUN

In the console: **Agent Platform → Deployments → Stream Discovery Agent → Playground**,
new session, ask: *"Recommend one thing to watch tonight using my profile."*
It should call the MCP tools + model and answer with a personalized recommendation.

Or from Cloud Shell:
```bash
python3 - <<PY
import vertexai
c=vertexai.Client(project="$PROJECT_ID", location="$REGION", http_options=dict(api_version="v1beta1"))
a=c.agent_engines.get(name="projects/$PROJECT_ID/locations/$REGION/reasoningEngines/$ENGINE_ID")
s=a.create_session(user_id="u1")
for ev in a.stream_query(user_id="u1", session_id=s["id"], message="Recommend one thing to watch tonight using my profile."):
    print(ev)
PY
```

## 10. Flip to ENFORCE (the real gateway)

```bash
TOKEN=$(gcloud auth print-access-token)
# ENFORCE = REMOVE iamEnforcementMode (there is no literal "ENFORCE" value); keep iapPolicyVersion.
curl -fsS -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "https://networkservices.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/authzExtensions/${GATEWAY_ID}-iap-authz?updateMask=metadata" \
  -d '{"metadata":{"iapPolicyVersion":"V1"}}'
sleep 90    # the PATCH is a slow LRO

# verify (metadata should no longer contain iamEnforcementMode):
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://networkservices.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/authzExtensions/${GATEWAY_ID}-iap-authz" \
  | python3 -c "import sys,json;print(json.load(sys.stdin).get('metadata'))"
```

Re-run the Playground / Step 9 test — it should still answer, now fully enforced.

---

## Troubleshooting (the traps we actually hit)

| Symptom | Cause / fix |
|---|---|
| `CERTIFICATE_VERIFY_FAILED: self signed certificate` at startup | Gateway not fully configured yet (or engine bound before it was). Do a **clean rebind** (delete the failed engine, re-run Step 7). NOT a CA-install problem — a correctly-bound, fully-configured agent auto-trusts the gateway's TLS-inspection CA. |
| `UNAVAILABLE: 240.0.0.2:443 Handshake read failed (Socket closed)` | The gateway drops egress **before** TLS when there is no authz policy / no egressor. Finish Steps 6 + 8 before relying on egress. |
| Everything denied / registry entries invisible | Gateway `registries:` points at `locations/global` instead of `/locations/$REGION`. Re-import (Step 4). |
| Egress logs as `unregisteredEndpoint` under DRY_RUN | Harmless in DRY_RUN. Granted calls to *registered* hosts emit no IAP log; only unregistered/denied ones do. Before ENFORCE, confirm the exact hostnames dialed are registered (Step 5). |
| `RuntimeError: Event loop is closed` on queries | Fixed in this codebase: the global Gemini client is a plain `@property` (fresh per call), not a cached singleton. If you re-introduce a cached genai `Client`, the bug returns. |
| `400 FAILED_PRECONDITION ... early-access activation` on deploy | Project lacks the Agent-Engine↔Gateway binding entitlement — ask Google to enable. |
| `ModuleNotFoundError: No module named 'a2a'` in the container | The `[a2a]` extra must be installed — it is in `requirements` / `deploy.py`; don't strip it. |

## Without a gateway (baseline)

To deploy the agent without any gateway (public MCPs only, no egress governance):
set `NO_GATEWAY=1` and skip Steps 4–6, 8, 10:
```bash
PROJECT_ID=... REGION=... PUBLIC_MCP_URL=... PRIVATE_MCP_URL=... NO_GATEWAY=1 python deploy.py
```
