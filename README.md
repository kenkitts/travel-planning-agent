# Travel Planning Agent

A conversational travel-itinerary-building agent hosted on Amazon Bedrock
AgentCore. It asks clarifying questions, then builds a day-by-day itinerary
grounded in live web search, weather forecasts, and points-of-interest
lookups. It does not book anything — see `DESIGN.md` for the full set of
design decisions and rationale, and `PLAN.md` for the phased build plan.

The hosted web UI (below) is the only supported way to use this agent —
there is no CLI or other standalone client.

## Architecture

```
Web UI (web/server.py), hosted on ECS Fargate behind a plain ALB —
the server itself runs the OIDC login flow against Okta and issues
its own KMS-encrypted session cookie
   │  IAM/SigV4 InvokeAgentRuntime (via web/agent_client.py)
   │  actor_id passed explicitly in the payload
   ▼
AgentCore Runtime (IAM auth)  ── hosts ──▶  Strands Agent (Python, Claude Sonnet via Bedrock)
   │                                   │
   │                                   ├─▶ AgentCore Memory (short-term events +
   │                                   │    long-term traveler preferences /
   │                                   │    session summaries, scoped by the
   │                                   │    caller-supplied actor_id)
   │                                   │
   │                                   └─▶ AgentCore Gateway (MCP, IAM auth)
   │                                          ├─ Web Search (managed connector)
   │                                          ├─ Weather Lambda (Open-Meteo)
   │                                          └─ Places Lambda (Amazon Location Service)
```

Five CDK stacks (deployed in this order):
- `TravelAgentToolsStack` — the weather and places Lambda functions
- `TravelAgentGatewayStack` — the AgentCore Gateway and its three targets
- `TravelAgentMemoryStack` — the AgentCore Memory resource
- `TravelAgentRuntimeStack` — the AgentCore Runtime hosting the agent
- `TravelAgentWebStack` — hosts the web UI on ECS Fargate behind a plain
  ALB (TLS termination/routing only — no auth logic of its own; see
  "Authentication" below). Required — this is the only supported way to
  use the agent. See "Hosting the Web UI" below.

## Repo layout

```
travel-planning-agent/
├── DESIGN.md              # design decisions + rationale
├── PLAN.md                # phased build plan, with completion notes
├── agent/                 # Strands agent + BedrockAgentCoreApp entrypoint
├── cdk/                   # CDK app (Python) — 5 stacks, see cdk/stacks/
├── lambdas/                # weather + places tool Lambdas, with tests
├── tests/                  # unit tests for the Lambda handlers + agent helpers
└── web/                    # the only client: server.py + agent_client.py +
                             # static/ + Dockerfile, see below
```

## Prerequisites

- Python 3.12
- Node.js (for the `cdk` CLI) — `npm install -g aws-cdk`
- An AWS account with:
  - Bedrock model access enabled for the Claude Sonnet model used
    (`us.anthropic.claude-sonnet-5` by default — check/enable in the Bedrock
    console under **Model access**)
  - CDK bootstrapped in the target region (`cdk bootstrap`)
- AWS credentials for that account active in your shell (e.g. via `aws sso
  login` or an equivalent credential process) — used for `cdk deploy`
  itself; at runtime, the deployed ECS task uses its own IAM role instead
  (see "Authentication" below).

## Authentication

The web UI has no auth of its own and cannot run standalone —
`web/server.py` runs the entire OAuth 2.0 Authorization Code + PKCE flow
against a dedicated Okta application itself, storing the resulting tokens
in a single, KMS-envelope-encrypted session cookie. An unauthenticated
top-level page load is redirected straight to Okta; an unauthenticated
`fetch()`/API call gets a clean `401`. If your access token expires, the
server transparently refreshes it (using the stored refresh token)
in-line with the next request — no login interruption unless the refresh
token itself has also expired, in which case you're sent back through the
Okta login. `actor_id` (scoping long-term AgentCore Memory — preferences,
conversation history) is derived automatically from each logged-in
person's verified Okta identity (`sub` claim). There is no supported way
to run `web/server.py` directly on your own machine as a standalone
single-user tool without its own real Okta app configuration — see
"Hosting the Web UI" below.

The Runtime itself is IAM/SigV4-authenticated (`InvokeAgentRuntime`, via
boto3) — there is no separate identity provider or bearer-token flow for
the Runtime. `TravelAgentWebStack`'s ECS task calls it with its own IAM
role; there is no other caller.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r cdk/requirements.txt
pip install -r tests/requirements.txt
```

## Deploy

From the `cdk/` directory, with your venv activated and AWS credentials
active:

```bash
cd cdk
cdk bootstrap   # once per account/region, if not already done
cdk deploy --all --require-approval never
```

This deploys the four base stacks (`TravelAgentToolsStack`,
`TravelAgentGatewayStack`, `TravelAgentMemoryStack`,
`TravelAgentRuntimeStack`) in dependency order. Deploying
`TravelAgentRuntimeStack` also bundles `agent/` (pip-installs
`agent/requirements.txt` into the asset) — this step can take a minute or
two the first time.

`TravelAgentWebStack` is deployed only if `WEB_CERTIFICATE_ARN` (and the
other `WEB_*` variables) are set — see "Hosting the Web UI" below. Without
them, `cdk deploy --all`/`cdk synth --all` simply skip it, but the agent
is not usable at all until it's deployed (there is no other client).

To update just the agent after a code change:

```bash
cdk deploy TravelAgentRuntimeStack --require-approval never
```

### Tear down

```bash
cdk destroy --all
```

This deletes every deployed stack and its resources (Lambdas, Gateway,
Memory, Runtime, and — if deployed — the VPC/ALB/ECS resources from
`TravelAgentWebStack`). It does **not** delete the CDK bootstrap stack
(`CDKToolkit`) or its S3 asset bucket.

## Web UI

The hosted web UI is the only supported way to use this agent. There is
no supported way to use it without AWS infrastructure — see "Hosting the
Web UI" below to deploy `TravelAgentWebStack`, which is required before
this UI is usable at all.

`web/server.py` requires a real, dedicated Okta app registration (see
"Hosting the Web UI" below) and rejects every `/api/chat` and
`/api/conversations*` request with a `401` (or, for a top-level page
load, redirects it straight to Okta) unless it carries a valid, KMS-
decryptable session cookie the server itself issued after a real Okta
login (see "Authentication" above) — running it "locally" without a real
Okta app and a KMS key it has access to is not a usable end-user mode, so
this is primarily a development/testing aid for the hosted deployment
itself rather than a way to use the chat UI without AWS infrastructure.

To run it against a real deployment's configuration (see "Hosting the
Web UI" below for how to obtain each value):

```bash
pip install -r web/requirements.txt
python web/server.py --agent-runtime-arn <runtime-arn> --memory-id <memory-id> \
    --oidc-issuer <issuer> \
    --oidc-authorization-endpoint <url> --oidc-token-endpoint <url> \
    --oidc-client-id <id> --oidc-client-secret-arn <secrets-manager-arn> \
    --oidc-redirect-uri <url> --session-cookie-kms-key-id <key-id> \
    --host 127.0.0.1
```

Then open `http://127.0.0.1:8420` in a browser. Your conversation's
runtime session ID is stored in the browser's `localStorage`, so reloading
the page continues the same conversation; use the **New conversation**
button to start over.

`--memory-id` (the `TravelAgentMemoryStack` Memory resource ID) enables the
**conversation history sidebar**: a list of your past conversations with a
preview of each, read live from AgentCore Memory (`ListSessions`/
`ListEvents`) rather than any local storage — click one to load its full
transcript and continue it. Hover a conversation for two icons: a pencil to
give it a custom name (stored as metadata on a dedicated marker event via
`CreateEvent`, since AgentCore Memory has no native session-title field —
this marker is excluded from the transcript and long-term memory
extraction), and a trash icon to permanently delete it (with a confirmation
prompt — AgentCore Memory has no session-level delete API either, so this
deletes every event in the session one at a time via `DeleteEvent`). Omit
`--memory-id` to run without the sidebar.

This requires whatever AWS credentials the process runs with (your local
credentials for local runs; the ECS task role for the hosted deployment)
to have `bedrock-agentcore:InvokeAgentRuntime`,
`bedrock-agentcore:ListSessions`, `bedrock-agentcore:ListEvents`,
`bedrock-agentcore:CreateEvent`, and `bedrock-agentcore:DeleteEvent`
permission on the relevant Runtime/Memory resources.

Scope/non-goals for the web UI:
- Live token-by-token streaming, with an optional diagnostic panel (off by
  default) showing every event the agent emits — reasoning, tool calls,
  full raw tool results, and the final answer — in a collapsible view.
- No structured itinerary rendering — agent responses are rendered as
  plain markdown (headings, bold/italic, lists) in the chat bubble.
- Conversation history is scoped per `actor_id` — no search or history
  across different people. Rename and delete are supported; deletion is
  permanent (there is no undo, and no confirmation beyond the browser's
  own confirm prompt).

You can also test the deployed Runtime directly from the **AgentCore
console's test chat**, bypassing the web UI (and its OIDC-derived
`actor_id`) entirely — useful for a quick sanity check that the Runtime
itself is healthy.

## Hosting the Web UI

`TravelAgentWebStack` deploys the web UI on ECS Fargate behind a plain,
internet-facing Application Load Balancer, for a shared, always-on
instance instead of everyone running `web/server.py` locally. See
`DESIGN.md` §2e (decisions #50-60) and `PLAN.md` Phase 12 for the full
design rationale.

How it authenticates users: `web/server.py` itself runs the entire OAuth
2.0 Authorization Code + PKCE flow against a dedicated Okta application —
there is no auth logic in the ALB at all. An unauthenticated top-level
page load is redirected straight to Okta; an unauthenticated `fetch()`/API
call gets a clean `401`. On successful login, the server issues a single,
KMS-envelope-encrypted session cookie carrying the Okta access/refresh
tokens, and derives that person's `actor_id` from the verified `sub`
claim — so every logged-in person gets their own conversation history and
long-term memory, without needing a `--actor-id` flag. If the access
token expires while a session is active, the server transparently
refreshes it inline using the stored refresh token — no login
interruption unless the refresh token itself has also expired.

`GET /api/whoami` returns the identity the server actually resolved for
the calling request (`{"sub": ..., "actor_id": ...}`), behind the same
auth as every other endpoint — useful for confirming who you're
authenticated as without digging through logs.

### Prerequisites for hosting

- **A domain you control**, already pointed (CNAME/A record) at the
  ALB's DNS name — DNS is entirely out of scope for this stack; it does
  not create a Route 53 record or use the ALB's raw generated DNS name
  for anything Okta-facing. Set this as `WEB_HOSTNAME` in `.env` — it
  must exactly match what you register as the Okta app's redirect URI
  below, or every login attempt fails with a `redirect_uri` mismatch.
- **An ACM certificate**, already issued and validated in `us-east-1` for
  `WEB_HOSTNAME` above, for the ALB's HTTPS listener. This project does
  not provision or import one for you.
- **A dedicated Okta OIDC web application** (confidential client, with a
  client secret), configured for the `openid offline_access` scopes and
  PKCE (S256). Configure the app to keep issuing the *same*, non-rotating
  refresh token on every refresh (not Okta's rotate-on-use alternative) —
  this project's silent-refresh logic assumes a fixed refresh token.
  Register `https://<WEB_HOSTNAME>/oauth2/callback` as an allowed
  redirect URI.

### Configure and deploy

```bash
cp .env.template .env
# fill in WEB_CERTIFICATE_ARN and the WEB_OIDC_* values
cd cdk
cdk deploy TravelAgentWebStack --require-approval never
```

`cdk/app.py` reads `WEB_CERTIFICATE_ARN`/`WEB_OIDC_*` from `.env` and only
constructs `TravelAgentWebStack` if they're all present — `cdk deploy
--all`/`cdk synth --all` work fine without this file at all if you only
want the four base stacks. The stack also provisions its own KMS key (for
session-cookie encryption) and a Secrets Manager secret (for the Okta
client secret) — no manual setup needed for either beyond providing the
Okta app's own client ID/secret in `.env`.

The stack provisions its own dedicated VPC (2 AZs, public + private
subnets, one NAT Gateway per AZ), an ECS cluster, a Fargate service (0.5
vCPU / 1GB per task, autoscaling 1–3 tasks on CPU utilization), and builds
the container image directly from `web/Dockerfile` as part of `cdk
deploy` (no separate manual Docker build/push step).

If you're deploying from an Apple Silicon (arm64) machine: the Fargate
task definition's `runtime_platform` is set to `ARM64` (see
`cdk/stacks/web_stack.py`) to match a native `arm64` Docker build.
Building for Fargate's `X86_64` default from an `arm64` dev machine
requires cross-architecture emulation, which was found in practice to
hang indefinitely pushing the built image to ECR — building/running
natively as `arm64` avoids that emulation path entirely. If you deploy
from an `x86_64` machine instead, change `runtime_platform` back to
`X86_64` in `web_stack.py`.

### Notes on this deployment

- **Not designed for high traffic** — sized for a small, known group of
  users (min 1 / max 3 tasks), not a general-purpose public service. Easy
  to raise the Fargate sizing/autoscaling limits in
  `cdk/stacks/web_stack.py` later if real usage calls for it.
- **The ALB idle timeout is 180s** (raised from the 60s default) — real
  itinerary-generation turns have taken up to ~56s in practice, and a
  long, tool-heavy turn could otherwise have its connection killed by the
  ALB mid-stream.
- **The ECS task role is scoped to exactly this deployment's Memory and
  Runtime ARNs** — no wildcard `bedrock-agentcore:*` permissions.

## Testing

Unit tests cover the two Lambda tool handlers (mocked, no live network or
AWS calls), the agent's response-extraction/session-parsing helpers, and
the web UI's `/api/chat` and `/api/conversations` endpoints (mocked agent
invocation, mocked AgentCore Memory client, and — for the OIDC login flow
specifically — a fake KMS client backing a real AES-GCM encrypt/decrypt
round trip, and mocked calls to Okta's token endpoint):

```bash
python -m pytest tests/ web/tests/
```

Agent behavior itself is validated manually (see `PLAN.md` Phase 5) rather
than with automated integration tests — this is a deliberate scope decision
for v1 (see `DESIGN.md` decision #18).

## Configuration

The agent reads its configuration from environment variables, set by
`RuntimeStack` from the other stacks' outputs:

| Variable | Source | Purpose |
|---|---|---|
| `GATEWAY_URL` | `GatewayStack` | MCP endpoint for tool discovery/invocation |
| `MEMORY_ID` | `MemoryStack` | AgentCore Memory resource ID |
| `AWS_REGION` | `RuntimeStack.region` | Region for the Memory client |
| `MODEL_ID` | `RuntimeStack.DEFAULT_MODEL_ID` | Bedrock model ID (Claude Sonnet) |

`MODEL_ID` and the namespace strings in `agent/agent.py` must stay in sync
with the corresponding constants in `cdk/stacks/runtime_stack.py` and
`cdk/stacks/memory_stack.py` respectively if either is changed.

This project's own `.env` (see "Hosting the Web UI" above) is unrelated to
the deployed agent's own configuration above — it only configures
`TravelAgentWebStack`'s ALB/OIDC setup, read directly by `cdk/app.py`, and
is never passed to the agent process itself.

## Observability

The Runtime gets a CloudWatch log group automatically (AgentCore's
default behavior for Runtime resources). The Gateway does **not** — by
default, a failing tool call surfaces to the end user only as a generic
"An internal error occurred. Please retry later.", with no visibility
into which target failed or why.

`TravelAgentGatewayStack` configures both CloudWatch application logs and
X-Ray traces for the Gateway:

- **Application logs**: `/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/travel-planning-agent-gateway`,
  stream `BedrockAgentCoreGateway_ApplicationLogs`. Each request/response
  is logged with `request_id`/`trace_id`/`span_id`, the target name, and
  (for errors) the actual upstream error message — e.g. a failing target
  logs both `"An error occurred while executing tool: <tool> from target
  <targetId>"` and the specific underlying failure on the next line.
- **Traces**: delivered to X-Ray (viewable via CloudWatch's GenAI
  Observability page, or the X-Ray console directly), correlated to the
  logs above via `trace_id`/`span_id`.

To debug a failing tool call, check the application log group first —
`aws logs get-log-events --log-group-name
/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/travel-planning-agent-gateway
--log-stream-name BedrockAgentCoreGateway_ApplicationLogs` (or `filter-log-events`
across a time range) — before assuming the problem is in this repo's own
code; several real failures have turned out to be inside AWS's own managed
connector backends, visible in these logs but not fixable from this
codebase.

## Known limitations

- No booking integrations, structured/JSON output, or automated integration
  tests — see `DESIGN.md`'s "Out of scope" section.
- `TravelAgentWebStack`'s hosted deployment is sized for a small, known
  group of users, not general-purpose public traffic — see "Hosting the
  Web UI" above.
