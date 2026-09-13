# Travel Planning Agent — Design Decisions

Status: Approved (interview complete, 2026-08-17)

## 1. Purpose & Scope

An AI agent that conversationally builds day-by-day travel itineraries. It is
**not** a booking assistant — no flights/hotels/payments in scope. Booking
integrations are an explicit future extension, not part of this build.

## 2. Core Design Decisions

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| 1 | Scope | Itinerary builder only (no booking) | Booking requires payment/PCI-adjacent integrations — large surface area for v1. Itinerary generation is demoable and extensible later. |
| 2 | Input style | Conversational gathering | Agent starts from a vague request ("plan me a trip to Japan") and asks follow-ups (dates, budget, pace, interests, travelers) before generating anything. Plays to LLM strengths vs. a rigid form. |
| 3 | Grounding tools | Web search + maps/places + weather | Pure-LLM itineraries hallucinate hours/prices and produce geographically illogical day plans. All three groundings are in scope for v1. |
| 4 | Tool providers | AWS-native: AgentCore Web Search (managed connector), Amazon Location Service (maps/places), Open-Meteo via Lambda (weather) | Minimizes external vendor keys; Web Search is a genuine GA AgentCore Gateway connector (verified via AWS docs — zero data egress, MCP-based). Location Service integrates via IAM, no separate key. |
| 5 | Tool access architecture | Single AgentCore Gateway with 3 targets: Web Search connector, Lambda target (weather), Lambda target (Location Service wrapper) | One consistent MCP interface, centralized IAM auth, easy to add more targets later (e.g. booking API). |
| 6 | Agent framework | Strands Agents SDK | AWS's own framework, built for AgentCore, first-class MCP/Gateway support, most current AgentCore samples use it. |
| 7 | Model | Claude Sonnet (latest via Bedrock) | Multi-day itinerary planning needs strong multi-step reasoning + reliable tool-use chaining across 3 tools. |
| 8 | Memory | AgentCore Memory — short-term AND long-term | Short-term: tracks in-session requirement gathering (dates, budget, etc.). Long-term: remembers user preferences (e.g. "prefers food-focused, budget travel") across sessions/trips. |
| 9 | Output format | Plain conversational markdown text (v1) | No structured itinerary schema exists yet; deferred until there's a real consumer that needs it, to avoid premature schema design. (Streaming/diagnostic surfacing of the same markdown output was added later — see decision #19 — but the output format itself is unchanged.) |
| 10 | Testing/invocation clients | AgentCore console test chat, local Python CLI REPL, and a local web UI | Console for zero-setup manual smoke tests; CLI for scripted/headless testing; local web UI (added later, see decision #19) for a more usable manual-testing/demo experience with live streaming and a diagnostics view. |
| 11 | IaC | AWS CDK (Python) | AgentCore has CDK L2 constructs (`aws_cdk.aws_bedrockagentcore`: `Memory`, `Gateway`, `GatewayTarget.ForLambda`, `CfnRuntime` — verified in CDK docs). Reproducible, version-controlled. |
| 12 | Application language | Python (agent code, Lambda handlers, CDK) | Strands SDK is Python-native; single-language stack avoids unnecessary context switching for two simple Lambda handlers. |
| 13 | Project type | Personal AWS account project, standard OSS tooling | Not a Brazil/internal-Amazon build. Plain `cdk deploy`, plain git repo. |
| 14 | Location & region | New local repo, `us-east-1` | Verified via `aws___get_regional_availability`: AgentCore, Location Service both available in `us-east-1` and `us-west-2`. us-east-1 chosen as default/most common launch region. |
| 15 | Auth for invoking agent | IAM auth only | Matches CLI/console testing tools, and later the local web UI (decision #19), all of which use IAM/SigV4-signed calls with local AWS credentials — no separate identity system. Open/no-auth explicitly rejected — AgentCore invocations cost real money and shouldn't be internet-exposed. |
| 16 | Weather provider | Open-Meteo | Free, no API key, global coverage (rules out US-only NWS), avoids Secrets Manager setup needed for OpenWeatherMap's key. |
| 17 | Cost posture | No hard budget constraint | Optimize for correct architecture over cost minimization; still defaulted to free-tier services where reasonable (Open-Meteo). |
| 18 | Testing strategy | Unit tests only (Lambda handlers + deterministic logic) | Itinerary output is LLM-generated/non-deterministic — integration tests would only verify "didn't crash," not quality. Manual testing (via #10) covers end-to-end behavior. |
| 19 | Local web UI | Single-user, local-only FastAPI app (`web/server.py`) calling the same deployed Runtime as the CLI, via a shared `cli/agent_client.py` | A more usable manual-testing/demo surface than the CLI or console, without standing up any new AWS infrastructure. Explicitly local-only (binds `127.0.0.1`) since it holds real AWS credentials and has no auth layer of its own — not a general-purpose hosted frontend (see decision #15, unchanged: IAM auth only, no separate identity system). |
| 20 | Response delivery | Live SSE streaming from `agent/agent.py`'s `invoke()` (an async generator `BedrockAgentCoreApp` auto-wraps as `text/event-stream`), consumed incrementally by both clients | Waiting for a full multi-tool-call turn to complete before showing anything is a poor interactive experience for multi-day itineraries that can involve a dozen-plus tool calls. The CLI consumes the same stream but only prints the final text, preserving its original non-streaming UX; the web UI renders text deltas live. |
| 21 | Diagnostics | Optional, off-by-default diagnostic panel in the web UI showing every labeled event (reasoning, tool_use, tool_result, error) from the same stream | Useful for understanding/debugging agent behavior (which tools were called, with what arguments, extended-thinking text) without adding permanent UI clutter or a separate observability stack. Reasoning deltas are accumulated into one growing entry (not one per token) and tool_result JSON is pretty-printed, since both arrive as raw fragments/unformatted text from the model. |
| 22 | Extended thinking | Enabled via Bedrock Converse `additional_request_fields: {"thinking": {"type": "adaptive", "display": "summarized"}}` | `display: "summarized"` is required to get any non-empty reasoning text back from Claude Sonnet 5 in this configuration — without it, reasoning content is present in the API but empty when rendered. Surfaced only in the diagnostic panel (see #21), not the main chat bubble, since it's not meant for end users. |
| 23 | Conversation history browsing | Web UI sidebar reads directly from AgentCore Memory (`ListSessions`/`ListEvents`), no separate local storage | Memory is already the durable source of truth for conversation history; a second local store would just be a cache to keep in sync. Session titles are stored as a dedicated marker event (via `CreateEvent`) since Memory has no native session-title field; deletion removes every event in a session one at a time (no session-level delete API). |
| 24 | Context growth control | `SummarizingConversationManager` (`proactive_compression=True, pin_first=6`) on the Strands `Agent` | Long-running conversations (many turns, many tool calls) grow context indefinitely under Strands' own default; proactive summarization bounds this instead of only reacting after hitting a context-window error. |
| 25 | Long-response handling | `max_tokens=8192` explicit on `BedrockModel`, with `MaxTokensReachedException` handled gracefully in `invoke()` | Bedrock Converse's default per-model max output token limit is too low for some multi-day, tool-grounded itineraries — confirmed in production, a 13-tool-call turn hit the limit mid-answer and surfaced as an opaque `InvokeAgentRuntime` 500 error. 8192 gives realistic headroom; the exception handler covers cases that still exceed it. |


## 2a-2p. Auth rearchitecture, observability, and feature history

The detailed, phase-by-phase decision log for everything built after
the initial v1 (JWT auth attempts, the hosted web UI, three full auth
rearchitecture phases, observability passes, Gateway-routed inference,
AWS DevOps Agent integration, CloudWatch alarms/dashboard, logout,
identity display/landing page, and educational prompt caching) has
moved to `DESIGN_HISTORY.md` to keep this file lean. Decision numbers
there continue from this file's #1-25 (starting at #26) and are
globally unique across both files.

Quick index of what's in `DESIGN_HISTORY.md`:

- §2a — JWT Authorization (decisions #26-36)
- §2b — Hosted Web UI on ECS Fargate (decisions #37-44)
- §2c — Post-deploy hardening (decisions #45-47)
- §2d — CLI removal (decisions #48-49)
- §2e — Phase 1 auth rearchitecture: app-level OIDC replaces the ALB (decisions #50-62)
- §2f — Phase 2 auth rearchitecture: RFC 8693 token exchange for the Runtime (decisions #63-79)
- §2g — Phase 3 auth rearchitecture: Gateway JWT authorizer + OBO exchange (decisions #80-96)
- §2h — Observability pass: logging, tracing, log retention (decisions #97-105)
- §2i — Gateway-routed inference for centralized governance (decisions #106-122)
- §2j — AWS DevOps Agent integration (decisions #123-133)
- §2k — CloudWatch Alarms + Dashboard fast-follow (decisions #134-146, #172)
- §2l — Logout capability (decisions #143-146)
- §2m — Identity display + unauthenticated landing page (decisions #147-157)
- §2n — Gateway-routed inference made mandatory (decisions #158-163)
- §2o — AgentCore Gateway WAF 403 root-cause fix (decisions #164-165)
- §2p — Educational prompt caching for ModelRouter (decisions #166-171)
- §2q — Bedrock Guardrails via Strands hooks (decisions #172-181)

## 5. Out of Scope (v1) — Explicit Fast-Follows

- Booking/payment integrations (flights, hotels, activities)
- Structured JSON itinerary output / export (PDF, calendar) — the web UI
  is a chat interface, not this fast-follow; it still only renders the
  same plain markdown output the agent produces.
- Integration/end-to-end automated tests
- Cost budgets/billing alarms

~~Cognito/JWT auth for non-AWS-credentialed clients~~ — attempted, see
§2a, then **reverted, see §2b decision #37.** IAM auth (decision #15) was
replaced by Okta-issued JWT bearer tokens (decisions #26–35), then
reverted back to IAM once `TravelAgentWebStack`'s OIDC-authenticated ALB
became the real human-facing identity boundary, making the Runtime's own
JWT authorizer redundant.

~~A hosted/multi-user frontend for the web UI~~ — **implemented, see §2b.**
`TravelAgentWebStack` hosts the web UI on ECS Fargate; it is now the only
supported client (§2d — the local CLI REPL that existed alongside the
local-only web UI, decision #19, has been removed entirely). Originally
fronted by an OIDC-authenticated ALB (§2b decision #38); as of §2e, the
ALB is a plain (non-authenticating) load balancer and the OIDC login flow
runs inside `web/server.py` itself.

## 6. Educational Backlog — Strands/AgentCore features not yet used (added 2026-09-12)

Ideated directly from this project's own explicit purpose (educational —
"if I can learn from it, it's worth implementing," per the user)
against the current Strands Agents and AgentCore feature catalogs,
checked against what `agent/agent.py` already imports/uses (model
routing, `SummarizingConversationManager`, `Limits.turns`,
`AfterModelCallEvent`, `ContextInjector`, `AgentSkills`, prompt caching,
MCP tool client, Identity OBO exchange, Memory, Gateway-routed
inference, Code Interpreter) so these are genuinely unused surface, not
re-suggestions. None of these are scoped, scheduled, or committed to —
this is a candidate list, evaluated but not built. Ranked by the
author's own rough "educational value vs. reasonable scope" judgment at
ideation time (recorded for context, not a hard priority order).

| # | Idea | What it is | Why it'd be worth building here |
|---|---|---|---|
| 1 | Strands Evals | Client-side, offline agent-evaluation harness: cases, experiments, evaluators (LLM-as-judge and otherwise), multi-turn user simulation. | Closes a gap this project's own DESIGN.md decision #18 explicitly acknowledged and accepted ("itinerary output is non-deterministic — integration tests would only verify 'didn't crash'"). Evals is designed exactly for scoring non-deterministic output via judge-based rubrics instead of exact-match assertions — would give this project a real regression suite for itinerary *quality*, not just "didn't error," for the first time. |
| 2 | AgentCore Browser tool | Fully managed, isolated remote browser (Playwright/CDP over WebSocket), with live view, session recording, and S3-stored replay. | A genuinely novel capability with no equivalent in anything built so far. Self-contained (one new tool, no architecture change) and visually satisfying to exercise (watching an agent drive a real, isolated browser; replaying a session). A narrow, in-scope use case exists: fetching info from a real site's JS-heavy widget (e.g. a specific attraction's live hours/ticket calendar) that the Web Search connector can't reliably parse — kept deliberately narrow given decision #1 (no booking/payment scope). |
| 3 | Multi-agent orchestration — Graph or Swarm | Strands' other two multi-agent patterns (this project already uses `ModelRouter`, which picks *one* candidate per call, not true multi-agent orchestration). Graph: a predefined DAG with explicit dependencies/fan-out/feedback loops. Swarm: autonomous agent-to-agent handoffs with no fixed execution order, plus its own safety rails (`max_handoffs`, `max_iterations`, `execution_timeout`, `node_timeout`, repetitive-handoff detection). | Meaningfully changes this project's architecture rather than adding a peripheral capability. A Graph fits naturally on top of the existing `trip-pacing` skill (decision #121) — an explicit research → weather-check → pacing-review → write DAG, with the pacing reviewer able to hand back to the writer node. Swarm would be the wilder, more chaotic option worth trying specifically to feel a genuinely less deterministic architecture and its own dedicated safety-rail mechanisms. |
| 4 | AgentCore Evaluations | AWS-managed, trace-based LLM-as-judge evaluation, automatically surfaced in the CloudWatch GenAI Observability dashboard (agent-level trend view, trace-level score + judge-reasoning drill-down). | Directly extends this project's already-built observability investment (§2h logs/traces, §2k alarms/dashboard) with the one layer explicitly deferred there: "is the agent actually good," not just "is it up." Smallest lift of this whole list — mostly CDK/config, similar footprint to the `TravelAgentDevOpsStack` integration (§2j) — a new fully-managed AWS service wired in, not new application code. Distinct from Strands Evals (#1): this is live-production-trace judging; Evals is pre-deploy/offline testing. The two are complementary, not redundant. |
| 5 | Standalone Nova Sonic voice agent exploration (`strands.experimental.bidi`) | A genuinely separate, local-only voice assistant using `BidiAgent`/`BidiAudioIO`/`BedrockNovaSonicModel`, talking directly to Bedrock's `InvokeModelWithBidirectionalStream` API with local AWS credentials — **not** an evolution of the existing web UI/SSE transport, and not a general "interrupt a text response" feature (researched in depth 2026-09-12; corrects the original, less-informed framing of this idea). Voice Activity Detection-based interruption (barge-in) is handled automatically by the provider; the agent's `run(inputs=[...], outputs=[...])` model is fundamentally different from a request-then-stream-response loop. | Deliberately scoped down from "rethink the web UI transport end to end" once research surfaced four real architectural mismatches with this project's existing infrastructure: (1) it's audio-native, not text — a real browser voice UI (mic capture, WebSocket/WebRTC transport, speaker playback) is a materially bigger lift than a transport swap; (2) Nova Sonic's bidirectional stream cannot be routed through this project's Gateway `bedrock-mantle` inference target at all — decision #158's "all inference through Gateway" architectural commitment would need an explicit, acknowledged exception; (3) `InvokeModelWithBidirectionalStream` requires standard AWS credential-based auth and explicitly cannot be used with Bedrock API keys, a different auth story from this project's JWT-bearer-token-only Runtime (decision #75/§2f); (4) `BidiAudioIO`'s PyAudio-based mic/speaker handling is meant for local dev use (a headset is required to avoid feedback loops) and has no obvious analog inside the headless ECS Fargate web server. A standalone local CLI script — direct Bedrock access with local credentials, a couple of tools wired in directly rather than through the Gateway — sidesteps all four mismatches and lets the genuinely novel parts (VAD-based interruption, live transcripts, simultaneous audio+tool-call streaming) actually be exercised, rather than forcing a production-architecture integration the feature doesn't fit yet. Confirmed via AWS's regional-availability data that Nova Sonic is available in this project's own region (`us-east-1`), so no region change would be needed for this scoped-down version. |
| 6 | A2A protocol (`strands.agent.a2a_agent`) | Agent-to-Agent protocol client/server support in Strands. | This project already brushed up against A2A once, unresolved: §2j's AWS DevOps Agent integration found its `/a2a/` endpoint consistently rejected SigV4 signing with a generic error, while the sibling `/mcp` endpoint worked correctly on the first attempt with identical signing code — never root-caused, since `/mcp` was sufficient for that phase's own goal. Exposing this project's own agent as an A2A server, or building a real A2A client against another agent, would be a legitimate way to finally resolve that lingering mystery from both sides of the protocol. |
| 7 | AgentCore Identity beyond OBO | Identity's broader per-user OAuth2 credential management for *third-party* (non-AWS-internal) APIs — consent flows, token storage, refresh — not just the Runtime→Gateway On-Behalf-Of exchange this project already built (§2g). | This project has only exercised one narrow slice of Identity (internal service-to-service OBO). The broader "agent acts on a specific user's behalf against a real third-party API they've separately authorized" pattern (e.g. a real calendar or booking API) is architecturally the natural next chapter of the same Identity investment already made, and was never attempted. |
| 8 | AgentCore Gateway Policy Engine / claim-based rate limiting and RBAC | Full Cedar-policy-based, per-user/per-role tool authorization on the Gateway, beyond the single flat TPM rate limit already built (decision #115) and the plumbing-only JWT propagation already built (§2g decision #87/§2i decision #112, both explicitly deferred as fast-follows). | This is genuinely half-built already — the propagated `sub` claim exists end-to-end and is verified working (decision #171's live trace confirmation), but nothing reads it for authorization decisions yet. The lowest-new-infrastructure item on this list in one sense (the hard plumbing problems are already solved) but the actual policy-design work (what should differ per user/role) was never done, since this project has no user tiers today (decision #115's own note). |
| 9 | Strands hooks beyond `AfterModelCallEvent` | The full hook lifecycle (before/after tool call, before/after invocation) — this project uses exactly one hook today, for cache-usage accounting (decision #170). | A smaller, entirely in-process (no new AWS infrastructure) way to explore the same "observe/intervene in the agent loop" idea as #8, without needing Gateway Policy Engine plumbing — e.g. a before-tool-call veto hook, or a dedicated audit-log sink for every tool invocation, built purely in Strands. Good low-risk entry point if #8's IAM/Cedar scope is more than wanted for a first pass at this general idea. **Done, see §2q** — `BeforeInvocationEvent`/`BeforeToolCallEvent`/`AfterToolCallEvent` all now log-only guardrail-check content, built as the delivery mechanism for item #10 below. |
| 10 | Bedrock Guardrails integration | Content-moderation/guardrail layer (denied topics, PII redaction, etc.) applied to model calls — via Strands' "Agent Control" runtime-guardrails integration or a direct Bedrock Guardrails resource. | The one item on this list motivated by a real, acknowledged gap rather than pure novelty: this is a consumer-facing chat agent (§2b's hosted, multi-user web UI) with zero content-moderation layer today. Worth building both as a security lesson and because it's a legitimately missing piece for anything beyond a personal single-user project. **Done, see §2q** — a real `AWS::Bedrock::Guardrail` (ContentFilter + PROMPT_ATTACK) called via `ApplyGuardrail` from the item #9 hooks above, log-only; PII/denied-topics/contextual-grounding categories and actual enforcement remain explicit, tracked follow-ups, not built in this pass. |
