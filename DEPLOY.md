# DEPLOY — Stream agent on Agent Engine behind Agent Gateway (asia-southeast1)

Run in **Cloud Shell** on the colleague's project. The two MCP servers must
already be deployed (zee5-catalog-mcp, zee5-subscriber-mcp). The gateway
`agentgw-publicmcp` already exists.

End state: agent on Agent Engine (Singapore), Agent Identity, bound to the
gateway, egress to the two run.app MCPs + the agent's Google-API bootstrap
hosts governed by IAP in **ENFORCE** mode.

---

## Step 0 — Variables + prereqs

```bash
export PROJECT_ID="gm-test-337806"
export REGION="asia-southeast1"
export GATEWAY_ID="agentgw-publicmcp"
export MCP1_URL="https://zee5-catalog-mcp-tz5avjn3mq-as.a.run.app/mcp"
export MCP2_URL="https://zee5-subscriber-mcp-tz5avjn3mq-as.a.run.app/mcp"
export PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

gcloud config set project "$PROJECT_ID"
gcloud version    # MUST be >= 570.0.0 for the agent-registry / IAP / gateway commands
gcloud services enable aiplatform.googleapis.com networkservices.googleapis.com \
  networksecurity.googleapis.com agentregistry.googleapis.com iap.googleapis.com \
  run.googleapis.com cloudbuild.googleapis.com

git clone https://github.com/samayyy/zee5-stream-agent.git && cd zee5-stream-agent
```

## Step 1 — Verify the gateway points at the REGIONAL registry (#1 silent killer)

```bash
gcloud alpha network-services agent-gateways describe "$GATEWAY_ID" --location="$REGION" \
  | grep -E "registries|governedAccessPath|protocols"
```
`registries:` MUST read `.../locations/asia-southeast1` (NOT `/locations/global`),
`governedAccessPath: AGENT_TO_ANYWHERE`, `protocols: MCP`. If it says `global`,
re-import it pointing at the regional registry before continuing.

## Step 2 — Register destinations in the regional Agent Registry

```bash
# 2a. The two public MCP servers (exact run.app hosts; no permutations for run.app).
gcloud alpha agent-registry services create zee5-catalog-mcp \
  --project="$PROJECT_ID" --location="$REGION" --display-name="Catalog MCP" \
  --mcp-server-spec-type=no-spec --interfaces="url=${MCP1_URL},protocolBinding=JSONRPC"
gcloud alpha agent-registry services create zee5-subscriber-mcp \
  --project="$PROJECT_ID" --location="$REGION" --display-name="Subscriber MCP" \
  --mcp-server-spec-type=no-spec --interfaces="url=${MCP2_URL},protocolBinding=JSONRPC"

# 2b. The agent's own Google-API bootstrap egress (model on the GLOBAL aiplatform
#     endpoint + observability/registry/iap), ~15 services x hostname permutations.
./scripts/register_gateway_endpoints.sh "$PROJECT_ID" "$REGION"
```
> Model note: `gemini-3.1-flash-lite` calls go to the **global** `aiplatform.googleapis.com`
> (via the agent's `_GlobalGemini`). The script registers `aiplatform.googleapis.com` +
> `aiplatform.mtls.googleapis.com` (global) — keep them. The DRY_RUN audit (Step 6) is the
> authoritative list of what the runtime actually dials.

## Step 3 — Attach the IAP authz extension (DRY_RUN) + policy to the gateway

```bash
TOKEN=$(gcloud auth print-access-token)
# 3a. authz extension — DRY_RUN (observe, don't block) + mandatory iapPolicyVersion V1
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "https://networkservices.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/authzExtensions?authzExtensionId=agentgw-iap-authz" \
  -d '{"service":"iap.googleapis.com","failOpen":true,"timeout":"1s","metadata":{"iamEnforcementMode":"DRY_RUN","iapPolicyVersion":"V1"}}'
# wait ~30s for the LRO, then:
# 3b. policy binding the extension to the gateway
curl -fsS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "https://networksecurity.googleapis.com/v1alpha1/projects/${PROJECT_ID}/locations/${REGION}/authzPolicies?authz_policy_id=agentgw-iap-policy" \
  -d '{
    "name":"agentgw-iap-policy","policyProfile":"REQUEST_AUTHZ","action":"CUSTOM",
    "target":{"resources":["projects/'"${PROJECT_ID}"'/locations/'"${REGION}"'/agentGateways/'"${GATEWAY_ID}"'"]},
    "customProvider":{"authzExtension":{"resources":["projects/'"${PROJECT_ID}"'/locations/'"${REGION}"'/authzExtensions/agentgw-iap-authz"]}}
  }'
```

## Step 4 — Configure + deploy the agent (bound to the gateway)

```bash
cp stream_agent/.env.example stream_agent/.env     # values already target gm-test / Singapore / the run.app URLs
cat stream_agent/.env                              # eyeball: MCP URLs + project/region correct
grep agentGateway stream_agent/.agent_engine_config.json   # must name agentgw-publicmcp

python3 -m venv .venv && source .venv/bin/activate
pip install -r stream_agent/requirements.txt
python -c "from vertexai import types; print('agent_gateway_config' in types.AgentEngineConfig.model_fields)"  # True

adk deploy agent_engine --project="$PROJECT_ID" --region="$REGION" \
  --display_name="Stream Discovery Agent" stream_agent
export ENGINE_ID="<numeric id from 'Created a new instance' / 'Deployed to Agent Platform'>"
```
> Under DRY_RUN the agent starts even before the egressor grant. If it fails with
> `FAILED_PRECONDITION ... requires additional early-access activation`, the
> Agent-Engine↔Gateway binding entitlement isn't active for this project — escalate
> to your Google contact (this is the one thing only Google can enable).

## Step 5 — Grant the agent identity egress (roles/iap.egressor, registry-wide)

```bash
ORG_ID=$(gcloud projects get-ancestors "$PROJECT_ID" --format='csv[no-heading](id,type)' | awk -F, '$2=="organization"{print $1}')
PRINCIPAL="principal://agents.global.org-${ORG_ID}.system.id.goog/resources/aiplatform/projects/${PROJECT_NUMBER}/locations/${REGION}/reasoningEngines/${ENGINE_ID}"
TMP=$(mktemp)
gcloud beta iap web get-iam-policy --resource-type=agent-registry --region="$REGION" --format=json > "$TMP"
python3 - "$TMP" "$PRINCIPAL" <<'PY'
import json,sys
p=json.load(open(sys.argv[1])) or {}
b=next((x for x in p.setdefault("bindings",[]) if x.get("role")=="roles/iap.egressor" and not x.get("condition")), None)
if not b: b={"role":"roles/iap.egressor","members":[]}; p["bindings"].append(b)
if sys.argv[2] not in b["members"]: b["members"].append(sys.argv[2])
json.dump(p, open(sys.argv[1],"w"))
PY
gcloud beta iap web set-iam-policy "$TMP" --resource-type=agent-registry --region="$REGION"
# Also grant the baseline runtime roles to the agent identity:
for R in roles/aiplatform.user roles/aiplatform.expressUser roles/browser \
         roles/agentregistry.viewer roles/logging.logWriter roles/monitoring.metricWriter \
         roles/cloudtrace.agent roles/telemetry.writer roles/serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="$PRINCIPAL" --role="$R" --condition=None --format='value(etag)'
done
```

## Step 6 — Test under DRY_RUN, then read the IAP audit (authoritative host list)

```bash
PROJECT_ID="$PROJECT_ID" LOCATION="$REGION" python - <<PY
import asyncio, vertexai
c=vertexai.Client(project="$PROJECT_ID", location="$REGION")
app=c.agent_engines.get(name="projects/$PROJECT_ID/locations/$REGION/reasoningEngines/$ENGINE_ID")
async def go():
    s=await app.async_create_session(user_id="smoke")
    sid=s["id"] if isinstance(s,dict) else s.id
    async for e in app.async_stream_query(user_id="smoke", session_id=sid,
        message="What should I watch tonight? I like Telugu thrillers."):
        print(str(e)[:300])
asyncio.run(go())
PY

# IAP would-allow/deny per host — anything with audited_resource_name=unregisteredResource
# must be registered (Step 2) before you ENFORCE:
gcloud logging read 'protoPayload.serviceName="iap.googleapis.com"
  protoPayload.authorizationInfo.permission="iap.webServiceVersions.egressViaIAP"' \
  --limit=40 --freshness=15m \
  --format='value(timestamp,labels."iap.googleapis.com/audited_resource_name",protoPayload.authorizationInfo[0].granted)'
```

## Step 7 — Flip to ENFORCE (the real gateway)

Once Step 6 shows would-allow for every host the agent dials (no
`unregisteredResource`), enforce by **removing** `iamEnforcementMode` from the
extension (ENFORCE = the field is absent; there is no literal "ENFORCE" value):

```bash
TOKEN=$(gcloud auth print-access-token)
curl -fsS -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "https://networkservices.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/authzExtensions/agentgw-iap-authz?updateMask=metadata" \
  -d '{"metadata":{"iapPolicyVersion":"V1"}}'
```
Re-run the Step 6 smoke test — it should still answer. If a call now fails, the
IAP audit names the missing host: register it (Step 2), re-grant egressor if
needed, retry. To revert to observe-only, PATCH `iamEnforcementMode` back to
`DRY_RUN`.

---

## Troubleshooting (matches the patterns we hit before)

| Symptom | Fix |
|---|---|
| `FAILED_PRECONDITION ... early-access activation` on deploy | Binding entitlement not active for this project/region — only Google can enable; escalate. |
| `code 13 / Internal error` on bind, or gateway delete "in use" by a deleted engine | One-bonded-engine-per-project + dangling-ref deadlock (preview). Use a fresh region or escalate. |
| `CERTIFICATE_VERIFY_FAILED self signed cert` → `create_session FAILED_PRECONDITION` | An egress DENIAL in TLS costume — a host isn't registered/authorized. Check IAP audit for `unregisteredResource`. |
| `Network is unreachable` on a plain `*.googleapis.com` host | Someone set `GOOGLE_API_USE_CLIENT_CERTIFICATE=false` — remove it; the agent→gateway leg needs mTLS. |
| Registry entries invisible / everything denied | Gateway `registries:` points at `/locations/global` instead of regional. Re-import (Step 1). |
| gcloud "Invalid choice: agent-gateways/agent-registry" | gcloud < 570 — `gcloud components update`. |
