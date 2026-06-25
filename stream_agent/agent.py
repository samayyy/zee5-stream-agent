"""Stream Personalized Discovery agent (Google ADK 2.x).

A single LlmAgent that holds TWO McpToolset instances, both pointed at public
Cloud Run FastMCP servers (Streamable HTTP):
  - PUBLIC catalog MCP        (synthetic content catalog)
  - PRIVATE personalization MCP (synthetic subscriber/account data)

Both toolsets are constructed SYNCHRONOUSLY at import time so that `adk web`,
`adk api_server`, and Vertex AI Agent Engine can discover the tools.

Egress governance: on Agent Engine this agent is bound to a Google Cloud Agent
Gateway (agent-to-anywhere). The gateway transparently governs all outbound
egress (the agent just dials the literal run.app MCP URLs); allowed destinations
are controlled by the Agent Registry + IAM (roles/iap.egressor), not by code.
The legacy PROXY_SERVER_URL / OIDC (PUBLIC_MCP_AUDIENCE) / static-token paths
below stay for backward-compat and are INERT when those env vars are unset —
which is the case for the gateway deployment (MCPs are public/unauthenticated).
"""
import os

import httpx

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.models.google_llm import Gemini
from google.genai import types as genai_types
from google.genai import Client as GenaiClient

from .instructions import DISCOVERY_INSTRUCTION

MODEL = os.environ.get("STREAM_MODEL", "gemini-2.5-flash")  # gemini-flash-latest 404s on Vertex us-central1
# "global" routes the MODEL via the global Vertex endpoint (gemini-3.x flash-lite is global-only),
# while GOOGLE_CLOUD_LOCATION (used by the Agent Engine session service) stays regional.
MODEL_LOCATION = os.environ.get("STREAM_MODEL_LOCATION", "")
SUBSCRIBER_ID = os.environ.get("STREAM_SUBSCRIBER_ID", "SUB-IN-100023")
PLAN_TIER = os.environ.get("STREAM_PLAN_TIER", "Premium-HD")

PUBLIC_MCP_URL = os.environ.get("PUBLIC_MCP_URL", "http://127.0.0.1:8081/mcp")
PUBLIC_MCP_TOKEN = os.environ.get("PUBLIC_MCP_TOKEN", "")
PRIVATE_MCP_URL = os.environ.get("PRIVATE_MCP_URL", "http://127.0.0.1:8082/mcp")
PRIVATE_MCP_TOKEN = os.environ.get("PRIVATE_MCP_TOKEN", "")
PROXY_SERVER_URL = os.environ.get("PROXY_SERVER_URL", "")  # set ONLY on Agent Engine
# When set (cloud), the public MCP is IAM-gated (--no-allow-unauthenticated) and reached with a
# Google OIDC token whose audience is this Cloud Run service ROOT url. Unset locally → static token.
PUBLIC_MCP_AUDIENCE = os.environ.get("PUBLIC_MCP_AUDIENCE", "")

PUBLIC_TOOLS = ["search_catalog", "get_title_metadata", "get_similar_titles",
                "get_trending", "get_new_releases"]
PRIVATE_TOOLS = ["get_profile", "get_watch_history", "get_continue_watching",
                 "get_taste_profile", "get_entitlements", "get_ratings",
                 "get_subscription", "get_billing", "get_transactions", "get_plans"]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


class _OidcAuth(httpx.Auth):
    """Attaches a Google-signed OIDC ID token (audience = the Cloud Run service URL) to every
    request, refreshing before expiry — so the agent can call the IAM-gated public catalog MCP
    (--no-allow-unauthenticated). Works wherever a metadata server / service account is present
    (Cloud Run, Agent Engine). Applied at the httpx layer so it also covers tool discovery."""

    def __init__(self, audience: str):
        self._audience = audience
        self._token = ""
        self._exp = 0.0

    def _id_token(self) -> str:
        import time
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2 import id_token as google_id_token
        if not self._token or time.time() >= self._exp:
            self._token = google_id_token.fetch_id_token(GoogleAuthRequest(), self._audience)
            self._exp = time.time() + 3000  # ~1h tokens; refresh comfortably early
        return self._token

    def sync_auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {self._id_token()}"
        yield request

    async def async_auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {self._id_token()}"
        yield request


def _public_httpx_factory(proxy_url: str, oidc_audience: str):
    """httpx client factory for the PUBLIC toolset: routes through the in-VPC proxy on Agent
    Engine (proxy_url) and/or attaches OIDC auth for the IAM-gated MCP (oidc_audience)."""
    oidc = _OidcAuth(oidc_audience) if oidc_audience else None

    def factory(headers=None, timeout=None, auth=None):
        kwargs = {"follow_redirects": True}
        if headers is not None:
            kwargs["headers"] = headers
        if timeout is not None:
            kwargs["timeout"] = timeout
        if proxy_url:
            kwargs["proxy"] = proxy_url
        kwargs["auth"] = oidc or auth  # OIDC wins when configured
        return httpx.AsyncClient(**kwargs)

    return factory


# ---- PUBLIC catalog toolset (internet) -------------------------------------------------
# Cloud: IAM-gated MCP reached with OIDC (PUBLIC_MCP_AUDIENCE set), optionally via the Agent
# Engine proxy. Local dev: static bearer token (PUBLIC_MCP_TOKEN), no custom httpx client.
_public_params = dict(
    url=PUBLIC_MCP_URL,
    headers={} if PUBLIC_MCP_AUDIENCE else _bearer(PUBLIC_MCP_TOKEN),
    timeout=20.0,
    sse_read_timeout=300.0,
)
if PROXY_SERVER_URL or PUBLIC_MCP_AUDIENCE:
    _public_params["httpx_client_factory"] = _public_httpx_factory(PROXY_SERVER_URL, PUBLIC_MCP_AUDIENCE)

public_catalog = McpToolset(
    connection_params=StreamableHTTPConnectionParams(**_public_params),
    tool_filter=PUBLIC_TOOLS,
)

# ---- PRIVATE personalization toolset (VPN / PSC-I only) --------------------------------
private_personalization = McpToolset(
    connection_params=StreamableHTTPConnectionParams(
        url=PRIVATE_MCP_URL, headers=_bearer(PRIVATE_MCP_TOKEN),
        timeout=20.0, sse_read_timeout=300.0,
    ),
    tool_filter=PRIVATE_TOOLS,
)

# gemini-3.x flash-lite is served ONLY on the GLOBAL Vertex endpoint. _GlobalGemini pins the
# MODEL's genai client to location="global" (ADK's documented api_client override), independent of
# GOOGLE_CLOUD_LOCATION — so the Agent Engine session service can stay regional (us-central1).
_global_client: GenaiClient | None = None


def _global_gemini_client() -> GenaiClient:
    global _global_client
    if _global_client is None:
        _global_client = GenaiClient(
            vertexai=True, project=os.environ.get("GOOGLE_CLOUD_PROJECT"), location="global"
        )
    return _global_client


class _GlobalGemini(Gemini):
    @property
    def api_client(self) -> GenaiClient:  # type: ignore[override]
        return _global_gemini_client()


# Retry transient Vertex 429s (dynamic shared quota) with backoff rather than failing the turn.
_retry = genai_types.HttpRetryOptions(
    attempts=6, initial_delay=1.0, max_delay=30.0, exp_base=2.0,
    http_status_codes=[429, 503, 500],
)
_MODEL = (_GlobalGemini if MODEL_LOCATION == "global" else Gemini)(model=MODEL, retry_options=_retry)

root_agent = LlmAgent(
    model=_MODEL,
    name="stream_discovery_agent",
    description="Personalized Stream Discovery content-discovery concierge using public catalog + private subscriber data.",
    instruction=DISCOVERY_INSTRUCTION.format(subscriber_id=SUBSCRIBER_ID, plan_tier=PLAN_TIER),
    tools=[public_catalog, private_personalization],
)
