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

## 3. Architecture Overview

```
User
  │
  ├─ CLI REPL (cli/chat.py) ──────────┐
  ├─ Local web UI (web/server.py) ────┤  boto3 bedrock-agentcore
  └─ AgentCore console test chat      │  InvokeAgentRuntime (IAM/SigV4),
                                       │  streamed as SSE
                                       ▼
                          AgentCore Runtime  ── hosts ──▶  Strands Agent (Python, Claude Sonnet via Bedrock)
                                       │                              │
                                       │                              ├─ reads/writes ──▶ AgentCore Memory
                                       │                              │                    (short-term: session state)
                                       │                              │                    (long-term: user preferences)
                                       │                              │
                                       │                              └─ MCP tool calls ──▶ AgentCore Gateway
                                       │                                                          │
                                       │                                        ┌─────────────────┼─────────────────┐
                                       │                                        ▼                 ▼                 ▼
                                       │                                 Web Search        Lambda: Weather    Lambda: Places
                                       │                                 (managed           (Open-Meteo API)   (Amazon Location
                                       │                                  connector)                           Service wrapper)
                                       ▼
                       Streamed events: reasoning / text / tool_use /
                       tool_result / done / error — CLI prints only the
                       final markdown text; the web UI renders text live
                       and (optionally) all events in a diagnostic panel.
```

The CLI and web UI share one client module (`cli/agent_client.py`) for
building runtime session IDs and consuming the SSE stream, so both clients
see identical event data — they only differ in how much of it they render.
The web UI is a local-only process (see decision #19); it is not a fifth
CDK stack.


## 4. Conversation Flow (v1)

1. User makes a vague request ("plan a trip to Kyoto").
2. Agent asks clarifying questions as needed: dates, trip length, budget,
   pace (relaxed/packed), interests, travelers/constraints. Uses short-term
   memory to avoid re-asking within a session; checks long-term memory for
   returning users' known preferences.
3. Once enough info is gathered, agent:
   - Calls Web Search for current info (events, closures, must-see spots).
   - Calls Location Service tool to geocode candidate places and sequence
     them into a sensible daily route (minimize backtracking).
   - Calls Weather tool (Open-Meteo) for the trip date range; adjusts
     outdoor/indoor activity mix if rain/extreme temps are forecast.
4. Agent renders a day-by-day itinerary as markdown chat text.
5. Long-term memory strategy extracts durable preferences from the
   conversation (e.g. "prefers walking tours," "traveling with kids") for
   future sessions.

## 5. Out of Scope (v1) — Explicit Fast-Follows

- Booking/payment integrations (flights, hotels, activities)
- Structured JSON itinerary output / export (PDF, calendar) — the local web
  UI (decision #19) is a manual-testing/demo surface, not this fast-follow;
  it still only renders the same plain markdown output as the CLI/console.
- A hosted/multi-user frontend, or any auth system beyond IAM (the local
  web UI is explicitly single-user and local-only — see decision #19)
- Cognito/JWT auth for non-AWS-credentialed clients
- Integration/end-to-end automated tests
- Cost budgets/billing alarms
