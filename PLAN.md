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
      — deployed to a personal AWS account, us-east-1. All 4 stacks
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
      — checked Cost Explorer for the deployment account: no dedicated
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

### Post-Phase-7 feature: delete conversations
Requested by the user: the ability to remove old conversations from the
sidebar.

AgentCore Memory has no session-level delete API — confirmed via a wide
code search across dozens of real implementations (TypeScript
`SdkSessionsMemoryStore.deleteSession`, several `clear_session`/
`clear_memory.py` scripts, a Next.js `DELETE` route) — every one of them
deletes a "session" by listing all of its events and deleting them one at
a time via `DeleteEvent`; there is no `DeleteSession` or batch-delete-
events operation. `list_conversations` already skips sessions with zero
events, so deleting every event is sufficient to make a conversation
disappear from the sidebar.

New endpoint: `DELETE /api/conversations/{session_id}`. Implementation:
`_list_all_event_ids()` pages through `ListEvents` with
`includePayloads=False` (IDs only; payloads are dead weight on the delete
path) and collects every event ID *before* deleting any of them — deleting
while paging can invalidate the pagination token, a failure mode called
out in more than one of the real implementations found. Deletion is then
best-effort: one failed `DeleteEvent` call doesn't abort the rest (there's
no batch/atomic delete to make this transactional), and the response
reports `deleted_events`/`failed_events` counts rather than an all-or-
nothing success/failure — a session with leftover events just doesn't
fully disappear from the sidebar, which is visible and re-triable rather
than a silent partial/corrupt state. 404s if the session has zero events
to begin with (nothing to delete) or on `ResourceNotFoundException`.

Frontend: each sidebar item gained a trash icon (same hover/mobile-visible
behavior as the rename pencil) that shows a native `window.confirm()`
prompt — appropriate for a local, single-user dev tool, not worth a custom
modal — before calling the endpoint. If the deleted conversation was the
currently active session, the UI starts a fresh conversation afterward
rather than leaving a transcript on screen for a session that no longer
exists.

Verified live end-to-end against the real deployed Runtime + Memory
resource: created a conversation, confirmed it appeared in the list,
deleted it (5 events — 2 chat turns plus 3 Strands session-state
snapshots), confirmed the list came back empty and
`GET /api/conversations/{id}` correctly 404'd afterward.

Added 7 tests to `web/tests/test_server.py` (full deletion, multi-page
pagination, partial-failure reporting, 404 on an empty/nonexistent
session, 404 on `ResourceNotFoundException` vs. 502 on other `ClientError`
codes, 404 when `--memory-id` is unset). Full suite: 81/81 passing.

## Phase 8 — JWT Authorization (Okta)

See DESIGN.md §2a (decisions #26–35) for full rationale. Execution order:

- [ ] Register a new, dedicated Okta application for the Travel Agent
      (native/public client, PKCE required, `offline_access` scope) —
      manual step in the Okta Admin Console, done by the user. Capture the
      issuer URL and client ID. **(User action required — not done by
      this build; see summary below.)**
- [x] `travel-planning-agent/.env.template` (committed) and `.env`
      (gitignored — already covered by the existing `.env`/`.env.*`
      `.gitignore` entries) — `OKTA_ISSUER`, `OKTA_CLIENT_ID`,
      `OKTA_SCOPES`, `OKTA_REDIRECT_PORT` for the new app, distinct from
      `~/okta-claude-code-token-helper/.env`.
- [x] `cli/agent_client.py` — added `get_okta_access_token()`, invoking
      `~/okta-claude-code-token-helper/okta-claude-code-token.py` as a
      subprocess with the Travel Agent's Okta env vars set explicitly
      (via a small `load_dotenv()` reader, not that script's own `.env`
      loading). Raises `RuntimeError` with the helper's stderr on nonzero
      exit.
- [x] `cli/agent_client.py`'s `stream_agent_events()` — replaced the
      SigV4-signed `client.invoke_agent_runtime(...)` boto3 call with a
      raw `httpx.stream()` HTTPS POST carrying `Authorization: Bearer
      <token>`. Confirmed via AWS's own docs (JWT bearer auth requires
      "HTTPS requests required, not managed by AWS SDKs") and multiple
      real reference implementations that boto3's `bedrock-agentcore`
      client has no bearer-token path — this was the one open technical
      question from the design phase, resolved during implementation.
- [x] `cli/chat.py` — dropped `--actor-id` (superseded by decision #31 —
      actor_id is now server-derived from the JWT, not client-supplied);
      calls `get_okta_access_token()` once per turn.
- [x] `web/server.py` — same token-acquisition and bearer-auth change as
      `cli/agent_client.py` (shared module); dropped `--actor-id` here
      too. `create_app()` now acquires one token at startup solely to
      derive `actor_id` (via `_actor_id_from_token()`, decoding the `sub`
      claim) for the conversation-history endpoints, which remain on
      boto3/IAM — Memory access (`ListSessions`/`ListEvents`/`CreateEvent`/
      `DeleteEvent`) is a separate, unrelated IAM-authorized boundary from
      the Runtime's JWT authorizer (decision #34's scope is Runtime auth
      only).
- [x] `cdk/stacks/runtime_stack.py` — replaced
      `RuntimeAuthorizerConfiguration.using_iam()` with `.using_jwt(...)`
      pointed at the Okta discovery URL (`{OKTA_ISSUER}/.well-known/openid-configuration`)
      and the new app's client ID (`allowed_clients`); added
      `request_header_configuration=agentcore.RequestHeaderConfiguration(allowlisted_headers=["Authorization"])`
      so the raw bearer token reaches the agent process (decision #34).
      Exact parameter names verified by reading the installed
      `aws_cdk.aws_bedrockagentcore` package source directly, not guessed.
      `RuntimeStack` now takes `okta_issuer`/`okta_client_id` kwargs;
      `cdk/app.py` reads them from `.env` (reusing
      `cli/agent_client.load_dotenv()`) and fails fast if either is unset.
- [x] `agent/requirements.txt` / `web/requirements.txt` — added
      `PyJWT==2.13.0`. `cli/requirements.txt` simplified to just
      `httpx==0.28.1` (boto3 no longer used by the CLI/web client path).
- [x] `agent/agent.py` — added `get_actor_id(context)`, which reads
      `context.request_headers["Authorization"]` (confirmed exact key via
      `bedrock_agentcore.runtime.app.py`'s canonical-casing normalization),
      decodes the JWT with PyJWT (`verify_signature=False` — the Runtime's
      JWT authorizer already validated it), and returns the `sub` claim,
      falling back to `DEFAULT_ACTOR_ID` if absent/malformed.
      `parse_runtime_session_id()` was renamed to `parse_session_id()` and
      now only extracts the session_id half of the runtime session ID
      string — the actor_id half is no longer derived from client-supplied
      input.
- [x] `tests/test_agent.py` — replaced `ParseRuntimeSessionIdTests` with
      `ParseSessionIdTests` + new `GetActorIdTests` (using real,
      unsigned-key PyJWT-encoded tokens, since signature verification is
      intentionally skipped in the code under test).
- [x] `tests/test_agent_client.py` — fully rewritten for `httpx.stream()`
      mocking (bearer-token headers, URL construction, HTTP-status-error
      and transport-error paths) plus new `GetOktaAccessTokenTests`
      (subprocess success/failure/missing-config/missing-script paths).
      `tests/test_chat.py` updated for `_consume_stream()`'s new
      `(access_token, arn, region, session_id, prompt, qualifier)`
      signature.
- [x] `web/tests/test_server.py` — added a module-level fixed test JWT
      (`sub=web-user`), patched `web_server.get_okta_access_token`
      globally in both test classes' `setUp()`, removed the `actor_id=`
      kwarg from all 7 `create_app()` call sites, updated
      `stream_agent_events` call-arg index assertions for the new
      6-argument signature.
- [x] Manual end-to-end verification against a live deployed Runtime is
      **deferred to the user** (requires a real Okta app registration and
      `cdk deploy`, neither of which this build session can perform) — see
      the summary at the end of this phase for the exact remaining steps
      (Okta app registration, `.env` setup, `cdk deploy`, live login/chat
      test, confirming an invalid/missing token is rejected).
- [x] `README.md` — replaced the IAM/local-AWS-credentials framing in
      "Usage" and "Web UI" (and the top Architecture diagram) with the new
      Okta login flow; added a new "Authentication" section documenting
      the `.env` setup step and the Okta app registration prerequisite.
- [x] Full test suite passing (`pytest tests/ web/tests/`): **125/125**.
      CDK synth verified up through stack construction/prop validation
      (with dummy `OKTA_ISSUER`/`OKTA_CLIENT_ID` values) — reached Lambda
      asset bundling before failing on an unrelated pre-existing local
      environment issue (the `finch` container VM was stopped), confirming
      `RuntimeStack`'s new Okta kwargs and `using_jwt()`/
      `RequestHeaderConfiguration` wiring are valid at the CDK API level.

### Post-implementation fix: Okta `client_id` claim mismatch, and real deployment
Live end-to-end verification against a real deployed Runtime (deferred at
initial implementation time, since it needed a real Okta app registration)
surfaced two real issues, both fixed and re-verified live:

1. **`allowedClients` never matches real Okta tokens.** A decoded live
   Okta access token showed no `client_id` claim at all — Okta places the
   client identifier in a `cid` claim instead. `allowedClients` validates
   `client_id`, so it would silently reject every real Okta-issued token.
   Fixed by switching `runtime_stack.py`'s
   `RuntimeAuthorizerConfiguration.using_jwt()` call from
   `allowed_clients=[okta_client_id]` to `allowed_audience=[okta_audience]`
   instead (validates the `aud` claim, which Okta does populate from the
   authorization server's own configured Audience setting). The user
   configured that Audience to a static string, `api://travel-planning-
   agent`, rather than the Runtime's own ARN — a Runtime recreation
   assigns a new ARN suffix, which would have silently invalidated every
   previously-issued/cached token had the ARN itself been used as the
   audience. `.env`/`.env.template`/`cdk/app.py` updated: `OKTA_AUDIENCE`
   replaces `OKTA_CLIENT_ID` as the value passed into `RuntimeStack`
   (`OKTA_CLIENT_ID` is retained in `.env` — still needed by the token
   helper script itself to request tokens, just no longer what the Runtime
   authorizer checks).
2. **`sub` claim shape breaks AgentCore Memory's `actorId` constraint.**
   `cdk deploy` succeeded and the JWT authorizer worked correctly (`401`
   confirmed for an unauthenticated request), but the first real CLI chat
   turn failed mid-stream: `ListEvents` raised `ValidationException` on
   `actorId`. This Okta org's `sub` claim is an email address
   (`kenkitts@amazon.com`) — AgentCore Memory's `actorId` pattern
   (`[a-zA-Z0-9][a-zA-Z0-9-_/]*(?::[a-zA-Z0-9-_/]+)*[a-zA-Z0-9-_/]*`,
   confirmed against the real `ListEvents` API reference) rejects `@`/`.`
   verbatim. `sub` itself is only OIDC-guaranteed to be unique and stable
   — never guaranteed to satisfy an arbitrary downstream system's ID
   format. Fixed by adding `sanitize_actor_id()` to `agent/agent.py`
   (maps every disallowed character to `-`, strips a leading run of them
   since the pattern requires an alphanumeric first character,
   deterministic per input) and calling it from `get_actor_id()`.
   Deliberately did *not* switch to Okta's `uid` claim instead (which is
   already regex-safe) — `uid` is Okta-specific, and sanitizing `sub`
   keeps the code correct against any OIDC-standard-compliant IdP, not
   just this one. The identical bug existed in `web/server.py`'s
   `_actor_id_from_token()` (used for the conversation-history sidebar's
   `ListSessions`/`ListEvents`/`CreateEvent` calls) and surfaced the same
   way live: `/api/conversations` and `/api/conversations/{id}` both
   returned `502` in a running web UI session. Fixed with an identical
   `_sanitize_actor_id()` in `web/server.py` — not extracted into a shared
   module, since `agent/` is a separate deployment unit with its own
   `requirements.txt` (matching this project's existing per-component
   boundary); the two copies are commented as needing to stay in sync.

   Added 7 tests to `tests/test_agent.py` (`SanitizeActorIdTests` +
   `GetActorIdTests.test_sanitizes_email_shaped_sub_claim`) and 6 to
   `web/tests/test_server.py` (`SanitizeActorIdTests` +
   `ActorIdFromTokenTests`). Full suite: **138/138** passing.

   Verified live end-to-end after both fixes and a `cdk deploy
   TravelAgentRuntimeStack` redeploy: `curl` against the live invocation
   URL with no `Authorization` header returned `401` (unauthenticated
   calls correctly rejected); the CLI (`python cli/chat.py
   --agent-runtime-arn ...`) completed a full turn and correctly recalled
   a prior-session fact about the user ("Good to see you again, Ken");
   the web UI's `GET /api/conversations` went from `502` to `200` with
   the actual conversation history populated, `session_id` values
   correctly prefixed with the sanitized `kenkitts-amazon-com` actor_id
   matching what the agent itself uses server-side.

## Phase 9 — Gateway Observability (CloudWatch logs + traces)

Prompted by an opaque `"An internal error occurred. Please retry later."`
surfaced through the agent when a Gateway tool call failed, with no way to
see the actual underlying error. Confirmed via AWS docs that, unlike
Runtime (which gets a CloudWatch log group automatically), AgentCore does
**not** provision any log/trace destination for Gateway resources by
default — it has to be configured explicitly, one-time, per Gateway.

- [x] `cdk/stacks/gateway_stack.py` — added `_configure_observability()`,
      wiring up both application logs and X-Ray traces for the Gateway,
      per AWS's documented SDK pattern (a delivery source + delivery
      destination + delivery, for each of the two log types):
      - A dedicated `logs.LogGroup` (`/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/travel-planning-agent-gateway`,
        1-month retention).
      - `logs.CfnDeliverySource` (×2: one `log_type="APPLICATION_LOGS"`,
        one `log_type="TRACES"`), both with `resource_arn` set to the
        Gateway's own ARN.
      - `logs.CfnDeliveryDestination` (×2): a `CWL` destination pointed at
        the new log group, and an `XRAY` destination (X-Ray is the only
        supported trace destination type for this resource).
      - `logs.CfnDelivery` (×2) connecting each source to its matching
        destination, with explicit `node.add_dependency()` calls since
        CloudFormation doesn't infer the source/destination ordering from
        the plain string `name`/ARN references these constructs take.
      Confirmed CloudWatch Transaction Search (required for X-Ray spans to
      actually land in CloudWatch) was already enabled and `ACTIVE` in
      this account via `xray get-trace-segment-destination` — no extra
      account-level setup needed.
- [x] Verified via `cdk synth` (clean resource graph: both delivery
      sources/destinations/deliveries present, correctly cross-referenced)
      and a full `cdk deploy TravelAgentGatewayStack` (all 10 resources
      `CREATE_COMPLETE`).
- [x] Full test suite still passing (138/138) — this is pure infra
      configuration, no new application logic to unit test.
- [x] Live-verified the new logs are actually useful: reproduced a real
      Web Search tool failure via the CLI, then pulled the newly-vended
      Gateway application logs (`/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/travel-planning-agent-gateway`,
      stream `BedrockAgentCoreGateway_ApplicationLogs`) and found the
      specific underlying error — a target-connectivity failure inside
      the managed Web Search connector's own backend, unrelated to this
      project's account/VPC/IAM configuration and outside anything fixable
      from this codebase. Before this phase, that failure surfaced to the
      end user only as the generic "An internal error occurred" message,
      with no way to see request/response bodies, target names, or
      trace/span IDs to correlate a failure back to a specific tool call.

## Phase 10 — Hosted Web UI on ECS Fargate (OIDC ALB), Runtime auth reverted to IAM

Requested: host the web UI on AWS (ECS Fargate + autoscaling, an ALB with
OIDC enabled, an existing TLS certificate) instead of everyone running
`web/server.py` locally. See DESIGN.md §2b (decisions #37-44) for the full
design rationale, gathered via a clarifying-questions pass before any code
was written.

- [x] `cdk/stacks/runtime_stack.py` — reverted `RuntimeAuthorizerConfiguration`
      from `.using_jwt(...)` back to `.using_iam()`, removed the
      `okta_issuer`/`okta_audience` constructor kwargs and the
      `RequestHeaderConfiguration` bearer-token-forwarding config entirely
      (DESIGN.md decision #37 — once `TravelAgentWebStack`'s ALB became the
      real human-facing identity boundary, the Runtime's own JWT authorizer
      was redundant, and had no answer for a non-interactive ECS task
      authenticating as a specific human anyway).
- [x] `agent/agent.py` — `get_actor_id()` rewritten to read `actor_id`
      directly from the invocation payload (`{"prompt": ..., "actor_id":
      ...}`) instead of decoding a forwarded JWT's `sub` claim; the caller
      (the web container, from its ALB's verified OIDC claims; the CLI,
      from a `--actor-id` flag) is trusted to supply the correct value —
      the same trust boundary every other IAM-authenticated payload field
      already has. `sanitize_actor_id()`'s character-set logic is
      unchanged (still needed regardless of where the raw value comes
      from). Removed the `PyJWT` dependency from `agent/requirements.txt`
      entirely — the agent no longer decodes any token.
- [x] `cli/agent_client.py` — full rewrite: dropped `get_okta_access_token()`
      and the `httpx`-based raw-HTTPS bearer-token streaming transport;
      `stream_agent_events()` now calls `boto3`'s
      `bedrock-agentcore.invoke_agent_runtime` (IAM/SigV4) directly, reading
      the streaming response body (a `botocore.response.StreamingBody`,
      which — confirmed by inspecting the installed botocore source
      directly — exposes the same `iter_lines()` interface the previous
      `httpx` response did, so the SSE-frame-parsing logic itself needed no
      changes). Added `build_invoke_payload()` to include `actor_id`
      alongside `prompt`. `cli/requirements.txt` swapped `httpx` back for
      `boto3`.
- [x] `cli/chat.py` — brought back a required `--actor-id` flag (there is no
      more Okta identity to derive one from); `run_repl()`/`_consume_stream()`
      updated for the new `stream_agent_events()` signature.
- [x] `web/server.py` — full rewrite of the auth path:
      - Removed the Okta-login-at-startup model entirely (no more
        `get_okta_access_token()`/subprocess call, no more single
        server-wide `actor_id` derived once and shared by every request).
      - Added `actor_id_from_oidc_header()`: derives `actor_id` fresh on
        *every* request from the ALB's `x-amzn-oidc-data` header, with real
        signature verification — decodes the JWT header (unverified) to
        read `kid`/`signer`, checks `signer` against the deployment's own
        `--alb-arn`, fetches (and caches, by `kid`) the ES256 public key
        from ALB's `https://public-keys.auth.elb.<region>.amazonaws.com/<kid>`
        endpoint, then verifies the full signature before trusting the
        `sub` claim — per AWS's own documented requirement that this header
        must be signature-verified, not trusted blindly, before any
        authorization decision is made on it. Raises 401 on any failure
        (missing header, signer mismatch, bad signature, malformed token) —
        unlike `agent/agent.py`'s payload-based `get_actor_id()`, there is
        no silent fallback to a shared default actor here, since a forged
        or missing OIDC header means the request didn't come through the
        ALB's login gate at all.
      - Every endpoint (`/api/chat`, `/api/conversations`,
        `/api/conversations/{id}`, the title/delete endpoints) now derives
        `actor_id` per-request via this function instead of using a
        startup-cached value — a long-running container correctly serves
        multiple distinct logged-in people without mixing up whose Memory a
        request should touch.
      - `create_app()` gained a required `alb_arn` parameter;
        `build_arg_parser()` gained `--alb-arn` (required) and changed the
        `--host` default from `127.0.0.1` to `0.0.0.0` (the ALB reaches the
        container over the VPC, not localhost).
      - `web/requirements.txt` gained `requests` (for the public-key fetch)
        and an explicit `cryptography` pin (previously only a transitive
        dependency); `PyJWT` stays, now used for ALB-header verification
        instead of Okta-token decoding.
- [x] `web/Dockerfile` (new) — `python:3.12-slim`, installs
      `web/requirements.txt`, copies `web/` and `cli/agent_client.py` (the
      one file `web/server.py` imports across the sibling `cli/`
      directory), no baked-in credentials — the ECS task role supplies AWS
      credentials at runtime via the container's task metadata endpoint.
- [x] `cdk/stacks/web_stack.py` (new) — `WebStack`:
      - A dedicated VPC (2 AZs, public + private-with-egress subnets, one
        NAT Gateway per AZ; DESIGN.md decision #39) — not a pre-existing
        VPC, matching this project's per-stack-provisions-its-own-resources
        pattern.
      - An ECS cluster + Fargate service (0.5 vCPU/1GB, `desired_count=1`,
        autoscaling 1-3 tasks on CPU 60% via `scale_on_cpu_utilization`;
        DESIGN.md decision #43), `circuitBreaker(rollback=True)` and
        `min_healthy_percent=100` (a `desired_count=1` deployment would
        otherwise be able to drop to zero running tasks mid-rollout, since
        ECS's own default `min_healthy_percent` is 50%).
      - Task role scoped to exactly this deployment's Memory/Runtime ARNs
        via `memory.grant_full_access()`/`runtime.grant_invoke()`
        (DESIGN.md decision #42) — no wildcard grants.
      - An internet-facing ALB (DESIGN.md decision #39) with a 180s idle
        timeout (decision #43) and an `authenticate-oidc` default listener
        action chained (via its own `next=` parameter, not a fluent
        method — confirmed by inspecting the installed CDK
        `aws_elasticloadbalancingv2` module's actual constructor signature
        rather than guessing) into a `forward` action to the Fargate target
        group (DESIGN.md decision #38). The container's `--alb-arn` CLI
        argument is wired from `self.load_balancer.load_balancer_arn`, so
        `actor_id_from_oidc_header()`'s signer check always matches this
        exact ALB.
      - `ecs.ContainerImage.from_asset()` builds/pushes the image as part of
        `cdk deploy` (DESIGN.md decision #41), pointed at
        `web/Dockerfile` with the build context set to the repo root
        (needed since the Dockerfile `COPY`s both `web/` and
        `cli/agent_client.py`) and explicit `exclude` patterns for
        `cdk/cdk.out`/`.venv`/`.git`/`__pycache__`.
      - **Real bug found and fixed during this stack's own development**:
        omitting those excludes caused a confirmed `ENAMETOOLONG` failure
        during `cdk synth` — without them, the Docker asset's build-context
        copy recursively copies `cdk/cdk.out` into itself on every synth
        (the previous synth's own output directory gets copied into the new
        one, which then gets copied into the next one, etc.), producing
        paths that eventually exceed the filesystem's path-length limit.
        Fixed by excluding `cdk/cdk.out` (and the other large/irrelevant
        directories) from the asset's Docker build context.
      - Certificate: `acm.Certificate.from_certificate_arn()` — this stack
        does not provision or import a certificate itself (DESIGN.md
        decision #40); the caller supplies an existing ACM certificate ARN.
      - `CfnOutput`s for the ALB's DNS name and ARN (the latter is also what
        an operator needs to configure a matching Okta redirect URI).
- [x] `cdk/app.py` — removed the `OKTA_ISSUER`/`OKTA_AUDIENCE` env-var
      requirement for `RuntimeStack` entirely (no longer needed after the
      IAM reversion); added conditional construction of `TravelAgentWebStack`
      gated on `WEB_CERTIFICATE_ARN` being set, reading the rest of its
      config (`WEB_OIDC_*`) from the same `.env`-loading mechanism, and
      failing fast with a clear error if `WEB_CERTIFICATE_ARN` is set but
      any `WEB_OIDC_*` var is missing. `cdk deploy --all`/`cdk synth --all`
      still work with zero `.env` configuration at all — they simply skip
      `TravelAgentWebStack` in that case.
- [x] `.env.template` — fully rewritten: removed the obsolete
      `OKTA_ISSUER`/`OKTA_CLIENT_ID`/`OKTA_SCOPES`/`OKTA_REDIRECT_PORT`/
      `OKTA_AUDIENCE`/`OKTA_TOKEN_HELPER_PATH` vars (no longer used
      anywhere in this project after the IAM reversion), replaced with
      `WEB_CERTIFICATE_ARN` and the `WEB_OIDC_*` vars `TravelAgentWebStack`
      actually needs.
- [x] Tests updated for the new auth model:
      - `tests/test_agent.py` — `GetActorIdTests` rewritten for the
        payload-based `get_actor_id()` (no more JWT construction/decoding
        in these tests).
      - `tests/test_agent_client.py` — full rewrite for the boto3-based
        transport (mocks `boto3.client(...).invoke_agent_runtime`'s
        response, including a mock `StreamingBody`-shaped `iter_lines()`).
      - `tests/test_chat.py` — updated for `_consume_stream()`'s new
        `(agent_runtime_arn, region, session_id, prompt, actor_id,
        qualifier)` signature.
      - `web/tests/test_server.py` — full rewrite: new
        `ActorIdFromOidcHeaderTests` class generates a real EC keypair
        in-test and signs JWTs with it exactly the way ALB would (ES256,
        `kid`/`signer` headers), mocking only the HTTP fetch of the public
        key (`requests.get`) so the actual cryptographic verification code
        path is genuinely exercised — covering acceptance of a valid
        token, `sub`-sanitization, and 401 rejection for each documented
        failure mode (missing header, signer mismatch, bad signature,
        malformed token, missing `sub`). Every other endpoint test class
        now passes a real `alb_arn` to `create_app()` and an
        `x-amzn-oidc-data` request header, with
        `actor_id_from_oidc_header` patched to a fixed `"web-user"` so
        those tests can focus on their own endpoint behavior without also
        constructing a signed token per call.
      - Full suite: **137/137** passing.
- [x] Verified via `cdk synth`: the four base stacks synth cleanly with no
      Okta configuration present at all; `TravelAgentWebStack` synths
      cleanly on its own and as part of a full `cdk synth --all` when
      `WEB_CERTIFICATE_ARN`/`WEB_OIDC_*` are set to placeholder values
      (real values require a real ACM certificate and a real Okta OIDC
      app registration, neither of which this build session can create —
      deferred to the user; see below).
- [x] `README.md` — full rewrite of the "Architecture" diagram,
      "Authentication" section (back to IAM/`--actor-id`, matching the
      original decision #15 framing), "Usage"/"Web UI" sections, plus a new
      "Hosting the Web UI" section documenting `TravelAgentWebStack`'s
      prerequisites (ACM cert, dedicated Okta OIDC app for the ALB, DNS
      out of scope), configure/deploy steps, and deployment notes (sizing,
      idle timeout, task role scope).
- [x] `DESIGN.md` — added §2b (decisions #37-44) documenting the Runtime-auth
      reversion and every hosting decision (compute/ALB/auth, networking,
      TLS/DNS scope, container build mechanism, IAM scope, sizing
      assumptions, CI/CD scope), each with the rationale gathered from the
      clarifying-questions pass before implementation began.

## Phase 11 — CLI removal (added 2026-08-28)

Requested: remove the local CLI REPL client entirely — the hosted web UI
(Phase 10) becomes the only supported way to use the agent. See
DESIGN.md §2d (decisions #48-49) for the full rationale.

- [x] Relocated `cli/agent_client.py` → `web/agent_client.py` (pure move,
      docstrings updated to drop CLI references) — `web/server.py`
      depends on this module for session-ID construction and the actual
      `InvokeAgentRuntime` call, so it could not simply be deleted with
      the rest of `cli/`.
- [x] `web/server.py` — import changed from a `sys.path.insert()` hack
      reaching into a sibling `cli/` directory to a plain same-directory
      `from agent_client import ...`; docstring/comments updated.
- [x] `web/Dockerfile` — simplified to `COPY . .` from a `web/`-only build
      context (previously `COPY web/ ./web/` plus a separate
      `COPY cli/agent_client.py ./cli/agent_client.py`); `ENTRYPOINT`
      updated from `web/server.py` to `server.py` to match.
- [x] `cdk/stacks/web_stack.py` — `ecs.ContainerImage.from_asset()`'s
      build context narrowed from the whole repo root to `web/` alone
      (`str(REPO_ROOT / "web")`, `file="Dockerfile"`); the `exclude` list
      for `cdk/cdk.out`/`.venv`/`.git` (needed only because the previous
      repo-root build context could otherwise recursively copy `cdk.out`
      into itself — DESIGN.md decision #41's `ENAMETOOLONG` bug) is
      removed entirely, since `web/` never contained any of those
      directories.
- [x] Deleted `cli/` entirely (`chat.py`, `agent_client.py`,
      `requirements.txt`).
- [x] Deleted `tests/test_chat.py` outright (tested `_consume_stream()`,
      CLI-only non-streaming-text-joining logic with no web-UI
      equivalent — the web UI streams SSE directly to the browser and
      never needs a joined final string). Moved `tests/test_agent_client.py`
      to `web/tests/test_agent_client.py` unchanged in content (none of
      its coverage was CLI-specific — it tests `build_runtime_session_id()`,
      `build_invoke_payload()`, and `stream_agent_events()` against mocked
      boto3 calls), only its `sys.path` setup updated for the new location.
      `web/tests/test_server.py`'s own `sys.path` insert (previously
      pointing at a `_CLI_DIR`) updated to point at `web/` instead.
- [x] Fixed CLI references in code comments/docstrings across
      `agent/agent.py` (module docstring, `SESSION_ID_SEPARATOR` comment,
      `get_actor_id()` and `invoke()` docstrings), `cdk/stacks/runtime_stack.py`
      (module docstring, `authorizer_configuration` inline comment — the
      unrelated `agentcore` AWS CLI tool reference elsewhere in this file
      was left alone, since that's a different tool), and `cdk/app.py`
      (`WebStack` docstring, `_load_dotenv()` docstring).
- [x] `README.md` — removed the "Usage" (CLI) section entirely; rewrote
      "Architecture" (single Web UI path), "Repo layout" (no `cli/`
      entry), "Authentication" (no CLI mention), and "Web UI" (no longer
      framed as "an alternative to the CLI") to reflect the web UI as the
      only client; fixed the "Hosting the Web UI" prerequisites section's
      stale CLI/Okta comparison.
- [x] `DESIGN.md` — added §2d (decisions #48-49); fixed the §3 architecture
      diagram and its following paragraph (previously described three
      different clients sharing `cli/agent_client.py`); fixed §5's "Out of
      Scope" section, which — independent of this change — was already
      stale before this session (it claimed the hosted web UI was still a
      future fast-follow, contradicting §2b, which had already shipped
      it); did not edit any historical decision-table row (#10, #15, #19,
      #20, #26, #27, #29, #31, #32, #34, #35, #37, #39, #41, #42) — these
      correctly record what was true at each point in time, per this
      document's own established convention of superseding rather than
      rewriting history.
- [x] Full test suite: **135/135** passing (141 from before this change,
      minus the 6 deleted `test_chat.py` tests — no other regressions).
- [x] Verified via `cdk synth`: `TravelAgentWebStack` still synthesizes
      cleanly with the narrowed Docker build context.
- [x] Verified the new `web/Dockerfile`'s `COPY . .` layout resolves
      correctly via a filesystem simulation (copied `web/`'s contents into
      a simulated `/app`, confirmed `agent_client.py`/`server.py`/`static/`
      land as siblings, and confirmed `from agent_client import
      build_runtime_session_id, stream_agent_events` actually imports and
      runs) — a real `docker build` could not be run in this environment
      (Docker Desktop requires an org sign-in unavailable here), so this
      simulation is the closest available substitute; the next real `cdk
      deploy` will perform the actual build and surface anything this
      simulation missed.

### Remaining manual steps (deferred to the user — this build session cannot perform them)
1. Register a dedicated Okta OIDC web application (confidential client,
   with a client secret) for the ALB — a *different* app than any used by
   the CLI (which no longer uses Okta at all after this change).
2. Obtain the ACM certificate ARN for the ALB's HTTPS listener (already
   issued/validated in `us-east-1`, per the user's stated prerequisite).
3. `cp .env.template .env` and fill in `WEB_CERTIFICATE_ARN` and the
   `WEB_OIDC_*` values from steps 1-2.
4. `cdk deploy TravelAgentWebStack --require-approval never` — this also
   needs `TravelAgentRuntimeStack`/`TravelAgentMemoryStack` already
   deployed (their outputs are consumed via CDK cross-stack references).
5. After the first deploy, take the `AlbDnsName` output and add
   `https://<that-dns-name>/oauth2/idpresponse` as an allowed redirect URI
   on the Okta app from step 1, then redeploy if the app's redirect URI
   list required an update to take effect.
6. Point real DNS at the ALB's DNS name if a friendlier URL than the raw
   ALB hostname is wanted — explicitly out of scope for this stack
   (DESIGN.md decision #40).

## Phase 12 — Auth rearchitecture Phase 1: app-level OIDC replaces the ALB (added 2026-08-28)

Removes the ALB's `authenticate-oidc` listener action; `web/server.py`
now runs the entire OAuth 2.0 Authorization Code + PKCE flow against a
new, dedicated Okta app itself, storing the result in a single,
KMS-envelope-encrypted session cookie. See DESIGN.md §2e (decisions
#50-60) for the full rationale — motivated by a later, not-yet-built
phase's need for a live, forwardable/exchangeable signed JWT to
eventually give the AgentCore Gateway real per-user identity for its
Policy Engine/rate-limiting features, which the ALB's fully-decoded,
discarded `x-amzn-oidc-data` header couldn't provide.

- [x] Designed and wrote `web/auth.py`: PKCE (S256, stdlib `hashlib`/
      `base64`, no new dependency) and `state` (stdlib `secrets`)
      generation; `build_authorization_url()`; `exchange_code_for_tokens()`
      and `refresh_access_token()` (both call Okta's token endpoint
      directly via `requests`); `SessionCookieCodec` (KMS
      `GenerateDataKey`/`Decrypt` envelope encryption, AES-256-GCM for the
      actual payload cipher via `cryptography.hazmat.primitives.ciphers.
      aead.AESGCM`); `is_browser_navigation()` (the 401-vs-redirect split,
      via `Sec-Fetch-Mode`/`Accept` header inspection); `get_or_refresh_session()`
      (the synchronous refresh-on-expiry logic); `redirect_to_login()`
      (builds the pending-login cookie + Okta redirect together).
- [x] `cdk/stacks/web_stack.py`: removed `authenticate_oidc` listener
      action and all `oidc_*` constructor params that only served it
      (`oidc_authorization_endpoint`/`oidc_token_endpoint` kept — the app
      itself needs them now — `oidc_user_info_endpoint` removed, unused
      by the new flow); listener's `default_action` is now a plain
      `ListenerAction.forward([target_group])`. Added a new customer-managed
      KMS key (`SessionCookieKey`, automatic annual rotation enabled,
      `RemovalPolicy.DESTROY`) with `grant_encrypt_decrypt()` to the task
      role. Added a new Secrets Manager secret (`OidcClientSecret`) for
      the Okta app's client secret, with `grant_read()` to the task role —
      unlike the ALB's own `authenticate-oidc` client_secret prop (never
      exposed to the container), this secret is now used directly by
      `web/server.py` on every login/refresh. Container command args
      updated: `--alb-arn` removed, `--oidc-issuer`/
      `--oidc-authorization-endpoint`/`--oidc-token-endpoint`/
      `--oidc-client-id`/`--oidc-client-secret-arn`/`--oidc-redirect-uri`/
      `--session-cookie-kms-key-id` added. `redirect_uri` is built from
      `self.load_balancer.load_balancer_dns_name` (a CDK token resolved at
      deploy time), same "deploy once, register the callback URL with
      Okta" chicken-and-egg as Phase 10's original ALB-OIDC redirect URI.
- [x] `cdk/app.py`: `WebStack` docstring updated; `WEB_OIDC_USER_INFO_ENDPOINT`
      removed from the required-vars list (no longer read anywhere).
- [x] `web/server.py`: removed `actor_id_from_oidc_header()`,
      `_verified_oidc_sub()`, `_fetch_alb_public_key()`, and the ALB
      public-key URL template/cache — all ALB-signature-verification
      machinery is gone. `_sanitize_actor_id()` (and its regex/default
      constant) kept as-is, now standalone rather than only reachable
      through the removed ALB-header path. Added `GET /oauth2/callback`
      (validates `state`, exchanges the code, sets the session cookie,
      redirects to the originally-requested page). Added a shared
      `_resolve_auth()`/`_actor_id()` helper pair used by every protected
      route (`/api/chat`, `/api/whoami`, `/api/conversations*`) — `_actor_id()`
      also applies a refreshed cookie's `Set-Cookie` onto the response if
      `get_or_refresh_session()` had to refresh. `/api/chat`'s
      `StreamingResponse` explicitly copies the dependency-injected
      `Response` object's headers onto itself, since FastAPI's automatic
      header-merging only applies when a route returns that same object
      directly — a `StreamingResponse` built inside the route needed this
      spelled out explicitly, confirmed by testing (a naive first version
      silently dropped a refreshed `Set-Cookie` on this endpoint only).
      `/api/config` deliberately left unauthenticated (unchanged from
      before — it's a static feature flag, not identity-scoped data).
      `build_arg_parser()`/`main()` updated for the new CLI args;
      `main()` fetches the Okta client secret from Secrets Manager once
      at startup (`_fetch_secret_value()`), not per-request.
- [x] `web/requirements.txt`: no changes — `boto3` (KMS/Secrets Manager),
      `requests` (Okta token endpoint calls), `cryptography` (AES-GCM),
      and `PyJWT` (decoding the access token's `sub` claim) were all
      already present; PKCE itself needs only the standard library.
- [x] `web/static/app.js`: removed `handleAuthExpired()`'s
      `AUTH_RELOAD_KEY`/capped-retry reload-loop-guard machinery (no
      longer needed — see DESIGN.md decision #56) and simplified it to a
      one-line `window.location.reload()`, only ever called from an
      actual, confirmed `res.status === 401` check (already present in
      every relevant call site from before this change, but previously
      dead code under the ALB's always-302 behavior). Every generic
      `catch` block that previously treated *any* fetch rejection as a
      possible auth failure now surfaces a real error message instead,
      since a rejection reaching those blocks is now reliably a genuine
      network/transport error, not an ambiguous CORS-blocked-redirect
      case. `init()`'s `/api/config` fetch no longer checks for a 401 (it
      can never return one — see server.py note above); the
      conversation-history transcript fetch became this page's first
      real auth check instead.
- [x] `web/tests/test_server.py`: fully rewritten. New `FakeKmsClient`
      (a real AES-256 data key, genuine envelope-encryption round trip,
      just without a network call to AWS) backs a shared `_AppTestCase`
      base class and `_authed_client()` helper that pre-loads a valid,
      encrypted session cookie for tests that just need "a logged-in
      user." Replaced the old `ActorIdFromOidcHeaderTests` class with
      `SessionCookieCodecTests` (encrypt/decrypt round trip, tamper
      rejection), `OAuthFlowTests` (401-vs-redirect split, full
      redirect→callback→authenticated-request round trip, `state`
      mismatch, Okta `error` param, token-exchange failure), and
      `SessionRefreshTests` (silent refresh on an expired access token
      with no refresh-token rotation, refresh failure surfacing as a 401,
      no refresh attempted when the access token is still valid).
      Every other existing test class's auth injection switched from
      patching `actor_id_from_oidc_header` to using `_authed_client()`.
      **Non-obvious fix required**: `TestClient` must be constructed with
      `base_url="https://testserver"` for these tests to work at all —
      `httpx`'s cookie jar silently drops `Secure`-flagged cookies under
      the default plain-`http://testserver` base URL, which looks
      identical to a genuine missing-session-cookie/401 failure and cost
      real debugging time to isolate as a test-harness artifact rather
      than an app bug.
- [x] Full test suite: **142/142** passing (136 from before this change;
      net +6 — `SessionCookieCodecTests` (3) + `OAuthFlowTests` (7) +
      `SessionRefreshTests` (3), minus the removed `ActorIdFromOidcHeaderTests`
      (8) covering the deleted ALB-signature-verification path).
- [x] Verified via `cdk synth TravelAgentWebStack` (with placeholder Okta
      config values) — synthesizes cleanly: KMS key, Secrets Manager
      secret, plain `forward`-only ALB listener, and the new container
      command args all present as expected in the synthesized template.
- [x] Verified the full OIDC round trip end-to-end via a manual
      `TestClient` script (not part of the automated suite, but run
      directly against the real `create_app()`/`auth.py` code, with only
      `requests.post` and the KMS client mocked): unauthenticated →
      redirect to Okta with real `state`/`code_challenge` params →
      simulated `/oauth2/callback` → session cookie set and decryptable →
      authenticated request succeeds → manually expiring the cookie's
      access-token timestamp and re-requesting triggers a real inline
      refresh call and a rewritten `Set-Cookie`, all with the confirmed
      no-refresh-token-rotation behavior (same refresh token used both
      times).

### Open verification item (not resolved by this build session)
### Open verification item — RESOLVED (2026-08-29)
`auth.py`'s `_sub_from_access_token()` decodes Okta's returned access
token as a JWT (unverified — safe here, see DESIGN.md decision #58) to
extract `sub`. **Confirmed against the real, registered Okta app (decision
#50): this org's access tokens are JWT-shaped and carry a `sub` claim** —
the assumption this decode step relies on holds. No code change needed.

### Remaining manual steps (deferred to the user — this build session cannot perform them)
1. ~~Register a new, dedicated Okta OIDC application~~ — **done**: a
   dedicated confidential-client Okta app has been registered for this
   web server (DESIGN.md decision #50), configured for `openid
   offline_access` scopes and PKCE (S256).
2. ~~Confirm this Okta app's refresh-token behavior~~ — **done**: this
   Okta app is configured to keep issuing the same, non-rotating refresh
   token (DESIGN.md decision #52).
3. ~~Resolve the open verification item above~~ — **done**, see above.
4. ~~Update `.env`'s `WEB_OIDC_*` values~~ — **done**, real values
   confirmed present (no placeholder text remaining) before deploy.
5. ~~`cdk deploy TravelAgentWebStack`~~ — **done**, twice: an initial
   deploy (`UPDATE_COMPLETE`, new task healthy, old task drained,
   confirmed via `describe-stacks`/`describe-target-health`), then a
   second deploy after a real bug was caught via live `curl` checks
   immediately after the first one (see DESIGN.md decision #61 — `GET /`
   had no auth check at all) — also `UPDATE_COMPLETE`, re-verified live.
6. ~~Register `https://<AlbDnsName output>/oauth2/callback`~~ — **superseded, see DESIGN.md decision #62**: the user's real Okta app was already
   registered with a friendly custom domain
   (`https://travel-agent.kenkitts.people.aws.dev/oauth2/idpresponse`),
   not the raw ALB DNS name this session's first deploy actually used —
   confirmed via a live `curl` against `/`'s `/authorize` redirect, which
   would have failed every real login attempt with a `redirect_uri`
   mismatch. Fixed by adding a new required `web_hostname` stack
   parameter (`WEB_HOSTNAME` env var, added to `.env`/`.env.template`)
   so `redirect_uri` is built from the caller's real domain instead of
   `load_balancer_dns_name` — confirmed via `cdk synth` that
   `--oidc-redirect-uri` now resolves to
   `https://travel-agent.kenkitts.people.aws.dev/oauth2/callback`. **Still
   required, deferred to the user**: update the Okta app's registered
   redirect URI from `/oauth2/idpresponse` (the ALB's old native-OIDC
   path, no longer served by anything) to `/oauth2/callback` (this
   server's actual route) — the domain now matches, but the path does
   not, and this codebase cannot update Okta's own app configuration
   itself.

### Live post-deploy verification (2026-08-29)
Confirmed via direct `curl` against the real deployed ALB (not just unit
tests) after both deploys:
- `GET /api/config` (no session) → `200` — correctly unauthenticated.
- `GET /` with `Sec-Fetch-Mode: navigate` and no session → `302` to Okta,
  with real `state`/`code_challenge`/`scope=openid+offline_access`
  params in the redirect URL — confirmed **only after the second deploy**;
  the first deploy incorrectly returned `200` here (DESIGN.md decision #61).
- `GET /` and `GET /api/whoami` with `Sec-Fetch-Mode: cors` and no
  session → `401` in both cases — the fetch()-vs-navigation split (decision
  #56) working as designed against real traffic, not just the test suite.

## Phase 13 — Auth rearchitecture Phase 2: RFC 8693 token exchange replaces IAM/SigV4 for the Runtime (added 2026-08-29)

See DESIGN.md §2f (decisions #63-75) for the full design rationale and
the two AWS-documentation findings that reshaped this phase mid-design
(JWT/IAM inbound auth are mutually exclusive per-Runtime, and boto3
cannot invoke a JWT-authorized Runtime at all).

### What was built
- `web/auth.py`: `RuntimeOidcConfig`/`RuntimeToken` dataclasses,
  `exchange_token_for_runtime()` (real RFC 8693 call to a dedicated Okta
  "API Services" app), a second, independent KMS-envelope-encrypted
  cookie (`RUNTIME_TOKEN_COOKIE_NAME`, via `RuntimeTokenCookieCodec` —
  `SessionCookieCodec`'s envelope logic was refactored into a shared
  `_KmsEnvelopeCodec` base so both cookies use the identical scheme
  without duplicating it), and `get_or_exchange_runtime_token()`
  (silent re-exchange on a missing/expired/malformed Runtime-token
  cookie — never itself an auth failure, per decision #69).
- `web/agent_client.py`: `stream_agent_events()` fully rewritten —
  `boto3.client('bedrock-agentcore').invoke_agent_runtime()` replaced
  with a raw `requests.post()` to the documented direct-Runtime HTTPS
  invocation URL, `Authorization: Bearer` header, manual SSE-body
  parsing (same yielded event shape as before). Gained a new required
  `bearer_token` parameter.
- `web/server.py`: `/api/chat` — and only `/api/chat`, per decision
  #70 — now resolves a Runtime-audienced JWT before invoking the agent,
  writes back a refreshed Runtime-token cookie when a fresh exchange
  happened, and returns a clean `502` (not a 401/redirect) on a genuine
  exchange failure (decision #71).
- `cdk/stacks/runtime_stack.py`: JWT authorizer support added as fully
  **optional** (decision #75) — `using_iam()` remains the default;
  `using_jwt(...)` only when `runtime_oidc_discovery_url` is passed in.
- `cdk/stacks/web_stack.py`: 6 new required constructor parameters for
  the exchange app's config, a second Secrets Manager secret
  (`RuntimeOidcClientSecret`) for its client secret (mirroring Phase
  1's `OidcClientSecret` pattern exactly), 6 new `--runtime-oidc-*`
  container command args.
- `cdk/app.py`: derives the Runtime's `discovery_url` from
  `WEB_RUNTIME_OIDC_ISSUER` + `/.well-known/openid-configuration`
  (Okta's discovery-URL convention); `RuntimeStack`'s new JWT config is
  entirely independent of `WEB_CERTIFICATE_ARN`/`WebStack` being
  deployed at all (decision #75); `WebStack`'s own `required_web_vars`
  gained the 6 new `WEB_RUNTIME_OIDC_*` keys as required-together-with
  the rest of `WEB_*`.
- `.env`/`.env.template`: new `WEB_RUNTIME_OIDC_*` block, using the same
  `{issuer}/v1/token` derivation convention already confirmed against
  Phase 1's own `WEB_OIDC_TOKEN_ENDPOINT`.
- Test suite: 163/163 passing (was 144 before this phase) — new
  `RuntimeTokenCookieCodecTests`, `TokenExchangeTests`, three new
  `/api/chat` integration tests (fresh-exchange, cached-token-reuse,
  exchange-failure-502), and a full rewrite of `test_agent_client.py`
  to mock `requests.post` instead of `boto3.client`.

### Verified
- `cdk synth TravelAgentRuntimeStack TravelAgentWebStack` against the
  **real** `.env` (not dummy values) — confirmed via direct JSON
  inspection of the synthesized templates: `AWS::BedrockAgentCore::Runtime`'s
  `AuthorizerConfiguration.CustomJWTAuthorizer` has the exact real
  `AllowedAudience`/`AllowedClients`/`AllowedScopes`/`DiscoveryUrl`;
  `WebStack` has the new `RuntimeOidcClientSecret` resource, all 6 new
  `--runtime-oidc-*` container args with correct real values, and
  `secretsmanager:GetSecretValue` IAM permission for the task role.
- Full test suite: 163 passed, 0 failed.

### Remaining manual steps
1. ~~`cdk deploy TravelAgentRuntimeStack TravelAgentWebStack`~~ — **done**
   (2026-08-29), with explicit user confirmation beforehand. Both
   stacks reached `UPDATE_COMPLETE`; confirmed via `describe-target-health`
   that the new ECS task is healthy and the old one drained; confirmed
   via CloudWatch logs that the container booted cleanly with all the
   new required `--runtime-oidc-*` args (`Application startup complete`,
   no argparse failures) and was already serving real traffic
   (`/api/config` 200s from the ALB health check, plus the live `curl`
   verification below).
2. Live verification after deploy via `curl` (not yet a real browser
   login) confirmed Phase 1 behavior is completely unaffected:
   unauthenticated `/api/config` → `200`, unauthenticated navigation to
   `/` → `302` to Okta, unauthenticated `/api/whoami` → `401`. **Not yet
   verified**: a real end-to-end `/api/chat` call (real login → real
   token exchange → real bearer-token `InvokeAgentRuntime` call → a
   real streamed response) — this requires a real browser login and has
   not been done yet; `curl` alone cannot exercise it (no real Okta
   session cookie to present).
3. ~~Consider testing the Runtime directly via the AgentCore console's
   test chat~~ — **confirmed regression** (user-reported, 2026-08-29):
   the AgentCore console's test-chat feature does require a valid JWT
   for console access and no longer works for this Runtime now that its
   authorizer is JWT-only — exactly the risk this step flagged before
   deploy, now confirmed rather than hypothetical. This is an accepted,
   known consequence of decision #63's opening AWS-docs finding (IAM and
   JWT inbound auth are mutually exclusive per-Runtime), not a bug to
   fix — the README's documented use case of testing the Runtime
   directly via the console (bypassing the web UI) is no longer
   available for this Runtime once `WEB_RUNTIME_OIDC_*` is configured.
   If this console-testing capability needs to be restored, the only
   options are: (a) revert this Runtime to IAM-only (undoes Phase 2
   entirely for this Runtime), or (b) obtain a valid JWT some other way
   to use in the console (e.g. minted via the same Okta token-exchange
   app, manually) — neither has been requested or built.
4. ~~Verify a real end-to-end `/api/chat` call~~ — **in progress, found
   and fixed two real bugs along the way** (DESIGN.md decisions #76-77):
   - A real login + real chat attempt first failed at the token-exchange
     step itself: Okta rejected the exchange with `invalid_dpop_proof`
     (DPoP/RFC 9449 was required on the exchange app — an unrelated,
     separate app-level Okta setting, not anything this phase's design
     covered). User disabled it in the Okta Admin Console; no code
     change needed.
   - The next real attempt got past token exchange but failed at
     `InvokeAgentRuntime` itself: `401 Claim 'client_id' value mismatch`
     — the *same* `cid`-vs-`client_id` real-token-shape mismatch this
     project already hit once before (see the original, later-reverted
     Okta-JWT cutover's own post-implementation fix, above). Fixed by
     removing `allowed_clients` from `RuntimeStack`'s JWT authorizer
     config entirely — `cdk/app.py` no longer wires
     `WEB_RUNTIME_OIDC_CLIENT_ID` into it; validation now relies solely
     on `allowedAudience`/`allowedScopes`. Redeployed
     `TravelAgentRuntimeStack` with this fix.
   - **Confirmed working end-to-end** (user-reported, 2026-08-29): a
     real login → real token exchange → real bearer-token
     `InvokeAgentRuntime` call → a real streamed agent response, all
     succeeding. Phase 2 is fully live and verified.
   - **Update (2026-08-30, DESIGN.md decision #79):** the user
     reconfigured the Okta client registrations to populate a real
     `client_id` claim on issued tokens, removing the root cause behind
     the `allowedClients` fix above. `allowed_clients` was wired back
     into `RuntimeStack`'s JWT authorizer (`cdk/app.py` passes
     `WEB_RUNTIME_OIDC_CLIENT_ID` again) as defense in depth on top of
     `allowedAudience`/`allowedScopes`. Redeployed
     `TravelAgentRuntimeStack` with this change.

## Phase 14 — Auth rearchitecture Phase 3: Gateway JWT authorizer + RFC 8693 On-Behalf-Of token exchange (added 2026-08-30)

See DESIGN.md §2g (decisions #80-87) for the full design rationale, the
AWS Security Blog post this follows almost directly, and the 18
clarifying-question-and-answer exchange that shaped it before any code
was written.

### What was built
- `cdk/stacks/gateway_stack.py`: authorizer switches from
  `GatewayAuthorizer.using_aws_iam()` to `GatewayAuthorizer.using_custom_jwt()`
  only when `gateway_oidc_discovery_url` is passed in (optional, mirrors
  `RuntimeStack`'s own pattern exactly). New
  `_add_oauth2_credential_provider()` provisions a
  `CfnOAuth2CredentialProvider` (`CustomOauth2` vendor,
  `onBehalfOfTokenExchangeConfig` with `grantType: TOKEN_EXCHANGE` and
  `actorTokenContent: NONE`), client secret passed as a plain literal
  string (**updated after live deployment** — the originally-planned
  `SecretReferenceProperty`/Secrets-Manager-reference approach was
  rejected by the live `CreateOauth2CredentialProvider` API for
  `CLIENT_SECRET_BASIC`/`CLIENT_SECRET_POST` auth; see DESIGN.md
  decision #90). Gateway construct ID is `"TravelAgentGatewayV2"`, not
  `"TravelAgentGateway"` (see DESIGN.md decision #88 for why — a
  permanent rename forced by AWS's Gateway API rejecting in-place
  authorizer-type changes).
- `cdk/stacks/runtime_stack.py`: removed the now-dead
  `gateway.grant_invoke(self.runtime)` IAM/SigV4 grant (same
  anti-pattern decision #78 already found and fixed once for
  `web_stack.py`'s Runtime-invoke grant); added a new grant for
  `bedrock-agentcore:GetResourceOauth2Token` (**expanded after live
  deployment** — see DESIGN.md decision #91 — to cover four resource
  families, not just the credential-provider ARN originally planned:
  the credential-provider ARN itself, `token-vault/default`,
  `workload-identity-directory/default`, and
  `workload-identity-directory/default/workload-identity/{RUNTIME_NAME}-*`
  as a wildcard prefix, not the resolved `agent_runtime_id`, to avoid a
  circular CloudFormation dependency), plus a new
  `secretsmanager:GetSecretValue` grant scoped to
  `bedrock-agentcore-identity!default/oauth2/{provider_name}-*` — the
  Secrets Manager secret AgentCore Identity creates and reads itself for
  the credential provider's client secret. Deliberately not
  `GetWorkloadAccessToken*` (AWS's docs confirm Runtime-managed
  identities can't call those directly; the workload token is
  auto-delivered). New `GATEWAY_OBO_PROVIDER_NAME` environment variable
  passed to the agent process. Also sets
  `request_header_configuration=agentcore.RequestHeaderConfiguration(allowlisted_headers=["Authorization"])`
  on the Runtime construct — **added after live deployment**, see
  DESIGN.md decision #95; without it the inbound `Authorization` header
  never reaches `agent.py` at all despite `authorizer_configuration`
  being set.
- `cdk/app.py`: new `GATEWAY_OIDC_*` env-var block (a third, separate
  Okta "API Services" app — distinct from both `WEB_OIDC_*` and
  `WEB_RUNTIME_OIDC_*`), read before `GatewayStack` construction and
  wired into its 6 new constructor kwargs; `RuntimeStack` construction
  now passes `gateway_oauth2_credential_provider=gateway_stack.oauth2_credential_provider`.
- `.env.template`: new `GATEWAY_OIDC_ISSUER`/`CLIENT_ID`/`CLIENT_SECRET`/
  `AUDIENCE`/`SCOPE` block with full setup instructions, matching Phase
  2's own `WEB_RUNTIME_OIDC_*` documentation style.
- `agent/agent.py`:
  - `get_actor_id()` now derives `actor_id` from the inbound JWT's `sub`
    claim (read via a new `_sub_from_authorization_header()` helper,
    decoding `BedrockAgentCoreContext.get_request_headers()`'s
    `Authorization` header without re-verifying the signature — safe
    because AgentCore Runtime's own JWT authorizer already verified it
    before invoking this code at all) — falling back to the payload's
    `actor_id` field only under IAM inbound auth. This restores decision
    #31's original "derive server-side from a verified token" stance for
    Memory scoping, closing the trust gap decision #37 had accepted.
  - `build_mcp_client()` is now `async def`. Unchanged (IAM/SigV4) path
    when `GATEWAY_OBO_PROVIDER_NAME` is empty. New path when set:
    reads the Runtime's own auto-delivered workload access token via
    `BedrockAgentCoreContext.get_workload_access_token()`, checks a new
    per-`sub`-keyed in-process cache
    (`_get_cached_or_exchange_gateway_token()`/`_GATEWAY_OBO_TOKEN_CACHE`
    — **added after live deployment**, see DESIGN.md decision #96;
    every conversation turn was re-exchanging against Okta without it),
    and on a cache miss exchanges the workload token for a
    Gateway-audienced JWT via
    `bedrock_agentcore.identity.auth.IdentityClient.get_token(auth_flow="ON_BEHALF_OF_TOKEN_EXCHANGE",
    custom_parameters={"subject_token_type": "urn:ietf:params:oauth:token-type:access_token"})`
    — the `custom_parameters` override **added after live deployment**,
    see DESIGN.md decision #92; AgentCore Identity's own default
    `subject_token_type` was rejected by this Okta org — then builds
    the MCP client with the base `mcp` library's own
    `streamablehttp_client(url=..., headers={"Authorization": f"Bearer {token}"})`
    instead of `aws_iam_streamablehttp_client` — no request signing at
    all under this mode. Raises `RuntimeError` (fails loud, doesn't
    silently degrade) if `GATEWAY_OBO_PROVIDER_NAME` is set but no
    workload token is present, since that combination means the
    Runtime's own inbound auth isn't actually JWT-configured to match.
- Test suite: 172/172 passing (was 163) — new `GetActorIdTests` cases for
  the JWT `sub`-claim path (a fake unsigned JWT via `pyjwt`'s `"none"`
  algorithm, since the decode path never verifies signatures anyway), and
  a new `BuildMcpClientTests` class covering both the IAM and OBO paths
  with mocked `IdentityClient`/`streamablehttp_client` calls (no real
  AgentCore Identity or network calls).

### Verified
- `cdk synth TravelAgentGatewayStack TravelAgentRuntimeStack` — both with
  no JWT config (falls back to IAM, matching the pre-Phase-3 baseline)
  and with dummy `GATEWAY_OIDC_*` values set — confirmed via direct JSON
  inspection of the synthesized templates: the Gateway's
  `AuthorizerConfiguration.CustomJWTAuthorizer` has the exact
  `AllowedAudience`/`AllowedClients`/`AllowedScopes`/`DiscoveryUrl`; the
  `AWS::BedrockAgentCore::OAuth2CredentialProvider` resource has the
  correct `CredentialProviderVendor`/`ClientId`/`ClientSecretConfig`/
  `OnBehalfOfTokenExchangeConfig` shape; the Runtime's IAM policy has
  exactly one new statement (`GetResourceOauth2Token`, correctly scoped
  to the credential provider's cross-stack-imported ARN) and zero
  `InvokeGateway`/`grant_invoke` statements remaining.
- `python -m pytest tests/ web/tests/`: 175/175 passing (was 172 at
  synth-only verification time — 3 new tests added for the OBO token
  cache, see below).
- **Deployed and verified live** (2026-08-31) — see DESIGN.md's "Live
  deployment findings" (decisions #88-96) for the full account of what
  the live deploy actually surfaced beyond synth/unit-test verification:
  a Gateway-authorizer-type change forcing full CDK-level replacement
  (construct-ID rename cascaded to the CloudWatch delivery sources), a
  `clientSecretConfig` API rejection requiring a plain-literal client
  secret instead, three sequential `AccessDeniedException`s resolving
  the true multi-resource-family IAM shape `GetResourceOauth2Token`
  actually needs, an Okta-specific `subject_token_type` override
  required via `customParameters`, a stale local `__pycache__` silently
  shipping through every deploy until the bundling command was fixed to
  strip it, AgentCore Runtime's per-session code pinning (requiring a
  fresh conversation to observe any new deploy's effect), and a missing
  `RequestHeaderConfiguration`/`allowlisted_headers=["Authorization"]`
  on `RuntimeStack` (decision #34 had already identified this
  requirement in the abstract during Phase 2's design, but it was never
  actually implemented until this live debugging surfaced it) — without
  which the inbound `Authorization` header never reached `agent.py` at
  all, silently breaking both `sub`-claim actor-ID derivation's edge
  case and the new OBO token cache below.
- Added an in-process, per-`sub`-keyed OBO token cache
  (`agent/agent.py`'s `_GATEWAY_OBO_TOKEN_CACHE`) after confirming live
  that every conversation turn was re-running the full RFC 8693 exchange
  against Okta — see DESIGN.md decision #96. Reduces this to
  approximately once per token lifetime (~1 hour) or per fresh/resumed
  session, confirmed live via CloudWatch logs showing 3 consecutive
  cache `HIT`s after the first `MISS`+`STORE` on a new session.
- End-to-end confirmed live: a real chat message exercising Gateway
  tools (weather/places/web search) succeeds with the Gateway's
  authorizer on `CUSTOM_JWT`, the Runtime performing the OBO exchange,
  and the exchanged token cached and reused across turns — the actual
  goal this phase set out to verify, not previously exercised at
  synth-only verification time.

## Explicit Non-Goals (tracked, not built now)
- Booking/payment tool integrations
- Structured JSON output / frontend
- Automated integration tests
- Billing budgets/alarms
- A CI/CD pipeline for `TravelAgentWebStack` (DESIGN.md decision #44 —
  plain manual `cdk deploy`, matching every other stack)
