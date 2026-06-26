"""Deploy the Stream agent to Agent Engine BOUND to the Agent Gateway, with the
gateway's private root CA installed into the container trust stores.

Use this instead of `adk deploy agent_engine` ONLY for the gateway-bound case —
it adds the build-time CA install + the three trust env vars, which `adk deploy`
can't express cleanly. (For the no-gateway case, plain `adk deploy` is fine.)

Prereqs in this folder before running:
  gateway_ca/gateway-root.crt   <- the gateway's private root CA PEM (see GATEWAY_CA_FIX.md)

Env (override as needed):
  PROJECT_ID, REGION, GATEWAY_ID, MCP1_URL, MCP2_URL

Run from the repo root:  python gateway_ca/deploy_with_gateway_ca.py
"""
import os
import sys

# This script lives in <repo>/gateway_ca/; ensure the repo root is importable so
# `from stream_agent.agent import root_agent` works regardless of CWD.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
# extra_packages paths ("installation_scripts/install.sh", "gateway-root.crt") are validated
# relative to CWD, so anchor CWD at the repo root.
os.chdir(_REPO_ROOT)

import vertexai
from vertexai import agent_engines

PROJECT_ID = os.environ.get("PROJECT_ID", "gm-test-337806")
REGION = os.environ.get("REGION", "asia-southeast1")
GATEWAY_ID = os.environ.get("GATEWAY_ID", "agentgw-publicmcp")
MCP1_URL = os.environ.get("MCP1_URL", "https://zee5-catalog-mcp-719187342121.asia-southeast1.run.app/mcp")
MCP2_URL = os.environ.get("MCP2_URL", "https://zee5-subscriber-mcp-719187342121.asia-southeast1.run.app/mcp")

# CA lives at the repo root (NOT under installation_scripts/, which the SDK reserves
# for declared scripts only). Resolve it relative to the repo root so CWD doesn't matter.
CA = os.path.join(_REPO_ROOT, "gateway-root.crt")
if not os.path.exists(CA):
    raise SystemExit("Missing gateway-root.crt at the repo root — fetch the gateway root CA first (see GATEWAY_CA_FIX.md).")

# The agent reads these at import; set them so the module imports cleanly here too.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", PROJECT_ID)
os.environ.setdefault("STREAM_MODEL", "gemini-3.1-flash-lite")
os.environ.setdefault("STREAM_MODEL_LOCATION", "global")
os.environ.setdefault("PUBLIC_MCP_URL", MCP1_URL)
os.environ.setdefault("PRIVATE_MCP_URL", MCP2_URL)

# build_options/extra_packages require a GCS staging bucket to upload the build context.
STAGING_BUCKET = os.environ.get("STAGING_BUCKET", f"gs://{PROJECT_ID}-agent-engine-staging")
if not STAGING_BUCKET.startswith("gs://"):
    STAGING_BUCKET = "gs://" + STAGING_BUCKET

vertexai.init(project=PROJECT_ID, location=REGION, staging_bucket=STAGING_BUCKET)
client = vertexai.Client(project=PROJECT_ID, location=REGION, http_options=dict(api_version="v1beta1"))

from stream_agent.agent import root_agent  # noqa: E402

app = agent_engines.AdkApp(agent=root_agent)

# UPDATE the existing engine in place when AGENT_ENGINE_ID is set (keeps the same
# bond + iap.egressor grant, avoids the one-bonded-engine-per-project limit).
# Otherwise CREATE a new bonded engine.
AGENT_ENGINE_ID = os.environ.get("AGENT_ENGINE_ID", "").strip()

COMBINED = "/etc/ssl/certs/combined-ca.pem"
# NO_GATEWAY=1 omits the gateway binding — used for a local validation dry-run in a
# project without the gateway-binding entitlement (exercises requirements / staging /
# installation_scripts / build_options / the CA-install at build, minus the bind).
NO_GATEWAY = os.environ.get("NO_GATEWAY", "").strip() not in ("", "0", "false", "False")
_config = {
        "display_name": "Stream Discovery Agent (gateway+CA)",
        "staging_bucket": STAGING_BUCKET,
        "identity_type": "AGENT_IDENTITY",
        "requirements": [
            "google-adk[a2a,mcp]==2.2.0",
            "google-cloud-aiplatform[agent_engines]>=1.148.1",
            "pydantic",
            "cloudpickle",
        ],
        # ship: the agent package (AdkApp pickles root_agent BY REFERENCE, so the
        # container must be able to `import stream_agent`), the declared install
        # script (under installation_scripts/), and the CA data file (repo root).
        "extra_packages": ["stream_agent", "installation_scripts/install.sh", "gateway-root.crt"],
        "build_options": {"installation_scripts": ["installation_scripts/install.sh"]},
        "env_vars": {
            # NOTE: GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION are RESERVED — the
            # Agent Engine runtime sets them; shipping them here is rejected
            # ("Environment variable name 'GOOGLE_CLOUD_PROJECT' is reserved").
            "GOOGLE_GENAI_USE_VERTEXAI": "TRUE",
            "STREAM_MODEL": "gemini-3.1-flash-lite",
            "STREAM_MODEL_LOCATION": "global",
            "STREAM_SUBSCRIBER_ID": "SUB-IN-100023",
            "STREAM_PLAN_TIER": "Premium-HD",
            "PUBLIC_MCP_URL": MCP1_URL,
            "PRIVATE_MCP_URL": MCP2_URL,
            "GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES": "false",
            "ADK_ENABLE_MCP_GRACEFUL_ERROR_HANDLING": "1",
            "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
            # --- the trust fix: point all three trust stacks at the combined bundle ---
            "SSL_CERT_FILE": COMBINED,
            "REQUESTS_CA_BUNDLE": COMBINED,
            "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH": COMBINED,
        },
}

if not NO_GATEWAY:
    _config["agent_gateway_config"] = {
        "agent_to_anywhere_config": {
            "agent_gateway": f"projects/{PROJECT_ID}/locations/{REGION}/agentGateways/{GATEWAY_ID}"
        }
    }
else:
    print("NO_GATEWAY dry-run: omitting the gateway binding")

if AGENT_ENGINE_ID:
    name = f"projects/{PROJECT_ID}/locations/{REGION}/reasoningEngines/{AGENT_ENGINE_ID}"
    print("Updating in place:", name)
    remote = client.agent_engines.update(name=name, agent=app, config=_config)
else:
    print("Creating a new bonded engine")
    remote = client.agent_engines.create(agent=app, config=_config)
print("Done:", remote.api_resource.name)
