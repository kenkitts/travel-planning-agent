# Travel Planning Agent

A conversational travel-itinerary-building agent hosted on Amazon Bedrock
AgentCore. It asks clarifying questions, then builds a day-by-day itinerary
grounded in live web search, weather forecasts, and points-of-interest
lookups. It does not book anything — see `DESIGN.md` for the full set of
design decisions and rationale, and `PLAN.md` for the phased build plan.

## Architecture

```
CLI (cli/chat.py) ──────────────┐
                                 │  IAM/SigV4 InvokeAgentRuntime
Web UI (web/server.py), hosted ─┘  (via cli/agent_client.py, shared by both clients)
on ECS Fargate behind an OIDC-       actor_id passed explicitly in the payload
authenticated ALB
   │
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
- `TravelAgentWebStack` *(optional)* — hosts the web UI on ECS Fargate
  behind an OIDC-authenticated ALB, for a shared/always-on deployment
  instead of running `web/server.py` on your own machine. See "Hosting
  the Web UI" below.

## Repo layout

```
travel-planning-agent/
├── DESIGN.md              # design decisions + rationale
├── PLAN.md                # phased build plan, with completion notes
├── agent/                 # Strands agent + BedrockAgentCoreApp entrypoint
├── cdk/                   # CDK app (Python) — 5 stacks, see cdk/stacks/
├── lambdas/                # weather + places tool Lambdas, with tests
├── tests/                  # unit tests for the Lambda handlers + agent helpers
├── cli/                    # local REPL client (chat.py) + shared agent_client.py
└── web/                    # web UI (server.py + static/ + Dockerfile), see below
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
  login` or an equivalent credential process) — used both for `cdk deploy`
  itself and, at runtime, for every InvokeAgentRuntime call the CLI or web
  UI makes (see "Authentication" below).

## Authentication

The CLI and web UI both authenticate to the deployed Runtime with plain
AWS IAM credentials (SigV4-signed `InvokeAgentRuntime` calls via boto3) —
there is no separate identity provider or bearer-token flow for the
Runtime itself. Long-term AgentCore Memory (preferences, conversation
history) is scoped by an explicit `actor_id` that the caller supplies:

- **CLI**: pass `--actor-id <your-id>` — any stable string you choose
  (e.g. your name or username). If multiple people share the CLI, each
  should use a different value.
- **Web UI**: if you're running `web/server.py` directly on your own
  machine (see "Web UI" below), there's no login of its own either — it's
  a single-user local tool, same as the CLI. If it's deployed via
  `TravelAgentWebStack` (see "Hosting the Web UI" below), the ALB in
  front of it handles real per-user login via OIDC, and `actor_id` is
  derived automatically from each logged-in person's verified identity —
  no flag needed in that case.

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
them, `cdk deploy --all`/`cdk synth --all` simply skip it.

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

## Usage

Get the deployed Runtime's ARN from the `TravelAgentRuntimeStack` stack
(e.g. via `aws bedrock-agentcore-control list-agent-runtimes` or the
CloudFormation console), then start a chat session:

```bash
pip install -r cli/requirements.txt
python cli/chat.py --agent-runtime-arn <runtime-arn> --actor-id <your-id>
```

Type messages at the `you>` prompt; type `exit` or `quit` to leave. The CLI
keeps one runtime session ID for the whole conversation, so the agent's
short-term memory carries across turns. Long-term preferences (e.g. "I
always prefer walking tours") persist across separate CLI runs that use
the same `--actor-id`.

You can also test directly from the **AgentCore console's test chat** for
the deployed Runtime.

## Web UI

A single-user web chat UI is available as an alternative to the CLI, and
can be run either locally on your own machine or hosted on AWS (see
"Hosting the Web UI" below) for a shared, always-on deployment.

To run it locally:

```bash
pip install -r web/requirements.txt
python web/server.py --agent-runtime-arn <runtime-arn> --alb-arn <alb-arn> \
    --memory-id <memory-id> --host 127.0.0.1
```

`--alb-arn` is only meaningful for the hosted deployment (it's used to
verify the ALB's signed OIDC claims header — see "Hosting the Web UI"
below); running locally without an ALB in front of it means every request
will fail actor identification, so local, non-hosted use of `web/server.py`
is now primarily a development/testing aid for the hosted deployment
rather than a supported end-user mode on its own. If you want a
single-user local tool without standing up any AWS infrastructure, the
CLI (above) is the better fit.

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
  full raw tool results, and the final answer — in a collapsible view. The
  CLI does not stream visibly; it consumes the same event stream
  internally and prints the final reply, matching its original UX.
- No structured itinerary rendering — agent responses are rendered as
  plain markdown (headings, bold/italic, lists) in the chat bubble.
- Conversation history is scoped per `actor_id` — no search or history
  across different people. Rename and delete are supported; deletion is
  permanent (there is no undo, and no confirmation beyond the browser's
  own confirm prompt).

## Hosting the Web UI

`TravelAgentWebStack` deploys the web UI on ECS Fargate behind an
internet-facing, OIDC-authenticated Application Load Balancer, for a
shared, always-on instance instead of everyone running `web/server.py`
locally. See `DESIGN.md` decision #37 and `PLAN.md` Phase 10 for the full
design rationale.

How it authenticates users: the ALB's `authenticate-oidc` listener rule
handles the entire login flow against Okta before a request ever reaches
the container — there is no login page inside the app itself. The ALB
forwards each authenticated user's verified identity as a signed
`x-amzn-oidc-data` header; the container verifies that signature itself
(ES256, against ALB's own public-keys endpoint) before trusting it, and
derives that person's `actor_id` from it — so every logged-in person gets
their own conversation history and long-term memory, without needing a
`--actor-id` flag.

### Prerequisites for hosting

- **An ACM certificate**, already issued and validated in `us-east-1`, for
  the ALB's HTTPS listener. This project does not provision or import one
  for you.
- **A dedicated Okta OIDC web application** (confidential client, with a
  client secret) registered for the ALB — this is a *different* Okta
  app/config than anything the CLI uses (the CLI has no ALB in front of
  it and authenticates to AWS directly with IAM credentials instead).
  Configure the ALB's own `https://<alb-dns-name>/oauth2/idpresponse` as
  an allowed redirect URI once you know the ALB's DNS name (available
  after the first deploy, from the stack's `AlbDnsName` output — you may
  need to deploy once, update the Okta app's redirect URI, then continue).
- **DNS is out of scope for this stack.** Point your own domain at the
  ALB's DNS name (from the `AlbDnsName` stack output) yourself if you
  want a friendlier URL than the raw ALB hostname — this project does not
  create a Route 53 record for you.

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
want the four base stacks.

The stack provisions its own dedicated VPC (2 AZs, public + private
subnets, one NAT Gateway per AZ), an ECS cluster, a Fargate service (0.5
vCPU / 1GB per task, autoscaling 1–3 tasks on CPU utilization), and builds
the container image directly from `web/Dockerfile` as part of `cdk
deploy` (no separate manual Docker build/push step).

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
invocation, mocked AgentCore Memory client, and — for the OIDC header
verification specifically — a real EC keypair used to sign test tokens the
same way the ALB would):

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
