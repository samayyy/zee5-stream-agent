# zee5-stream-agent — Stream Discovery ADK agent (Agent Engine + Agent Gateway)

> **Deploying from scratch?** Follow [`PLAYBOOK.md`](PLAYBOOK.md) — a step-by-step
> Cloud Shell runbook that stands up the MCPs, the gateway, and this agent (with
> IAP enforcement) in a fresh project. `deploy.py` is the env-driven deploy entrypoint.

A Google ADK 2.x `LlmAgent` ("Stream Discovery Concierge") that personalizes
content recommendations using **two public Cloud Run MCP servers**:

- catalog (public data) — https://github.com/samayyy/zee5-mcp-public
- subscriber (private/account data) — https://github.com/samayyy/zee5-mcp-private

Deployed on **Vertex AI Agent Engine** (asia-southeast1) with a first-class
**Agent Identity**, bound to a Google Cloud **Agent Gateway** (`agentgw-publicmcp`)
that governs all egress (default-deny → Agent Registry + `roles/iap.egressor`).

The agent code is unchanged from the local/dev version — it's fully env-driven
(`PUBLIC_MCP_URL`, `PRIVATE_MCP_URL`); with the MCPs public and the gateway in
front, the OIDC/proxy/token paths stay inert.

## Deploy

Full step-by-step (registry → IAP authz → deploy → ENFORCE) is in
**[DEPLOY.md](DEPLOY.md)**. Prereqs: gcloud ≥ 570, the project's
Agent-Engine↔Gateway early-access entitlement active in asia-southeast1, and the
two MCP services already deployed (see the MCP repos).
