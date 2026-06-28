"""Deploy the Stream Discovery agent to Vertex AI Agent Engine.

Fully env-driven — no project hardcoding — so it works in any project from
Cloud Shell. Deploys with Agent Identity and (by default) BOUND to an Agent
Gateway so all egress is governed. Set NO_GATEWAY=1 to deploy without a gateway.

Required env:
  PROJECT_ID         e.g. my-new-project
  REGION             e.g. asia-southeast1
  PUBLIC_MCP_URL     e.g. https://zee5-catalog-mcp-XXXX.<region>.run.app/mcp
  PRIVATE_MCP_URL    e.g. https://zee5-subscriber-mcp-XXXX.<region>.run.app/mcp
  GATEWAY_ID         e.g. agentgw  (omit / set NO_GATEWAY=1 to skip gateway binding)

Optional env:
  STAGING_BUCKET     defaults to gs://<PROJECT_ID>-agent-engine-staging
  STREAM_MODEL       defaults to gemini-3.1-flash-lite
  STREAM_MODEL_LOCATION   defaults to global  (gemini-3.x flash-lite is global-only)
  STREAM_SUBSCRIBER_ID / STREAM_PLAN_TIER   demo subscriber defaults
  AGENT_ENGINE_ID    if set, UPDATE that engine in place instead of creating a new one
  NO_GATEWAY=1       deploy without binding to a gateway

Run from the repo root:  python deploy.py
"""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
os.chdir(_REPO_ROOT)

import vertexai
from vertexai import agent_engines


def _require(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"ERROR: required env var {name} is not set (see deploy.py header).")
    return v


PROJECT_ID = _require("PROJECT_ID")
REGION = _require("REGION")
PUBLIC_MCP_URL = _require("PUBLIC_MCP_URL")
PRIVATE_MCP_URL = _require("PRIVATE_MCP_URL")

NO_GATEWAY = os.environ.get("NO_GATEWAY", "").strip() not in ("", "0", "false", "False")
GATEWAY_ID = os.environ.get("GATEWAY_ID", "").strip()
if not NO_GATEWAY and not GATEWAY_ID:
    sys.exit("ERROR: set GATEWAY_ID (the gateway to bind to) or NO_GATEWAY=1 to skip.")

STAGING_BUCKET = os.environ.get("STAGING_BUCKET", f"gs://{PROJECT_ID}-agent-engine-staging")
if not STAGING_BUCKET.startswith("gs://"):
    STAGING_BUCKET = "gs://" + STAGING_BUCKET

STREAM_MODEL = os.environ.get("STREAM_MODEL", "gemini-3.1-flash-lite")
STREAM_MODEL_LOCATION = os.environ.get("STREAM_MODEL_LOCATION", "global")
STREAM_SUBSCRIBER_ID = os.environ.get("STREAM_SUBSCRIBER_ID", "SUB-IN-100023")
STREAM_PLAN_TIER = os.environ.get("STREAM_PLAN_TIER", "Premium-HD")
AGENT_ENGINE_ID = os.environ.get("AGENT_ENGINE_ID", "").strip()

# The agent module reads these at import; set them so importing root_agent here works too.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", PROJECT_ID)
os.environ.setdefault("STREAM_MODEL", STREAM_MODEL)
os.environ.setdefault("STREAM_MODEL_LOCATION", STREAM_MODEL_LOCATION)
os.environ.setdefault("PUBLIC_MCP_URL", PUBLIC_MCP_URL)
os.environ.setdefault("PRIVATE_MCP_URL", PRIVATE_MCP_URL)

vertexai.init(project=PROJECT_ID, location=REGION, staging_bucket=STAGING_BUCKET)
client = vertexai.Client(project=PROJECT_ID, location=REGION, http_options=dict(api_version="v1beta1"))

from stream_agent.agent import root_agent  # noqa: E402

app = agent_engines.AdkApp(agent=root_agent)

_config = {
    "display_name": "Stream Discovery Agent",
    "staging_bucket": STAGING_BUCKET,
    "identity_type": "AGENT_IDENTITY",
    "requirements": [
        "google-adk[a2a,mcp]==2.2.0",
        "google-cloud-aiplatform[agent_engines]>=1.148.1",
        "pydantic",
        "cloudpickle",
    ],
    # AdkApp pickles root_agent BY REFERENCE, so the container must be able to
    # `import stream_agent`.
    "extra_packages": ["stream_agent"],
    "env_vars": {
        # NOTE: GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION are RESERVED — the
        # Agent Engine runtime sets them; do NOT ship them here.
        "GOOGLE_GENAI_USE_VERTEXAI": "TRUE",
        "STREAM_MODEL": STREAM_MODEL,
        "STREAM_MODEL_LOCATION": STREAM_MODEL_LOCATION,
        "STREAM_SUBSCRIBER_ID": STREAM_SUBSCRIBER_ID,
        "STREAM_PLAN_TIER": STREAM_PLAN_TIER,
        "PUBLIC_MCP_URL": PUBLIC_MCP_URL,
        "PRIVATE_MCP_URL": PRIVATE_MCP_URL,
        # Keep agent-bound tokens shared with GCP services (do NOT set USE_CLIENT_CERTIFICATE=false).
        "GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES": "false",
        "ADK_ENABLE_MCP_GRACEFUL_ERROR_HANDLING": "1",
        "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
    },
}

if not NO_GATEWAY:
    _config["agent_gateway_config"] = {
        "agent_to_anywhere_config": {
            "agent_gateway": f"projects/{PROJECT_ID}/locations/{REGION}/agentGateways/{GATEWAY_ID}"
        }
    }
    print(f"Binding to gateway: {GATEWAY_ID}")
else:
    print("NO_GATEWAY=1 — deploying without a gateway binding")

if AGENT_ENGINE_ID:
    name = f"projects/{PROJECT_ID}/locations/{REGION}/reasoningEngines/{AGENT_ENGINE_ID}"
    print("Updating in place:", name)
    remote = client.agent_engines.update(name=name, agent=app, config=_config)
else:
    print(f"Creating a new engine in {PROJECT_ID}/{REGION}")
    remote = client.agent_engines.create(agent=app, config=_config)
print("Done:", remote.api_resource.name)
