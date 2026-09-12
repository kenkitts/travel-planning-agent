# Travel Planning Agent — Feature Checklist

A running checklist of features and capabilities: what's implemented and
live, what's on the backlog, and what was considered/built and then
explicitly rejected or reverted. See `DESIGN.md` for full rationale on
every decision referenced here, and `PLAN.md` for the phased build
history.

**Legend**: ✅ implemented and live · ⬜ not yet implemented (backlog/
deferred) · ❌ considered/built, then explicitly rejected or reverted

## Core agent capabilities
- ✅ Conversational itinerary building (clarifying questions before generating)
- ✅ Web Search grounding (AgentCore managed connector)
- ✅ Weather grounding (Open-Meteo via Lambda)
- ✅ Places/maps grounding (Amazon Location Service via Lambda)
- ✅ AgentCore Memory — short-term (session state)
- ✅ AgentCore Memory — long-term (cross-session traveler preferences)
- ✅ Plain markdown itinerary output
- ⬜ Structured JSON itinerary output / export (PDF, calendar)
- ❌ Booking/payment integrations (flights, hotels, activities) — explicitly out of scope (v1 scope decision, never revisited)

## Model & routing
- ✅ Claude Sonnet via Bedrock
- ✅ Model routing (`ModelRouter`/`ClassifierStrategy` — cheap/Haiku vs. capable/Sonnet candidates)
- ✅ Extended thinking / adaptive reasoning surfaced in diagnostics
- ✅ Anthropic-native prompt caching (system prompt + tools, both serving candidates)
- ⬜ 1-hour cache TTL comparison (explicitly skipped by user request; 5-minute default kept)
- ✅ Gateway-routed inference (`bedrock-mantle` connector) — now mandatory, no direct-Bedrock fallback
- ✅ Per-user token-per-minute rate limiting on inference (Gateway `CfnGatewayRateLimit`)
- ✅ Graceful throttle handling (`ModelThrottledException` → friendly chat message)
- ✅ Agent loop iteration cap (`Limits.turns`)

## Context & memory management
- ✅ Context compaction (`SummarizingConversationManager`)
- ✅ Date-grounding via `ContextInjector` (not baked into cached system prompt)
- ✅ Conversation history sidebar (list/rename/delete, backed by AgentCore Memory)
- ⬜ Search across conversation history

## Tools & extensibility
- ✅ MCP tool client (Gateway-hosted tools)
- ✅ AgentCore Code Interpreter (itinerary arithmetic/date math)
- ✅ Strands AgentSkills plugin (`trip-pacing` procedural knowledge)
- ⬜ AgentCore Browser tool (managed remote browser)
- ⬜ Multi-agent orchestration — Graph pattern
- ⬜ Multi-agent orchestration — Swarm pattern
- ⬜ A2A protocol (agent-to-agent client/server)

## Identity & auth
- ❌ Cognito/JWT auth directly on the Runtime for non-AWS-credentialed clients — implemented, then reverted once the web ALB became the real identity boundary (decision #37)
- ✅ Okta OIDC login (app-level, not ALB-level)
- ✅ KMS-encrypted session cookies
- ✅ RFC 8693 token exchange: web server → Runtime (per-user JWT)
- ✅ RFC 8693 On-Behalf-Of exchange: Runtime → Gateway (per-user JWT)
- ✅ AgentCore Identity for OBO token exchange
- ⬜ AgentCore Identity beyond OBO (third-party OAuth2 credential management, consent flows)
- ✅ Logout (session clear + Okta refresh-token revocation)
- ❌ Full Okta SSO sign-out (`/v1/logout`) — considered, explicitly scoped out (decision #144)
- ✅ Unauthenticated landing page
- ✅ Identity label in UI ("Signed in as ...")

## Security / governance
- ✅ Gateway JWT authorizer (per-user identity propagation)
- ⬜ Gateway Policy Engine / claim-based RBAC and per-role rate limiting
- ⬜ Bedrock Guardrails / content moderation
- ⬜ Strands hooks beyond `AfterModelCallEvent` (before/after tool-call hooks, audit logging)
- ✅ WAF on the internet-facing ALB
- ✅ TLS 1.2/1.3-only ALB policy
- ✅ Gateway service-role trust-policy hardening (confused-deputy fix)

## Hosting & infra
- ✅ Hosted web UI (ECS Fargate + ALB, autoscaling)
- ❌ Local-only single-user web UI as the primary usage mode — superseded once the hosted web UI shipped (still technically runnable for dev/testing only)
- ❌ Local CLI REPL client — built, then removed entirely once the web UI became the only supported client (decision #48)
- ⬜ CI/CD pipeline for `TravelAgentWebStack` (explicit non-goal, plain manual `cdk deploy` only)

## Observability
- ✅ Gateway application logs + X-Ray traces
- ✅ Structured logging across both tool Lambdas
- ✅ Log retention (30 days) across all log groups
- ✅ Web UI tracing via ADOT/OpenTelemetry
- ✅ 100% X-Ray sampling rule
- ✅ CloudWatch Alarms (9, SNS-notified) across Lambda/ECS/ALB/AgentCore
- ✅ CloudWatch Dashboard
- ✅ AWS DevOps Agent integration (on-demand investigation)
- ⬜ AgentCore Memory instrumentation (spans/logs/metrics — explicitly deferred, decision #140)
- ⬜ AgentCore Evaluations (live-trace LLM-as-judge, CloudWatch-surfaced)
- ⬜ Strands Evals (offline agent-evaluation harness)

## Testing
- ✅ Unit tests (Lambda handlers, agent helpers, web server) — 222/222 passing
- ⬜ Automated integration/end-to-end tests (explicit non-goal — itinerary output is non-deterministic)

## Cost/budget
- ✅ AWS DevOps Agent cost guardrail ($100/month budget with alerts)
- ⬜ Billing budgets/alarms for the rest of the stack (explicit non-goal)

## Streaming / interaction model
- ✅ SSE streaming (text, reasoning, tool calls, cache usage, routing decisions)
- ⬜ Standalone Nova Sonic voice agent exploration (`strands.experimental.bidi`, local CLI — not a web UI transport change)
