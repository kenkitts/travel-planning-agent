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
│   ├── test_places_handler.py
│   └── test_agent.py              # agent.py helper functions (no AWS calls)
├── cli/
│   ├── agent_client.py            # shared: session ID + InvokeAgentRuntime call
│   └── chat.py                    # local REPL client
└── web/
    ├── server.py                  # local FastAPI backend, reuses agent_client
    ├── requirements.txt
    ├── static/                    # index.html, app.js, style.css
    └── tests/
        └── test_server.py
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
- [x] `README.md` — setup, deploy, and usage instructions
- [x] Review AWS costs incurred (Bedrock invocations, Lambda, Location Service calls)
      — checked Cost Explorer for account 800206160271: no dedicated
      Bedrock / Bedrock AgentCore / Location Service line items had posted
      yet as of this check (Cost Explorer has a ~24h+ reporting lag, and
      this session's usage happened same-day). Lambda cost so far is
      negligible (~$0.00004, a handful of test invocations). Actual
      Bedrock/AgentCore spend from this session's ~10 test conversations
      is expected to be a few cents at most (Claude Sonnet invocations on
      short prompts, no provisioned throughput). No cost-control action
      needed for a personal dev account at this usage level, but revisit
      Cost Explorer in 24-48h to confirm, and remember to `cdk destroy --all`
      when done experimenting to stop the AgentCore Gateway/Memory/Runtime
      resources (and their Lambda targets) from continuing to run.
- [x] Confirm all unit tests pass (`pytest`) — 33/33 passing after the Phase 5 fixes

### Post-Phase-6 fixes (undocumented at the time, recorded here retroactively)
1. **Gateway service role missing Web Search invoke permission**: the
   Gateway's service role had `lambda:InvokeFunction` for the weather/places
   Lambda targets (auto-granted by `add_lambda_target()`) but no permission
   at all to invoke the Web Search managed connector, so any web-search tool
   call failed. Fixed by granting `bedrock-agentcore:InvokeGateway` and
   `bedrock-agentcore:InvokeWebSearch` (on the literal `arn:...:tool/web-search.v1`
   resource, owned by the service under account `aws`) on the Gateway's
   service role — see `_grant_web_search_invoke()` in `gateway_stack.py`.
2. **All infra tagged** `{"auto-delete":"no"}` and `{"project":"travel-planning-agent"}`
   via `cdk.Tags.of(app).add(...)` in `cdk/app.py`, cascading to all 4 stacks.
3. **Long-term memory retrieval silently dropped by a relevance-score filter
   bug**: `bedrock-agentcore`'s `AgentCoreMemorySessionManager` filters
   retrieved memory records with `m.get("score", 0.0) >= relevance_score`,
   but real `retrieve_memory_records` results carry no `score` field at all
   — so any positive `relevance_score` (including the library's own default
   of 0.2) discarded every retrieved memory before it could be injected into
   the model's context, even though CloudWatch logs showed retrieval
   "succeeding" (that log line fires earlier and unconditionally, before the
   filter runs). Fixed by setting `relevance_score=0.0` explicitly for both
   retrieval namespaces in `agent/agent.py`'s `build_session_manager()`.
   Verified via Bedrock model invocation logging showing the actual
   `<user_context>` block present in the outbound request after the fix.

## Phase 7 — Web UI (local, single-user)
- [x] `cli/agent_client.py` — extracted `build_runtime_session_id()` and
      `invoke_agent()` out of `cli/chat.py` into a shared module, so
      `cli/chat.py` and `web/server.py` use one implementation of the
      `InvokeAgentRuntime` call instead of duplicating it.
- [x] `web/server.py` — FastAPI backend, local-only (binds `127.0.0.1` by
      default): `GET /` serves the chat UI, `GET /api/config` exposes the
      configured `--actor-id` to the frontend, `POST /api/chat` invokes the
      agent via `agent_client.invoke_agent()` and returns the plain-text
      response. No new AWS infrastructure — uses the caller's existing local
      AWS credentials, same as the CLI.
- [x] `web/static/{index.html,app.js,style.css}` — chat UI: message list,
      input box, "New conversation" button. Session ID (and message history,
      for redisplay on reload) persisted in the browser's `localStorage` so
      a page reload continues the same conversation, unlike the CLI which
      always starts a fresh session. A small hand-rolled markdown-to-HTML
      renderer (headings, bold/italic, lists, paragraphs) is used instead of
      a vendored library, since the agent's output shape is simple and known
      (see `agent/prompts.py`'s "Writing the itinerary" section) — output is
      HTML-escaped before any markdown transformation is applied, so agent
      text can never inject raw HTML/script into the page.
- [x] `web/requirements.txt` — `fastapi`, `uvicorn`, `pydantic`, `boto3`,
      pinned to versions already resolved in this project's venv.
- [x] `web/tests/test_server.py` — 7 tests against `/api/chat`, `/api/config`,
      and `/` using FastAPI's `TestClient`, mocking `invoke_agent()` (no real
      AWS calls).
- [x] **Real bug found and fixed during end-to-end verification**: the
      Runtime's `invoke()` entrypoint returned `str(result.message)` — the
      Python `repr()` of the full Strands result message dict, including a
      `reasoningContent` block (Claude's extended-thinking signature blob),
      not the plain assistant reply text. This was live in the deployed
      agent already (the CLI printed the same raw dict, it just blended in
      less obviously in a terminal than in a rendered chat bubble would
      have). Fixed by adding `extract_response_text()` to `agent/agent.py`,
      which concatenates only the `text` content blocks, and using it at
      both `invoke()` return sites. Added `tests/test_agent.py` (8 tests) for
      this and `parse_runtime_session_id()`. Verified live: before the fix,
      `/api/chat` returned the raw dict repr string; after redeploying
      `TravelAgentRuntimeStack`, the same request/session returned clean
      plain text, confirmed via curl against the running local server, and a
      full itinerary-generation turn (weather + web search + places tools)
      was independently re-verified to render correctly through the
      frontend's markdown renderer (checked with a standalone Node.js
      harness against the real API response).
- [x] End-to-end verified against the live deployed Runtime ARN: `/api/config`,
      `GET /`, a no-memory "What do you know about me?" turn, a multi-turn
      clarifying-question exchange, and a full tool-grounded itinerary
      generation (Portland, OR) — all returned clean, correctly-rendered
      responses.
- [x] `README.md` updated with a "Web UI" section (setup, run, scope/non-goals)
      and the stale Phase-5 "Known limitations" note (about memory not
      surfacing) removed, since that was fixed by the Phase-6-adjacent fix
      above.
- [x] Full repo test suite passing: 48/48 (`tests/` + `web/tests/`).

### Post-Phase-7 fix: MaxTokensReachedException surfaced as an opaque 500
Reported by the user via the web UI: `InvokeAgentRuntime` returned
`RuntimeClientError` / HTTP 500 ("Received error (500) from runtime.").
CloudWatch runtime logs showed the real cause:
`strands.types.exceptions.MaxTokensReachedException: Model stopped
generating due to maximum token limit` — a real request (3-day luxury
Diablo Lake / North Cascades itinerary, dog-friendly, 13 tool calls before
the final answer) ran the model out of its Bedrock Converse `maxTokens`
budget mid-response. `BedrockModel` only sets `maxTokens` on the request if
`max_tokens` is explicitly configured — `agent/agent.py` never set it, so
Bedrock fell back to its own default limit, which wasn't enough for a long,
richly-detailed, tool-grounded itinerary. The exception propagated
uncaught out of `invoke()`, which AgentCore Runtime reports to callers as a
generic 500.

Two fixes in `agent/agent.py`:
1. `BedrockModel(..., max_tokens=MAX_OUTPUT_TOKENS)` with
   `MAX_OUTPUT_TOKENS = 8192` (overridable via `MAX_OUTPUT_TOKENS` env var),
   giving long itineraries realistic headroom.
2. Added `run_agent_turn()`, wrapping `agent(user_message)`: if
   `MaxTokensReachedException` is still raised (a response longer than even
   8192 tokens), Strands has already appended the partial assistant message
   to `agent.messages` — confirmed by reading `strands.event_loop.event_loop`
   and `_recover_message_on_max_tokens_reached` source directly rather than
   assuming — so the partial text is extracted and returned with a
   "cut off, ask me to continue" note instead of failing the whole request.

Added 3 new tests to `tests/test_agent.py` (`RunAgentTurnTests`, using a
`MagicMock` fake agent to deterministically exercise the success and
max-tokens paths without needing a real model call). Full suite: 51/51
passing. Verified live: redeployed `TravelAgentRuntimeStack`, then
re-sent the exact same reproduction prompt (3-day Diablo Lake/Bella
itinerary) through the running local web server — returned a complete
4504-character itinerary with a natural ending, HTTP 200, and CloudWatch
confirmed `"Invocation completed successfully (56.617s)"` with no exception.

### Post-Phase-7 feature: conversation history sidebar
Requested by the user: a way to revisit and continue past conversations
from the web UI, rather than only ever seeing the current session (the
existing `localStorage`-cached message list was lost on a different device
or browser, and there was no way to browse other sessions at all).

Implemented with **no new storage** — AgentCore Memory is already the
system of record for every session's raw turn history (short-term events,
90-day retention per `memory_stack.py`), so the feature is purely two new
read-only endpoints on top of the existing `ListSessions`/`ListEvents` data
plane APIs:
- `GET /api/conversations` — lists the current `--actor-id`'s sessions
  (via `ListSessions`), with a preview built from each session's first user
  message (via one `ListEvents` call per session), sorted newest-first.
  Sessions with zero events (a `sessionId` AgentCore assigned but never
  used) are skipped.
- `GET /api/conversations/{session_id}` — the full chronological transcript
  for one session (via `ListEvents`, reversed since the API returns
  newest-first).

Both are gated on a new `--memory-id` server CLI arg; omitting it disables
the sidebar entirely (`/api/config` reports `history_enabled: false`) so
the feature degrades cleanly for anyone who doesn't pass it. This is a
**new IAM permission requirement** for local callers: `InvokeAgentRuntime`
(already required for chat) does not cover `ListSessions`/`ListEvents` —
those are separate actions on the Memory resource, granted server-side to
the Runtime's execution role by `memory.grant_full_access()` in
`runtime_stack.py`, but not automatically to whoever runs `web/server.py`
locally with their own credentials.

Frontend: `static/index.html`/`app.js`/`style.css` gained a sidebar
(`#sidebar`) listing conversations, click-to-switch (fetches that session's
transcript and swaps the active `runtimeSessionId`), an active-conversation
highlight, and an overlay/toggle behavior under 720px width for mobile. The
old `localStorage`-cached message-list hack (`HISTORY_STORAGE_KEY`) was
removed entirely — AgentCore Memory is now the single source of truth for
transcripts, so a page reload re-fetches the current session's transcript
from the server instead of replaying a local cache that could drift out of
sync with what the agent actually has in Memory.

Added `ConversationHistoryEndpointTests` (9 tests) to
`web/tests/test_server.py`, covering the list/get endpoints, preview
truncation, newest-first-to-chronological ordering, empty-session
filtering, the disabled-sidebar 404 path, and upstream-error handling. Full
suite: 60/60 passing.

### Post-Phase-7 feature: custom conversation titles/rename
Requested by the user: a way to tell conversations in the sidebar apart
beyond the auto-generated first-message preview (e.g. two separate
sessions both starting "Hi" are indistinguishable).

AgentCore Memory has no native session-title field — `ListSessions` only
ever returns `{sessionId, actorId, createdAt}`, confirmed against the real
API. The chosen approach: a title is a small marker `CreateEvent` whose
**event-level `metadata`** field (a real, separate feature from the
conversational payload — up to 15 key-value pairs) carries
`{"conversationTitle": {"stringValue": "..."}}`, with `extractionMode:
"SKIP"` so it's never fed into long-term memory extraction (it's a UI
label, not something the traveler said). `list_conversations` scans a
session's events newest-first and prefers the latest marker's title over
the auto-generated preview; `_event_turns` excludes marker events from
transcripts entirely, so a rename can never appear as a fake chat turn.

New endpoint: `PUT /api/conversations/{session_id}/title` — normalizes
whitespace, truncates to 80 chars with an ellipsis, writes the marker
event, and returns the (possibly-truncated) title actually stored.

Frontend: each sidebar item gained a pencil icon (visible on hover, or
always visible under the 720px mobile breakpoint) that swaps the label
into an inline text input; Enter/blur commits via the new endpoint,
Escape cancels.

**Two real bugs found and fixed via live testing** against a real
deployed Runtime + Memory resource (the initial mocked unit tests, using
`MagicMock()` for the boto3 client, did not catch either):
1. The raw boto3 `bedrock-agentcore` `CreateEvent` call requires
   `eventTimestamp` explicitly — omitting it isn't defaulted the way
   `bedrock_agentcore.memory.client.MemoryClient.create_event()` defaults
   it internally (this server calls the plain boto3 client directly, not
   that wrapper), and raised `botocore.exceptions.ParamValidationError`.
2. `ParamValidationError` is a `BotoCoreError` subclass, not a
   `ClientError` — the endpoint's original `except ClientError` block
   didn't catch it, so the failure surfaced as a raw, unhandled 500
   instead of a clean `502`. Fixed by also catching `BotoCoreError`.

Verified live end-to-end: created a real conversation against the
deployed Runtime, set a title, confirmed it appeared in
`GET /api/conversations` in place of the preview and was correctly
excluded from `GET /api/conversations/{id}`'s transcript, then renamed it
a second time and confirmed the newer title won.

Added 10 more tests to `web/tests/test_server.py` (title preferred over
preview, title `None` when never renamed, latest-of-two-renames wins,
marker excluded from transcript, successful `PUT` with correct
metadata/`extractionMode`/bare session ID, whitespace normalization,
empty-title rejection, long-title truncation, 404 when `--memory-id`
unset, 502 on `ClientError`). Full suite: 74/74 passing.

## Explicit Non-Goals (tracked, not built now)
- Booking/payment tool integrations
- Structured JSON output / frontend
- Cognito/JWT auth
- Automated integration tests
- Billing budgets/alarms
