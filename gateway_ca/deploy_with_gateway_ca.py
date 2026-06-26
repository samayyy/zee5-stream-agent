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

import vertexai
from vertexai import agent_engines

PROJECT_ID = os.environ.get("PROJECT_ID", "gm-test-337806")
REGION = os.environ.get("REGION", "asia-southeast1")
GATEWAY_ID = os.environ.get("GATEWAY_ID", "agentgw-publicmcp")
MCP1_URL = os.environ.get("MCP1_URL", "https://zee5-catalog-mcp-719187342121.asia-southeast1.run.app/mcp")
MCP2_URL = os.environ.get("MCP2_URL", "https://zee5-subscriber-mcp-719187342121.asia-southeast1.run.app/mcp")

CA = "gateway_ca/gateway-root.crt"
if not os.path.exists(CA):
    raise SystemExit(f"Missing {CA} — fetch the gateway root CA first (see GATEWAY_CA_FIX.md).")

# The agent reads these at import; set them so the module imports cleanly here too.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", PROJECT_ID)
os.environ.setdefault("STREAM_MODEL", "gemini-3.1-flash-lite")
os.environ.setdefault("STREAM_MODEL_LOCATION", "global")
os.environ.setdefault("PUBLIC_MCP_URL", MCP1_URL)
os.environ.setdefault("PRIVATE_MCP_URL", MCP2_URL)

vertexai.init(project=PROJECT_ID, location=REGION)
client = vertexai.Client(project=PROJECT_ID, location=REGION, http_options=dict(api_version="v1beta1"))

from stream_agent.agent import root_agent  # noqa: E402

app = agent_engines.AdkApp(agent=root_agent)

COMBINED = "/etc/ssl/certs/combined-ca.pem"
remote = client.agent_engines.create(
    agent=app,
    config={
        "display_name": "Stream Discovery Agent (gateway+CA)",
        "identity_type": "AGENT_IDENTITY",
        "agent_gateway_config": {
            "agent_to_anywhere_config": {
                "agent_gateway": f"projects/{PROJECT_ID}/locations/{REGION}/agentGateways/{GATEWAY_ID}"
            }
        },
        "requirements": [
            "google-adk[a2a,mcp]==2.2.0",
            "google-cloud-aiplatform[agent_engines]>=1.148.1",
        ],
        # ship the CA + install script into the build, and run the script (as root):
        "extra_packages": ["gateway_ca/install.sh", "gateway_ca/gateway-root.crt"],
        "build_options": {"installation_scripts": ["gateway_ca/install.sh"]},
        "env_vars": {
            "GOOGLE_GENAI_USE_VERTEXAI": "TRUE",
            "GOOGLE_CLOUD_PROJECT": PROJECT_ID,
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
    },
)
print("Deployed:", remote.api_resource.name)
