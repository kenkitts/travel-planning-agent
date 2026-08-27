# Travel Planning Agent

A conversational travel-itinerary-building agent hosted on Amazon Bedrock
AgentCore. It asks clarifying questions, then builds a day-by-day itinerary
grounded in live web search, weather forecasts, and points-of-interest
lookups. It does not book anything — see `DESIGN.md` for the full set of
design decisions and rationale, and `PLAN.md` for the phased build plan.

## Architecture

```
CLI (cli/chat.py)  ──┐
                      │  HTTPS InvokeAgentRuntime with an Okta JWT bearer token
Web UI (web/server.py)┘  (via cli/agent_client.py, shared by both clients)
   │
   ▼
AgentCore Runtime (Okta JWT authorizer)  ── hosts ──▶  Strands Agent (Python, Claude Sonnet via Bedrock)
   │                                   │
   │                                   ├─▶ AgentCore Memory (short-term events +
   │                                   │    long-term traveler preferences /
   │                                   │    session summaries, scoped by the
   │                                   │    caller's Okta `sub` claim)
   │                                   │
   │                                   └─▶ AgentCore Gateway (MCP, IAM auth)
   │                                          ├─ Web Search (managed connector)
   │                                          ├─ Weather Lambda (Open-Meteo)
   │                                          └─ Places Lambda (Amazon Location Service)
```

Four CDK stacks (deployed in this order):
- `TravelAgentToolsStack` — the weather and places Lambda functions
- `TravelAgentGatewayStack` — the AgentCore Gateway and its three targets
- `TravelAgentMemoryStack` — the AgentCore Memory resource
- `TravelAgentRuntimeStack` — the AgentCore Runtime hosting the agent

The web UI (`web/`) is **not** a fifth AWS stack — it's a local-only backend
that runs on your machine and calls the deployed Runtime the same way the
CLI does. See "Web UI" below.

## Repo layout

```
travel-planning-agent/
├── DESIGN.md              # design decisions + rationale
├── PLAN.md                # phased build plan, with completion notes
├── agent/                 # Strands agent + BedrockAgentCoreApp entrypoint
├── cdk/                   # CDK app (Python) — 4 stacks, see cdk/stacks/
├── lambdas/                # weather + places tool Lambdas, with tests
├── tests/                  # unit tests for the Lambda handlers + agent helpers
├── cli/                    # local REPL client (chat.py) + shared agent_client.py
└── web/                    # local web UI (server.py + static/), see below
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
  login` or an equivalent credential process) — the CDK deploy itself uses
  the default credential chain (deploying infrastructure is still an
  AWS-credentialed operation; only the deployed agent's own inbound calls
  use Okta — see "Authentication" below).
- An Okta org with a **dedicated OIDC application** registered for this
  project (native/public client, PKCE required, `offline_access` scope
  enabled — see "Authentication" below for the exact steps), and the
  [`okta-claude-code-token-helper`](https://github.com/) script (or your
  own clone of it) available locally to acquire tokens.

## Authentication

The CLI and web UI authenticate to the deployed Runtime with an Okta-issued
JWT bearer token — **not** AWS IAM credentials. This is a full cutover:
once the Runtime's authorizer is configured for JWT, IAM/SigV4 calls to
`InvokeAgentRuntime` no longer work for any caller (see `DESIGN.md` §2a for
the full rationale).

### 1. Register a dedicated Okta application

In your Okta Admin Console, create a new application (separate from any
other Okta app you may already use for unrelated tools):

- **Application type**: Native Application (or "SPA" if your Okta version
  models loopback-redirect CLI tools that way) — it must be a **public
  client with no client secret**.
- **Grant type**: Authorization Code, with **PKCE required/enabled**.
- **Sign-in redirect URI**: `http://localhost:5309/callback` (or whatever
  port you configure via `OKTA_REDIRECT_PORT` below).
- **Scopes**: `groups` and **`offline_access`** (required for refresh
  tokens — without it you'd be forced through a full browser login every
  time your access token expires, typically hourly).

### 2. Configure this project's `.env`

```bash
cp .env.template .env
# edit .env with your Okta org's issuer URL and this app's client ID
```

```bash
OKTA_ISSUER=https://your-org.okta.com/oauth2/default
OKTA_CLIENT_ID=0oaXXXXXXXXXXXXXXXXX
OKTA_SCOPES=groups offline_access
OKTA_REDIRECT_PORT=5309
OKTA_TOKEN_HELPER_PATH=~/okta-claude-code-token-helper/okta-claude-code-token.py
```

`OKTA_TOKEN_HELPER_PATH` should point at your local clone of the token
helper script. Both `cdk deploy` (to configure the Runtime's JWT
authorizer) and the CLI/web UI (to acquire tokens) read this same `.env`.

### 3. Log in once, manually

The CLI/web UI both shell out to the token helper script per request, but
the very first login needs a real interactive terminal to open a browser:

```bash
python3 ~/okta-claude-code-token-helper/okta-claude-code-token.py
```

This opens your browser, walks you through Okta login, and caches the
resulting token (and a refresh token) at
`~/.cached-credentials/token-cache.json`. Subsequent CLI/web UI turns reuse
and silently refresh this cached token — no browser popup needed again
until the refresh token itself expires or is revoked.

Long-term memory (preferences, conversation history) is scoped to your
Okta identity's `sub` claim, derived server-side from your verified token —
there's no `--actor-id` flag to set; whoever's Okta account is logged in is
who the agent remembers.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r cdk/requirements.txt
pip install -r tests/requirements.txt
```

## Deploy

From the `cdk/` directory, with your venv activated, AWS credentials
active, and `.env` configured (see "Authentication" above — `cdk deploy`
reads `OKTA_ISSUER`/`OKTA_CLIENT_ID` from it to configure the Runtime's JWT
authorizer):

```bash
cd cdk
cdk bootstrap   # once per account/region, if not already done
cdk deploy --all --require-approval never
```

This deploys all 4 stacks in dependency order. Deploying `TravelAgentRuntimeStack`
also bundles `agent/` (pip-installs `agent/requirements.txt` into the asset)
— this step can take a minute or two the first time.

To update just the agent after a code change:

```bash
cdk deploy TravelAgentRuntimeStack --require-approval never
```

### Tear down

```bash
cdk destroy --all
```

This deletes all 4 stacks and their resources (Lambdas, Gateway, Memory,
Runtime, IAM roles). It does **not** delete the CDK bootstrap stack
(`CDKToolkit`) or its S3 asset bucket.

## Usage

Get the deployed Runtime's ARN from the `TravelAgentRuntimeStack` stack
(e.g. via `aws bedrock-agentcore-control list-agent-runtimes` or the
CloudFormation console), then start a chat session:

```bash
pip install -r cli/requirements.txt
python cli/chat.py --agent-runtime-arn <runtime-arn>
```

Type messages at the `you>` prompt; type `exit` or `quit` to leave. The CLI
keeps one runtime session ID for the whole conversation, so the agent's
short-term memory carries across turns. Long-term preferences (e.g. "I
always prefer walking tours") persist across separate CLI runs for the
same Okta identity (see "Authentication" above).

You can also test directly from the **AgentCore console's test chat** for
the deployed Runtime — note that the console's test chat uses its own auth
path and is unaffected by the Okta cutover described above.

## Web UI

A local, single-user web chat UI is available as an alternative to the CLI.
It runs entirely on your machine — no new AWS infrastructure — and uses
the same Okta login as the CLI to call the deployed Runtime.

```bash
pip install -r web/requirements.txt
python web/server.py --agent-runtime-arn <runtime-arn> --memory-id <memory-id>
```

Then open `http://localhost:8420` in a browser. Your conversation's runtime
session ID is stored in the browser's `localStorage`, so reloading the page
continues the same conversation; use the **New conversation** button to
start over. Long-term memory is scoped to your Okta identity (`sub`
claim), same as the CLI — each person who wants to use this runs their own
local copy of the web UI and logs into their own Okta account (see
"Authentication" above); this is still a local-only, single-user-per-process
tool, not a hosted multi-user frontend.

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

This still requires your local AWS credentials (separate from your Okta
login, which authenticates chat itself) to have
`bedrock-agentcore:ListSessions`, `bedrock-agentcore:ListEvents`,
`bedrock-agentcore:CreateEvent`, and `bedrock-agentcore:DeleteEvent`
permission on the Memory resource — Memory access is IAM-authorized
independently of the Runtime's JWT authorizer (see `DESIGN.md` decision
#34).

Scope/non-goals for the web UI (see `DESIGN.md` if you want to extend it):
- No hosted/multi-user frontend — it's a local dev tool that a small,
  known group of people can each run on their own machine (see `DESIGN.md`
  decisions #28/#30), not a general-purpose hosted frontend reachable over
  a network. Do not bind it to anything other than `127.0.0.1` (the
  default) or expose it beyond your machine.
- Live token-by-token streaming, with an optional diagnostic panel (off by
  default) showing every event the agent emits — reasoning, tool calls,
  full raw tool results, and the final answer — in a collapsible view. The
  CLI does not stream visibly; it consumes the same event stream
  internally and prints the final reply, matching its original UX.
- No structured itinerary rendering — agent responses are rendered as
  plain markdown (headings, bold/italic, lists) in the chat bubble.
- Conversation history is scoped to the current Okta identity — no search
  or history across different people. Rename and delete are supported;
  deletion is permanent (there is no undo, and no confirmation beyond the
  browser's own confirm prompt).

## Testing

Unit tests cover the two Lambda tool handlers (mocked, no live network or
AWS calls), the agent's response-extraction/session-parsing helpers, and
the web UI's `/api/chat` and `/api/conversations` endpoints (mocked agent
invocation and mocked AgentCore Memory client):

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

This is separate from the project's own `.env` (see "Authentication"
above), which configures the Okta app used by `cdk deploy` (to set up the
Runtime's JWT authorizer) and by the CLI/web UI clients (to acquire
tokens) — `.env` is read by `cli/agent_client.py`/`cdk/app.py` directly,
not passed to the deployed agent process itself.

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
- The web UI (see above) is local-only and single-user by design, not a
  general-purpose hosted frontend.
