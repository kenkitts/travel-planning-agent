# Travel Planning Agent — Design Decisions

Status: Approved (interview complete, 2026-08-17)
Owner: kenkitts

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
| 9 | Output format | Plain conversational markdown text (v1) | No UI/client exists yet; structured JSON output is deferred until there's a real consumer, to avoid premature schema design. |
| 10 | Testing/invocation clients | AgentCore console test chat + local Python CLI REPL script | Console for zero-setup manual smoke tests; CLI script for faster interactive dev-loop testing. |
| 11 | IaC | AWS CDK (Python) | AgentCore has CDK L2 constructs (`aws_cdk.aws_bedrockagentcore`: `Memory`, `Gateway`, `GatewayTarget.ForLambda`, `CfnRuntime` — verified in CDK docs). Reproducible, version-controlled. |
| 12 | Application language | Python (agent code, Lambda handlers, CDK) | Strands SDK is Python-native; single-language stack avoids unnecessary context switching for two simple Lambda handlers. |
| 13 | Project type | Personal AWS account project, standard OSS tooling | Not a Brazil/internal-Amazon build. Plain `cdk deploy`, plain git repo. |
| 14 | Location & region | New local repo, `us-east-1` | Verified via `aws___get_regional_availability`: AgentCore, Location Service both available in `us-east-1` and `us-west-2`. us-east-1 chosen as default/most common launch region. |
| 15 | Auth for invoking agent | IAM auth only | Matches CLI/console testing tools; no separate identity system needed since there's no UI yet. Open/no-auth explicitly rejected — AgentCore invocations cost real money and shouldn't be internet-exposed. |
| 16 | Weather provider | Open-Meteo | Free, no API key, global coverage (rules out US-only NWS), avoids Secrets Manager setup needed for OpenWeatherMap's key. |
| 17 | Cost posture | No hard budget constraint | Optimize for correct architecture over cost minimization; still defaulted to free-tier services where reasonable (Open-Meteo). |
| 18 | Testing strategy | Unit tests only (Lambda handlers + deterministic logic) | Itinerary output is LLM-generated/non-deterministic — integration tests would only verify "didn't crash," not quality. Manual testing (via #10) covers end-to-end behavior. |

## 3. Architecture Overview

```
User (CLI REPL or AgentCore console test chat)
        │  IAM-authenticated invoke
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
   Plain markdown itinerary response
```

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
- Structured JSON itinerary output / frontend or export (PDF, calendar)
- Cognito/JWT auth for non-AWS-credentialed clients
- Integration/end-to-end automated tests
- Cost budgets/billing alarms
