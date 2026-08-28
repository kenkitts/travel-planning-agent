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

## 2a. JWT Authorization (added 2026-08-26, supersedes decision #15)

Status: Approved (interview complete, 2026-08-26)

Decision #15 (IAM auth only) and decision #5's explicit exclusion of
"Cognito/JWT auth for non-AWS-credentialed clients" are superseded by this
section. The trigger: a small group of known users (beyond just the
original author) needs to use the CLI/web UI, and IAM/SigV4 with local AWS
credentials doesn't scale to "share this with a few people" without also
sharing AWS credentials — undesirable.

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| 26 | Auth cutover scope | Full replacement: AgentCore Runtime's authorizer switches from `RuntimeAuthorizerConfiguration.using_iam()` to `.using_jwt(...)` (custom JWT authorizer). No dual IAM+JWT path. | AgentCore Runtime supports one authorizer configuration at a time. Once JWT is configured, IAM/SigV4 calls to `InvokeAgentRuntime` no longer work for anyone — both clients (CLI and web UI) must move to bearer tokens together. |
| 27 | Token issuer | Okta (existing org) — a **new, dedicated Okta application** registered specifically for the Travel Agent, distinct from the author's pre-existing `okta-claude-code-token-helper` app (used for an unrelated Claude Code LLM gateway). | Reusing the same Okta app/client ID would let a token minted for one system silently authenticate to the other — undesired coupling. A separate app keeps blast radius and scope (audience, allowed scopes) contained to this project. |
| 28 | Users | Small, known group of individuals, each with their own Okta identity — not a single personal token, not open self-service signup. | Matches the actual near-term need (share access with a few specific people) without taking on a public-facing identity system. |
| 29 | Clients migrated | Both `cli/chat.py` and `web/server.py` move from IAM/SigV4 (via `boto3`'s default credential chain) to JWT bearer tokens on the `InvokeAgentRuntime` call. Neither client keeps an IAM fallback. | Consistent with decision #26 — IAM stops working for any caller once the Runtime's authorizer is switched, so there is no such thing as "just migrate one client." |
| 30 | Web UI hosting scope | Unchanged from decision #19: still local-only, single machine per user, binds `127.0.0.1`, no new hosting infrastructure. Each of the small group of users runs their own local copy and logs in with their own Okta identity against the one shared deployed Runtime. | Hosting the web UI somewhere network-reachable (TLS, a domain, cookie/session handling, public exposure) is a materially different and larger project than "add JWT auth" — explicitly deferred as a separate future effort, not bundled into this change. |
| 31 | Memory scoping / `actor_id` derivation | `actor_id` for AgentCore Memory is derived **server-side**, inside `agent/agent.py`, from the verified JWT's `sub` claim (Okta's stable, immutable per-user identifier) — not from a client-supplied `--actor-id` flag or any other client-controlled value. No override/impersonation escape hatch. | The whole point of adding real per-user auth is that one user's long-term Memory (preferences, conversation history) can't be read or written under another user's identity just by passing a different flag. `sub` (vs. `email`) is used because it's guaranteed stable even if a user's Okta profile email changes later — using `email` as the key would orphan old Memory records on any such change. An override flag "for testing" was explicitly rejected as a foot-gun that reintroduces the same spoofing risk it's meant to close; testing as a different persona means logging into Okta as that user, not passing a flag. |
| 32 | Token acquisition mechanism | Both clients acquire a bearer token by invoking the author's existing `~/okta-claude-code-token-helper/okta-claude-code-token.py` script as a **subprocess, once per request/turn** (not cached independently by the Travel Agent clients), reusing that script's own caching/silent-refresh/interactive-login/cross-process-locking behavior. Configuration (`OKTA_ISSUER`, `OKTA_CLIENT_ID`, `OKTA_SCOPES`, `OKTA_REDIRECT_PORT`) for the Travel Agent's dedicated Okta app (decision #27) lives in a new `travel-planning-agent/.env` (gitignored; `.env.template` committed) and is passed to the subprocess as real environment variables — not via that script's own `.env` file, which is reserved for the pre-existing Claude Code gateway app and would otherwise force a shared/renamed dotenv file the script doesn't natively support. | Reimplementing OAuth 2.0 Authorization Code + PKCE, token caching, silent refresh, and cross-process locking from scratch for this project would duplicate a script that already does exactly this and is already trusted for a different real use case. Re-invoking per turn (rather than the client caching a token itself) means a multi-hour session keeps working transparently across token expiry without any client-side refresh logic — the helper script already handles "valid → instant," "expired but refreshable → silent refresh," and "neither → interactive login." |
| 33 | Token cache collision | Accepted: the helper script's token cache path (`~/.cached-credentials/token-cache.json`) is a hardcoded constant, shared regardless of which Okta app's config is passed in via environment variables. Logging into the Travel Agent's Okta app evicts the Claude Code gateway's cached token and vice versa — only one is "logged in" at a time; switching tools forces a fresh interactive login for whichever wasn't used most recently. | Making the cache path configurable would require modifying the shared helper script (adding e.g. an `OKTA_CACHE_PATH` override) — explicitly deferred; the author chose to accept the collision for now rather than take on that (small, but nonzero) maintenance change. |
| 34 | Claim propagation mechanism | The Runtime is configured with `requestHeaderAllowlist: ["Authorization"]` alongside the `customJwtAuthorizer`. This does **not** happen automatically from JWT authorizer configuration alone — confirmed against AWS's own docs (`inbound-jwt-authorizer.html`, `runtime-header-allowlist.html`): the authorizer validates the token's signature/expiry/issuer/audience/client/scopes at the edge before the agent runs, but claims are not injected into the invocation context as a separate, pre-parsed field. The allowlisted raw `Authorization` header is forwarded to `agent/agent.py` via `context.request_headers`, and the agent code itself is responsible for extracting `sub` from it. | This is a real, small implementation task (decode a JWT payload inside the agent), not a built-in AgentCore feature — worth recording explicitly since it was initially assumed to be automatic during design, and a future reader of `agent.py` should understand why this decoding step exists there rather than being handled by the Runtime. |
| 35 | JWT decoding in agent code | `PyJWT`, decode-only (`jwt.decode(token, options={"verify_signature": False})`), added to `agent/requirements.txt`. No signature re-verification inside the agent. | The Runtime's `customJwtAuthorizer` has already cryptographically verified the token (signature, expiry, issuer, audience/client/scopes) before the agent process ever runs — decoding again inside the agent is purely to read the `sub` claim out of a token already known to be valid, not a second trust boundary. Hand-rolling base64url + JSON decoding was considered and rejected: it's a small amount of code but has enough real edge cases (padding, encoding) that a well-known, actively maintained library is preferable to reinventing it for no real savings. |
| 36 | Gateway observability | Added CloudWatch application logs + X-Ray traces for `TravelAgentGatewayStack`'s Gateway (`gateway_stack.py`'s `_configure_observability()`): a dedicated log group, a delivery source/destination/delivery pair each for `APPLICATION_LOGS` and `TRACES`, per AWS's documented SDK pattern for gateway resources. | Confirmed via AWS docs that, unlike Runtime, AgentCore does not provision any log/trace destination for Gateway resources automatically — without this, a failing tool call surfaces to the end user only as an opaque "An internal error occurred" with no way to see the real upstream error, target name, or request/response bodies. Enabling this let a real Gateway target failure be root-caused directly from CloudWatch logs (specific error message, failing target, trace/span IDs) rather than guessed at. |

## 2b. Hosted Web UI on ECS Fargate (added 2026-08-27, supersedes decision #19/#26-35's Runtime-auth half)

Adding `TravelAgentWebStack` — a shared, always-on hosted deployment of the
web UI, as an alternative to everyone running `web/server.py` locally
(decision #19, still supported and unchanged for local use). This section
also reverts the Runtime-auth half of decisions #26-35 (the Okta-JWT
bearer-token cutover) back to IAM — decision #37 explains why — while
decisions #26-35's other artifacts (the ALB's own separate OIDC login,
`sanitize_actor_id()`'s rationale, etc.) carry forward in adapted form.

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| 37 | Runtime auth model, reverted | `RuntimeAuthorizerConfiguration.using_iam()` — full reversion of decisions #26-35's Okta-JWT bearer-token cutover. Every caller (the CLI; now also `TravelAgentWebStack`'s ECS task) invokes `InvokeAgentRuntime` with its own AWS credentials, SigV4-signed. `actor_id` moves from a server-side-decoded JWT `sub` claim to an explicit field in the invocation payload (`agent/agent.py`'s `get_actor_id(payload)`) — the caller is trusted to supply the correct value, the same way it's already trusted with IAM access to invoke the Runtime at all. | The entire reason decisions #26-35 added JWT auth was to give the CLI and local web UI a way to distinguish real human identities with no other authentication layer available to them. Once `TravelAgentWebStack`'s OIDC-authenticated ALB exists, that job is already done *before* a request ever reaches this project's own code — the ALB is a real, independently-operated identity boundary, and the Runtime's own JWT authorizer became redundant plumbing duplicating a check the ALB already performs. Worse, the JWT-bearer-token path was *only* usable by a caller that could complete an interactive Okta login (the CLI's local subprocess flow) — it had no answer for how a non-interactive ECS task would authenticate as a specific human user, and "the container performs a service-to-service OAuth client-credentials grant just to talk to its own account's Runtime" would have been meaningfully more complex than simply trusting the task's own IAM identity, which every other AgentCore-Runtime-calling component in this project already does. IAM auth is not a security regression here: the caller that can reach the Runtime with valid credentials is either (a) this project's own ECS task, sitting behind the ALB's real OIDC gate, or (b) a local CLI user who already has full IAM access to this account's resources — there is no untrusted public path that can supply an arbitrary `actor_id`. |
| 38 | Web UI hosting: compute, load balancing, and auth | ECS Fargate (0.5 vCPU/1GB per task, autoscaling 1-3 on CPU 60%) behind an internet-facing Application Load Balancer with an `authenticate-oidc` listener action (Okta as the IdP) placed in front of a `forward` action to the Fargate target group. `web/server.py`'s `actor_id_from_oidc_header()` independently re-verifies the ALB's signed `x-amzn-oidc-data` header (ES256, fetching the signing public key from ALB's per-region `public-keys.auth.elb.<region>.amazonaws.com/<kid>` endpoint, and checking the JWT header's `signer` field against this exact ALB's ARN) before trusting any claim in it — per AWS's own documented security requirement, not an optional hardening step. | User-specified (compute type, autoscaling, and OIDC-on-ALB were all given constraints up front, not derived). Independent signature re-verification (rather than trusting the header blindly) matters specifically because a misconfigured security group, or any bug that let a request reach the Fargate task without actually passing through the ALB's listener rule, could otherwise let a caller forge an arbitrary `actor_id`/`sub` claim and read or write another user's long-term Memory — the same class of risk `sanitize_actor_id()` was originally added for in decisions #26-35, now defended at the transport-trust layer instead of (or in addition to) the character-set layer. |
| 39 | Web UI hosting: networking | A new, dedicated VPC (2 AZs, public + private-with-egress subnets, one NAT Gateway per AZ) — not a pre-existing/shared VPC. Fargate tasks sit in the private subnets with no public IP, reachable only from the ALB's security group; the ALB itself sits in the public subnets, internet-facing. | Matches this project's existing pattern of every stack provisioning its own resources rather than depending on external infrastructure (no other stack in this project references a pre-existing VPC). Keeping compute off the public internet even though the security group would already block unsolicited inbound traffic is the standard defense-in-depth posture for a real multi-tenant, internet-reachable service — a materially different risk profile than the local-only web UI (decision #19) or the CLI, both of which run entirely under the operator's own control. |
| 40 | Web UI hosting: TLS and DNS scope | The caller supplies an existing ACM certificate ARN (`WEB_CERTIFICATE_ARN`); this stack does not provision, import, or validate a certificate itself, and does not create any Route 53 record — the ALB's own generated DNS name is used as-is, and pointing a real domain at it is left entirely to the caller. | User-specified: explicitly out of scope for this stack, to avoid this CDK app guessing at a domain/hosted-zone configuration it has no visibility into (a wrong guess here has real blast radius — a misconfigured Route 53 record or a wrongly-scoped ACM certificate request touches shared DNS/PKI state, not just this project's own resources). |
| 41 | Container build and deploy mechanism | `ecs.ContainerImage.from_asset()` — CDK builds and pushes the Docker image (from a new `web/Dockerfile`) to an auto-created ECR repository as part of `cdk deploy`, with the same manual (non-pipeline) deploy workflow used by every other stack in this project. The asset's Docker build context is the whole repo root (needed since the Dockerfile `COPY`s both `web/` and `cli/agent_client.py`, which are siblings, not nested under one directory), with explicit `exclude` patterns for `cdk/cdk.out`, `.venv`, `.git`, and `__pycache__` — omitting these caused a real, confirmed `ENAMETOOLONG` failure during this stack's own development, since `cdk synth` re-copies its own previous output into the new asset staging directory without them (a self-referential recursive copy). | Matches `RuntimeStack`'s existing `AgentRuntimeArtifact.from_code_asset()` pattern for `agent/` — one `cdk deploy` builds and deploys everything, no separate manual image build/push step or CI/CD pipeline, consistent with every other stack's deploy story in this project (user-specified: explicitly deferred a real pipeline as future scope, not needed for a small-group internal tool). |
| 42 | Web UI hosting: IAM scope | The ECS task role is granted only `memory.grant_full_access()` and `runtime.grant_invoke()` — scoped to the exact Memory and Runtime resource ARNs this deployment uses, no wildcard `bedrock-agentcore:*` grant. | User-specified, and consistent with this project's existing least-privilege convention (e.g. `GatewayStack`'s `_grant_web_search_invoke()` scoping to a literal resource ARN rather than a service-wide wildcard) — this task role is a real blast-radius boundary for a genuinely multi-tenant, internet-reachable service, unlike the CLI's use of the operator's own broader local credentials. |
| 43 | Web UI hosting: sizing/traffic assumptions | Fargate: 0.5 vCPU/1GB per task, autoscaling 1-3 tasks on CPU utilization target 60%. ALB idle timeout raised to 180s (from the 60s default); Fargate `health_check_grace_period` matched to the same 180s. Deployment configured with `circuitBreaker` (rollback on failure) and `minHealthyPercent=100` (matches `desired_count=1` — without this, a single-task deployment could briefly drop to zero running tasks mid-rollout). | User-specified: a genuinely multi-tenant identity model (decision #38's per-user OIDC-derived `actor_id`) but explicitly *not* a high-traffic-volume design target — sized conservatively and easy to scale up later via a CDK property change, not because of any measured or expected load. The 180s idle-timeout/grace-period value is not arbitrary: real itinerary-generation turns have been observed taking up to ~56s (see PLAN.md Phase 7's `MaxTokensReachedException` fix notes), close enough to the ALB's 60s default that a long, tool-heavy turn could otherwise have its connection killed by the ALB mid-stream. |
| 44 | Web UI hosting: CI/CD scope | Plain manual `cdk deploy TravelAgentWebStack`, same as every other stack — no CodePipeline/Amazon Pipelines automation. | User-specified: explicitly deferred as separate, larger scope (source triggers, approval gates, rollback strategy) that wasn't asked for — matches this project's existing all-manual-deploy pattern for the four base stacks. |

Explicitly still true, carried over from decisions #26-35: the ALB's OIDC
login (decision #38) is a genuinely separate Okta app/config from anything
the CLI uses (decision #27's "dedicated Okta app" framing, now applied to
the ALB instead of the Runtime) — the CLI still has no ALB in front of it
and never touches Okta at all after this change. `sanitize_actor_id()`'s
rationale (an OIDC `sub` claim is not guaranteed to satisfy AgentCore
Memory's `actorId` character-set pattern; a real Okta org's `sub` was an
email address) is unchanged — it now runs inside `web/server.py`'s
`actor_id_from_oidc_header()` on the ALB's claims instead of on a
Runtime-forwarded bearer token, and `agent/agent.py`'s `get_actor_id()`
also sanitizes defensively regardless of caller.

Explicitly still true, carried over from the superseded decisions:
IAM auth is **removed**, not kept as a parallel option (unlike a typical
"add an alternative" change) — see decision #26. The web UI's local-only,
single-user, no-hosting scope (decision #19) is explicitly **not**
reopened by this change — see decision #30.

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
- A hosted/multi-user frontend for the web UI (decision #19/#30 — still
  local-only per user, even after JWT auth (§2a) lets multiple distinct
  people use it)
- Integration/end-to-end automated tests
- Cost budgets/billing alarms

~~Cognito/JWT auth for non-AWS-credentialed clients~~ — **implemented, see
§2a.** IAM auth (decision #15) has been fully replaced by Okta-issued JWT
bearer tokens (decisions #26–35), not Cognito as originally anticipated
here.
