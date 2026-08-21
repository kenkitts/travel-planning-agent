# Travel Planning Agent

A conversational travel-itinerary-building agent hosted on Amazon Bedrock
AgentCore. It asks clarifying questions, then builds a day-by-day itinerary
grounded in live web search, weather forecasts, and points-of-interest
lookups. It does not book anything — see `DESIGN.md` for the full set of
design decisions and rationale, and `PLAN.md` for the phased build plan.

## Architecture

```
CLI (cli/chat.py)  ──┐
                      │  boto3 bedrock-agentcore InvokeAgentRuntime (IAM/SigV4)
Web UI (web/server.py)┘  (via cli/agent_client.py, shared by both clients)
   │
   ▼
AgentCore Runtime  ── hosts ──▶  Strands Agent (Python, Claude Sonnet via Bedrock)
   │                                   │
   │                                   ├─▶ AgentCore Memory (short-term events +
   │                                   │    long-term traveler preferences /
   │                                   │    session summaries)
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
  login` or an equivalent credential process) — the CDK deploy and CLI both
  use the default credential chain.

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
python cli/chat.py --agent-runtime-arn <runtime-arn> --actor-id <your-name>
```

Type messages at the `you>` prompt; type `exit` or `quit` to leave. The CLI
keeps one runtime session ID for the whole conversation, so the agent's
short-term memory carries across turns. Long-term preferences (e.g. "I
always prefer walking tours") persist across separate CLI runs for the same
`--actor-id`.

You can also test directly from the **AgentCore console's test chat** for
the deployed Runtime.

## Web UI

A local, single-user web chat UI is available as an alternative to the CLI.
It runs entirely on your machine — no new AWS infrastructure — and uses your
existing local AWS credentials to call the same deployed Runtime.

```bash
pip install -r web/requirements.txt
python web/server.py --agent-runtime-arn <runtime-arn> --actor-id <your-name>
```

Then open `http://localhost:8420` in a browser. Your conversation's runtime
session ID is stored in the browser's `localStorage`, so reloading the page
continues the same conversation; use the **New conversation** button to
start over. Long-term memory is scoped by `--actor-id`, same as the CLI.

Scope/non-goals for the web UI (see `DESIGN.md` if you want to extend it):
- No login/auth — it's a local dev tool for one person, not multi-user or
  public-facing. Do not bind it to anything other than `127.0.0.1` (the
  default) or expose it beyond your machine — it holds your real AWS
  credentials.
- No token-by-token streaming — full responses only, matching the CLI.
- No structured itinerary rendering — agent responses are rendered as
  plain markdown (headings, bold/italic, lists) in the chat bubble.

## Testing

Unit tests cover the two Lambda tool handlers (mocked, no live network or
AWS calls), the agent's response-extraction/session-parsing helpers, and
the web UI's `/api/chat` endpoint (mocked agent invocation):

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

## Known limitations

- No booking integrations, structured/JSON output, or automated integration
  tests — see `DESIGN.md`'s "Out of scope" section.
- The web UI (see above) is local-only and single-user by design, not a
  general-purpose hosted frontend.
