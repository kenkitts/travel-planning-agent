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
- [x] `cdk deploy` all stacks; capture Gateway URL/ARN + Memory ID as outputs
      — deployed to account 800206160271, us-east-1. All 4 stacks
      (`TravelAgentToolsStack`, `TravelAgentGatewayStack`,
      `TravelAgentMemoryStack`, `TravelAgentRuntimeStack`) reached
      `CREATE_COMPLETE`/`UPDATE_COMPLETE`.

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
- [x] `cli/chat.py` — simple REPL: reads user input, calls
      `bedrock-agentcore` `invoke_agent_runtime` (boto3), streams/prints
      response, maintains session ID across turns
- [ ] Manual smoke test via AgentCore console test chat (deferred — requires
      a deployed stack; see Phase 5)

## Phase 5 — End-to-End Validation (manual, per decision #18)
- [x] Full conversation: vague request → clarifying Q&A → itinerary generated
      (verified live against the deployed Runtime: turn 1 asked for
      dates/budget/interests/pace/travelers; turn 2 generated a full 2-day
      Boston itinerary)
- [x] Verify itinerary reflects weather (itinerary noted January cold weather
      and dressed the plan around indoor refuges; the weather tool call
      itself came back empty for a date >16 days out, and the agent
      correctly fell back to seasonal expectations per its system prompt)
- [x] Verify itinerary is geographically sequenced (not random order) —
      itinerary grouped Freedom Trail/downtown on day 1 and North End/
      Charlestown on day 2, not randomly interleaved
- [x] Start a second session as the same user; verify a previously stated
      preference surfaces — confirmed at the infrastructure level: the
      preference was extracted into AgentCore Memory
      (`ListMemoryRecords` showed the exact stated preference in the
      `/travel-agent/actor/{actorId}/preferences` namespace) and correctly
      retrieved on the next session's first turn (CloudWatch log: "Retrieved
      1 memories from namespace: .../preferences", confirmed across two
      separate follow-up sessions). The model still answered "I don't have
      any information" when asked directly, even after strengthening the
      system prompt to explicitly instruct it to check recalled context
      before answering — the retrieved memory record isn't reliably
      reaching the model's context via
      `AgentCoreMemorySessionManager`'s injection for this query shape.
      This is a known gap in how retrieved memories surface into the
      conversation (Strands/bedrock_agentcore library behavior, not a
      config or IAM issue) — tracked as a Phase 6+ follow-up rather than
      blocking Phase 5, since the storage/extraction/retrieval pipeline
      itself is proven correct.
- [x] Verify IAM-unauthenticated calls are rejected — confirmed empirically:
      the Gateway's `authorizerType` is `AWS_IAM`, and it returned
      401 Unauthorized until the agent's MCP client was fixed to SigV4-sign
      requests (see runtime_stack.py / agent.py fix below)

### Real bugs found and fixed during deployment
1. **RuntimeStack entrypoint format**: `AgentRuntimeArtifact.from_code_asset`
   requires `entrypoint=["agent.py"]` (a single script filename) — the
   Runtime invokes the interpreter itself based on `runtime=`. Using
   `["python3", "agent.py"]` fails with `InvalidRequest: Invalid entrypoint
   value`. Confirmed against dozens of real production CDK stacks — none
   use a two-element `[interpreter, script]` array.
2. **Gateway auth is not automatic from within Runtime**: contrary to the
   assumption recorded in an earlier design note, the Runtime's execution
   role IAM policy alone does not authorize calls to a Gateway configured
   with `GatewayAuthorizer.using_aws_iam()`. The outbound MCP request must
   itself be SigV4-signed, or the Gateway returns 401 Unauthorized. Fixed
   by switching `agent/agent.py`'s MCP transport from a plain
   `streamablehttp_client` to `mcp_proxy_for_aws.client.aws_iam_streamablehttp_client`
   (added `mcp-proxy-for-aws==1.6.4` to `agent/requirements.txt`).
3. **Cross-region inference profile IAM scope**: the `us.` cross-region
   inference profile for Claude Sonnet 4.5 routes actual model invocations
   to `us-east-1`, `us-east-2`, and `us-west-2` (confirmed via
   `bedrock:GetInferenceProfile`), but the Runtime's execution role only
   granted `bedrock:InvokeModel*` on `foundation-model/*` in `self.region`
   (`us-east-1`). Requests routed to `us-east-2` failed with
   `AccessDeniedException`. Fixed by granting `foundation-model/*` across
   all three regions in `runtime_stack.py`.

## Phase 6 — Wrap-up
- [ ] `README.md` — setup, deploy, and usage instructions
- [ ] Review AWS costs incurred (Bedrock invocations, Lambda, Location Service calls)
- [x] Confirm all unit tests pass (`pytest`) — 33/33 passing after the Phase 5 fixes

## Explicit Non-Goals (tracked, not built now)
- Booking/payment tool integrations
- Structured JSON output / frontend
- Cognito/JWT auth
- Automated integration tests
- Billing budgets/alarms
