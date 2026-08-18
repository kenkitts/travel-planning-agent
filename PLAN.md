# Travel Planning Agent — Build Plan

See `DESIGN.md` for the full rationale behind each decision. This is the
execution plan, in dependency order.

## Repo Layout (target)

```
travel-planning-agent/
├── DESIGN.md
├── PLAN.md
├── README.md
├── cdk/
│   ├── app.py
│   ├── cdk.json
│   ├── requirements.txt
│   └── stacks/
│       ├── tools_stack.py        # Lambdas: weather, places
│       ├── gateway_stack.py      # AgentCore Gateway + 3 targets
│       ├── memory_stack.py       # AgentCore Memory (short+long term)
│       └── runtime_stack.py      # AgentCore Runtime hosting the agent
├── agent/
│   ├── agent.py                  # Strands Agent + BedrockAgentCoreApp entrypoint
│   ├── requirements.txt
│   └── prompts.py                # system prompt(s)
├── lambdas/
│   ├── weather/
│   │   ├── handler.py            # Open-Meteo wrapper
│   │   └── tool_schema.json
│   └── places/
│       ├── handler.py            # Amazon Location Service wrapper
│       └── tool_schema.json
├── tests/
│   ├── test_weather_handler.py
│   └── test_places_handler.py
└── cli/
    └── chat.py                   # local REPL client, calls invoke_agent_runtime
```

## Phase 0 — Project Scaffolding
- [ ] `git init`, `.gitignore` (Python/CDK/venv standard)
- [ ] Python virtualenv, `cdk init` in `cdk/`
- [ ] Confirm AWS credentials/target account (`us-east-1`), `cdk bootstrap`

## Phase 1 — Tool Lambdas (bottom-up, independently testable)
- [ ] `lambdas/weather/handler.py` — calls Open-Meteo (geocoding + forecast),
      returns normalized JSON (daily temp range, precip probability, conditions)
- [ ] `lambdas/places/handler.py` — wraps Amazon Location Service
      (`SearchPlaceIndexForText`/geocode + route/distance calls) for
      candidate-place lookup and day sequencing support
- [ ] Unit tests for both handlers (mock external calls / boto3 client) — per
      decision #18, unit tests only
- [ ] Define each Lambda's `tool_schema.json` (input/output schema for the
      Gateway Lambda target)

## Phase 2 — Infra: Gateway, Memory, Lambdas (CDK)
- [x] `tools_stack.py` — deploy the two Lambda functions + IAM roles
- [x] `gateway_stack.py` — create AgentCore Gateway; add:
      - Web Search managed connector target
      - Lambda target → weather handler
      - Lambda target → places handler
      - IAM role for Gateway (`GATEWAY_IAM_ROLE` credential provider)
- [x] `memory_stack.py` — create AgentCore `Memory` resource with a long-term
      strategy enabled (built-in preference-extraction strategy) plus
      short-term event storage
- [ ] `cdk deploy` all stacks; capture Gateway URL/ARN + Memory ID as outputs
      (deferred — requires a target AWS account; `cdk synth` validated the
      templates render correctly, see DESIGN.md decision log)

## Phase 3 — Agent Application
- [x] `agent/prompts.py` — system prompt: itinerary-builder persona,
      instructions to ask clarifying questions before generating, to call
      tools for grounding, and to output plain markdown itineraries
- [x] `agent/agent.py` — Strands `Agent` wired to:
      - `BedrockModel` (Claude Sonnet)
      - MCP client pointed at the Gateway URL (discovers Web Search/weather/places tools)
      - AgentCore Memory client (short-term: `create_event`/`get_last_k_turns`;
        long-term: `retrieve_memories` at session start, injected into context)
      - Wrapped in `BedrockAgentCoreApp` entrypoint for Runtime hosting
- [x] `runtime_stack.py` — CDK `CfnRuntime` (or L2 equivalent) deploying the
      agent container/code to AgentCore Runtime, IAM auth only (decision #15)

## Phase 4 — Client Tooling
- [ ] `cli/chat.py` — simple REPL: reads user input, calls
      `bedrock-agentcore` `invoke_agent_runtime` (boto3), streams/prints
      response, maintains session ID across turns
- [ ] Manual smoke test via AgentCore console test chat

## Phase 5 — End-to-End Validation (manual, per decision #18)
- [ ] Full conversation: vague request → clarifying Q&A → itinerary generated
- [ ] Verify itinerary reflects weather (e.g. request a rainy-season destination)
- [ ] Verify itinerary is geographically sequenced (not random order)
- [ ] Start a second session as the same user; verify a previously stated
      preference (e.g. "I like walking tours") surfaces without re-asking
- [ ] Verify IAM-unauthenticated calls are rejected

## Phase 6 — Wrap-up
- [ ] `README.md` — setup, deploy, and usage instructions
- [ ] Review AWS costs incurred (Bedrock invocations, Lambda, Location Service calls)
- [ ] Confirm all unit tests pass (`pytest`)

## Explicit Non-Goals (tracked, not built now)
- Booking/payment tool integrations
- Structured JSON output / frontend
- Cognito/JWT auth
- Automated integration tests
- Billing budgets/alarms
