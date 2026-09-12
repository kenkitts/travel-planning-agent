"""Travel Planning Agent — Strands Agent hosted on Amazon Bedrock AgentCore Runtime.

Wires together:
  - Claude Sonnet and Haiku via the Gateway's own inference target
    (strands.models.anthropic.AnthropicModel, pointed at the Gateway's
    /inference/v1/messages path) — the sole model-call path, for
    centralized governance/rate-limiting. There is no direct-bedrock-
    runtime fallback: build_model_router() raises RuntimeError if the
    Gateway path can't be used (see DESIGN.md's "Gateway-routed
    inference mandatory" decision). A Strands ModelRouter
    (strands.models.routing) with a ClassifierStrategy picks between a
    cheap Haiku serving candidate and the Sonnet serving candidate per
    turn, based on whether the request looks like it needs tool use/
    multi-step reasoning — see build_model_router()'s docstring for the
    full design and DESIGN.md's model-routing decision. This is an
    educational feature (exercising Strands' ModelRouter/ClassifierStrategy
    API directly), not a load-bearing production capability. Both serving
    candidates and the classifier itself route through the same Gateway
    inference target, authenticated with the same OBO-cached Gateway
    token as the tools path.
  - Tools from the AgentCore Gateway (Web Search + weather + places), over
    MCP — SigV4 (IAM) request signing by default, or a per-user JWT
    bearer token obtained via RFC 8693 On-Behalf-Of token exchange when
    the Gateway's own authorizer is switched to JWT (DESIGN.md's Phase 3
    decision; see build_mcp_client() below). Under IAM auth, the
    Runtime's execution role has bedrock-agentcore:InvokeGateway, but that
    permission only takes effect if the outbound request is actually
    SigV4-signed — the Gateway otherwise responds 401 Unauthorized
    (confirmed against a real deployment; the Runtime does not sign
    Gateway calls automatically).
  - AgentCore Memory (short-term conversation history + long-term traveler
    preferences and session summaries) via the Strands session_manager
    integration, so the agent recalls context within a session and across
    separate trips for the same traveler.
  - A SummarizingConversationManager (proactive_compression=True, pin_first=6)
    to bound in-session context growth for long-running conversations — see
    build_conversation_manager() for why this replaces Strands' own silent
    default.

The entrypoint (invoke()) is an async generator: it streams labeled
diagnostic events (reasoning/text/tool_use/tool_result/routing/done/error)
as an SSE response, one per turn of agent.stream_async(). BedrockAgentCoreApp
auto-detects the async-generator return and wraps it as a
text/event-stream StreamingResponse — see stream_agent_turn() for the
event-shape translation and MaxTokensReachedException handling.

Configuration is read from environment variables set by RuntimeStack:
  GATEWAY_URL               - the AgentCore Gateway's MCP endpoint
  MEMORY_ID                 - the AgentCore Memory resource ID
  AWS_REGION                - region for the Memory client (falls back to boto3 default)
  MODEL_ID                  - Bedrock model ID for Claude Sonnet (has a sane default)
  HAIKU_MODEL_ID            - Bedrock model ID for Claude Haiku, used as both the
                              ModelRouter's cheap serving candidate and its
                              classifier model (has a sane default)
  GATEWAY_OBO_PROVIDER_NAME - name of the AgentCore Identity OAuth2 credential
                              provider used for the Gateway's RFC 8693 On-Behalf-Of
                              token exchange (see build_mcp_client() below); empty
                              string if GatewayStack's JWT authorizer isn't configured
  GATEWAY_INFERENCE_URL     - base URL of the Gateway's inference target
                              (".../inference"); required — build_model_router()
                              raises RuntimeError if this is unset, since
                              there is no other model-call path (see
                              build_model_router() below)

Auth, Runtime inbound: IAM/SigV4 by default (DESIGN.md decision #37), or JWT
Bearer Token when RuntimeStack's Okta config is set (DESIGN.md's Phase 2
decision). Phase 3 changed how actor_id is derived once JWT inbound auth
is active: get_actor_id() below now reads the caller's verified `sub`
claim from the inbound Authorization header (via
BedrockAgentCoreContext.get_request_headers()) instead of trusting a
plain actor_id string in the invocation payload — restoring decision #31's
original "derive server-side from a verified token, never from client
input" stance for Memory scoping, now that a real per-request identity
(the JWT itself) is available again. Falls back to the payload's actor_id
field only when running under IAM inbound auth (no bearer token exists to
read a `sub` from in that mode) — see get_actor_id()'s own docstring for
the full trust rationale under each mode.

Auth, Runtime -> Gateway (Phase 3, added after Phase 2): the Gateway's own
inbound authorizer switches from AWS IAM/SigV4 to JWT Bearer Token when
GatewayStack's Okta config is set (DESIGN.md's Phase 3 decision) — the
same hard, mutually-exclusive IAM-vs-JWT switch already confirmed for the
Runtime's own inbound auth in Phase 2, now also true for the Gateway.
When JWT is configured, build_mcp_client() below performs an RFC 8693
On-Behalf-Of token exchange via AgentCore Identity (bedrock_agentcore.
identity.auth.IdentityClient, ON_BEHALF_OF_TOKEN_EXCHANGE flow) —
exchanging the Runtime's own automatically-delivered workload access
token (which itself carries the caller's original inbound JWT as its
subject — see RuntimeStack's docstring) for a Gateway-audienced JWT, then
presents that as a plain Bearer token to the Gateway's MCP endpoint
instead of SigV4-signing the request. This is a materially different
mechanism from Phase 2's own token exchange (web/auth.py's
exchange_token_for_runtime(), a hand-rolled HTTP POST to Okta's /token
endpoint) — here, AgentCore Identity performs the entire exchange
server-side; agent.py never makes an HTTP call to Okta directly, and
never touches the Okta client secret (held in Secrets Manager, read only
by AgentCore Identity itself). See DESIGN.md's Phase 3 section for the
full design. build_model_router()'s two serving candidates and its
classifier all reuse this exact same exchanged token — one exchange per
caller, regardless of how many model roles ultimately consume it (see
build_model_router()'s docstring for why this makes per-candidate
token-fetching unnecessary).

A fresh MCPClient and Agent are built per request (not shared globally),
following the documented safe pattern for AgentCore Runtime: it avoids
cross-request state leakage and thread-safety issues if concurrent
invocations land on the same container.
"""
import json
import logging
import os
import re
import threading
import time
from datetime import date
from typing import Any, Optional

from bedrock_agentcore.identity.auth import IdentityClient
from bedrock_agentcore.memory.integrations.strands.config import (
    AgentCoreMemoryConfig,
    RetrievalConfig,
)
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from bedrock_agentcore.runtime import BedrockAgentCoreApp, BedrockAgentCoreContext
from mcp.client.streamable_http import streamablehttp_client
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from strands import Agent
from strands.agent.conversation_manager import SummarizingConversationManager
from strands.hooks import AfterModelCallEvent
from strands.models.anthropic import AnthropicModel
from strands.models.model import CacheConfig, CacheToolsConfig
from strands.models.routing import ClassifierStrategy, ModelRouter, RoutingCandidate
from strands.types.agent import Limits
from strands.types.exceptions import MaxTokensReachedException, ModelThrottledException
from strands.tools.mcp.mcp_client import MCPClient
from strands.vended_plugins.context_injector import ContextInjector
from strands.vended_plugins.skills import AgentSkills
from strands_tools.code_interpreter import AgentCoreCodeInterpreter

from prompts import SYSTEM_PROMPT, build_current_date_context

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("GATEWAY_URL", "")
MEMORY_ID = os.environ.get("MEMORY_ID", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
# Phase 3: name of GatewayStack's OAuth2 credential provider, used for the
# Gateway's RFC 8693 On-Behalf-Of token exchange (see build_mcp_client()).
# Empty string (not unset) when GatewayStack's JWT authorizer isn't
# configured — mirrors GATEWAY_URL's own "empty string means not
# configured" convention.
GATEWAY_OBO_PROVIDER_NAME = os.environ.get("GATEWAY_OBO_PROVIDER_NAME", "")
# Base URL of the Gateway's inference target (see gateway_stack.py's
# _add_inference_target()) — e.g. "https://<gateway-id>.gateway.bedrock-
# agentcore.<region>.amazonaws.com/inference". Non-empty means "route
# model calls through the Gateway" (build_model() below); empty means
# "call bedrock-runtime directly", the existing default behavior. Always
# set by RuntimeStack (the target always exists) — this variable's only
# job is the opt-in, not detecting whether the target exists.
GATEWAY_INFERENCE_URL = os.environ.get("GATEWAY_INFERENCE_URL", "")
# Must match GATEWAY_OIDC_SCOPE/GATEWAY_OIDC_AUDIENCE in cdk/app.py, which
# configures these same values on the Gateway's own JWT authorizer
# (allowedScopes/allowedAudience) — see cdk/stacks/gateway_stack.py.
GATEWAY_OBO_SCOPE = "gateway:invoke"
GATEWAY_OBO_AUDIENCE = "travel-agent-gateway"
# AgentCore Identity's TOKEN_EXCHANGE grant mode defaults subject_token_type
# to "urn:ietf:params:oauth:token-type:jwt" — confirmed live (Okta System
# Log: "invalid_subject_token_type") that this org's custom authorization
# server only accepts "urn:ietf:params:oauth:token-type:access_token" for
# this grant (matches Okta's own documented token-exchange examples, and
# AWS's "Implement on-behalf-of token exchange for multi-tenant agents
# with Amazon Bedrock AgentCore Gateway" blog post's "Common pitfalls"
# section, which calls this out explicitly as an Okta-specific override).
# There is no CDK/CfnOAuth2CredentialProvider-level field for this — the
# override must be passed per-call via get_token()'s custom_parameters,
# which maps directly to GetResourceOauth2Token's customParameters request
# field (confirmed against the installed bedrock_agentcore SDK source).
GATEWAY_OBO_SUBJECT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"

# In-process cache for OBO-exchanged Gateway JWTs, keyed by the inbound
# token's `sub` claim. Without this, every single agent turn re-runs the
# full Runtime -> AgentCore Identity -> Okta round trip (confirmed live:
# one exchange per build_mcp_client() call, and a fresh MCPClient/Agent is
# built per request per this module's own docstring) — real added latency
# and Okta token-endpoint load per message, for a token that's actually
# valid for its full lifetime across many turns of the same conversation.
# Keying by `sub` (rather than a single global cached token) is required
# for correctness, not just tidiness: this Runtime's container can serve
# different end users across requests, and caching one user's delegated
# Gateway token under a shared key would hand it to a different user's
# request. Process-local (not e.g. AgentCore Memory or another shared
# store) because a cached token is only ever useful to requests landing on
# this same container — sharing it further would just add complexity for
# no benefit, and would need its own encryption-at-rest story for a live
# bearer credential. A threading.Lock guards concurrent refresh attempts
# for the same `sub` (AgentCore Runtime's documented pattern allows
# concurrent invocations on one container), and expired entries for other
# users are swept opportunistically on each access rather than via a
# separate background task, since this cache is expected to stay small
# (bounded by the number of distinct concurrent users a single container
# actually serves).
_GATEWAY_OBO_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_GATEWAY_OBO_TOKEN_CACHE_LOCK = threading.Lock()
# Refresh this many seconds before the token's real `exp` claim to avoid a
# request starting an MCP call with a token that expires mid-flight.
GATEWAY_OBO_TOKEN_REFRESH_SKEW_SECONDS = 60

# Sonnet is the design's chosen model (see DESIGN.md decision #7) for its
# multi-step reasoning and tool-use reliability across the three Gateway tools.
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-5")
# Cheap-tier serving candidate AND classifier model for the ModelRouter-
# based tiered inference path (see build_model_router()) — one model
# fills both roles, matching this project's decision to keep the
# candidate/classifier model set minimal rather than adding a third,
# even-cheaper model just for classification. Default confirmed live via
# `aws bedrock get-inference-profile` as a real, ACTIVE, SYSTEM_DEFINED
# cross-region inference profile in this account (same "us."-prefix
# convention MODEL_ID's own default uses).
HAIKU_MODEL_ID = os.environ.get(
    "HAIKU_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)
# Extends ClassifierStrategy's own built-in default system prompt (which
# already implements a close variant of this policy — "select the least
# capable candidate that can still deliver a complete and accurate
# result") with explicit, this-agent-specific escalation signals. Written
# proactively, not reactively: rather than shipping the bare default and
# waiting to observe real misroutes, this names the concrete phrasing
# patterns (weather/place/date/planning language) that should always
# escalate to the capable candidate, since a bare complexity policy has
# no way to know this agent's tools are the actual complexity signal that
# matters here. Deliberately does NOT attempt to correct a misrouted turn
# after the fact (ClassifierStrategy only picks the opening candidate and
# never switches after a non-failure — see DESIGN.md's model-routing
# decision for why building corrective mid-turn routing machinery was
# explicitly rejected in favor of investing in this prompt instead).
_ROUTING_POLICY_SYSTEM_PROMPT = (
    "You are a model-routing classifier for a travel-planning assistant. Select exactly one "
    "candidate for the latest user message. The two candidates differ only in cost/capability, "
    "not in which tools they can call — either candidate CAN call tools if it decides to, but "
    "routing a tool-requiring turn to the cheap candidate risks a worse or slower result. "
    "Prefer the cheap candidate only for turns that are purely conversational and need no "
    "grounding: greetings, small talk, answering from context already in the conversation (e.g. "
    "\"what do you know about me\"), or simple clarifying-question exchanges about dates/budget/"
    "interests/travelers with no research involved. Escalate to the capable candidate whenever "
    "the message mentions or implies: weather or forecasts, specific places/points of interest/"
    "things to do, specific dates or date math (trip length, \"next week\", calendar dates), "
    "budget/cost arithmetic across multiple items, or generating/revising an itinerary — these "
    "signal a turn is likely to need tool calls (weather, places, web search, code interpreter) "
    "or multi-step reasoning, even if the message itself is phrased simply or conversationally. "
    "When genuinely uncertain, prefer the capable candidate — a wrongly-escalated simple turn "
    "costs more, but a wrongly-cheapened complex turn produces a worse answer for the traveler."
)
# Bedrock's Converse API (and, by the same token, the Gateway's own
# bedrock-mantle-routed Anthropic Messages API path) defaults to a fairly
# low per-model max output token limit if maxTokens is omitted from the
# request — both BedrockModel and AnthropicModel only set it when
# max_tokens is explicitly configured. A real multi-day, tool-grounded
# itinerary response can be long — confirmed in production: a 3-day trip
# request that made 13 tool calls before writing its answer hit
# strands.types.exceptions.MaxTokensReachedException partway through the
# itinerary, which surfaced to callers as an opaque
# InvokeAgentRuntime 500 error. 8192 gives long itineraries realistic room
# to complete; MAX_TOKENS_REACHED is still handled gracefully in invoke()
# below in case an even longer response exceeds this.
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "8192"))

# Backstop against an agent loop that never converges — e.g. the model
# repeatedly alternating tool calls without making progress toward an
# answer. Strands' `Limits(turns=...)` counts agent-loop iterations (one
# model call plus any tool execution that follows), checked at the top of
# each iteration so a tool call already in flight always finishes first;
# when tripped, the loop stops gracefully with stop_reason "limit_turns"
# (no exception — handled explicitly in stream_agent_turn() below).
#
# 30 is deliberately generous, not a tight cap: a real, already-documented
# production turn (see PLAN.md's post-Phase-7 MaxTokensReachedException
# fix) made 13 tool calls before writing its final answer for a 3-day,
# multi-stop itinerary. Tool calls that are independent (e.g. weather for
# several days, multiple place searches) are often batched several-per-
# turn by Claude, so that request's actual turn count was likely well
# under 13 — but there's no live per-cycle breakdown confirming the exact
# ratio, so 30 is sized to clear that documented case with real margin
# under the conservative assumption that tool calls could be mostly
# sequential, rather than picked as a round number. This is independent of,
# and a much tighter ceiling than, the ~15-minute Runtime invocation
# timeout and the Gateway's token-per-minute rate limit (DESIGN.md) — a
# loop of cheap, low-token tool calls could iterate many times without
# ever tripping the token budget, which is the gap this closes.
AGENT_MAX_TURNS = int(os.environ.get("AGENT_MAX_TURNS", "30"))

# TTL for the prompt-cache breakpoints _build_gateway_anthropic_model() adds
# to the two ModelRouter serving candidates (not the classifier — see that
# function's own docstring). "5m" (Anthropic's own API default) vs "1h" is
# a real cost/reuse-window tradeoff, not cosmetic: a 1-hour cache write
# costs more (2x base input-token rate vs. 1.25x for 5-minute) but survives
# a longer gap between turns before falling back to a full-price rewrite.
# Env-var-overridable (not CDK-wired) specifically so this project's own
# live comparison — deploy with the 5-minute default, observe real
# cache-read/write behavior via the diagnostic panel, then redeploy with
# PROMPT_CACHE_TTL=1h and repeat — needs no code change between the two
# runs, matching this file's existing MAX_OUTPUT_TOKENS/AGENT_MAX_TURNS
# override convention. An empty string (the default) is treated as "use
# the API's own 5-minute default" — CacheConfig.ttl=None, not the literal
# string "5m", since Anthropic's API only requires a TTL value at all when
# requesting the non-default 1-hour window.
PROMPT_CACHE_TTL = os.environ.get("PROMPT_CACHE_TTL", "").strip() or None

# Procedural-knowledge skills (see agent/skills/), loaded via Strands'
# AgentSkills plugin: lightweight metadata for each skill is injected into
# the system prompt on every invocation, and the model loads a skill's full
# instructions on demand via the plugin's own "skills" tool — not injected
# upfront, to avoid bloating every request's token cost with instructions
# most turns don't need. Resolved relative to this file (not CWD) since
# agent/ is bundled and run as a flat directory.
SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")


def build_skills_plugin() -> AgentSkills:
    """Build the AgentSkills plugin, loading every skill under SKILLS_DIR."""
    return AgentSkills(skills=[SKILLS_DIR])


def build_date_context_injector() -> ContextInjector:
    """Build the ContextInjector that folds today's date into fresh user turns.

    Replaces this project's original approach (prepending "Today's date is
    ..." directly onto SYSTEM_PROMPT, computed once per invoke() call) once
    prompt caching was added for the two ModelRouter serving candidates (see
    build_model_router()) — a date glued to the front of the system prompt
    would invalidate any cache point placed after it once per UTC day at
    minimum, since Anthropic's cache requires an exact prefix match. See
    prompts.py's build_current_date_context() docstring for the full
    rationale, including why this is the officially documented replacement
    for the deprecated strands_tools.current_time tool, not just a caching
    workaround.

    trigger="userTurn" (the default, passed explicitly here for clarity) —
    injects only when the latest message is a fresh user ask, not on
    intermediate tool-result turns. This matches how this agent actually
    uses the date (grounding the traveler's own request for relative-date
    math), not something a mid-tool-loop cycle needs; date math within a
    tool loop already goes through build_code_interpreter_tool(), not a
    second read of "today" from context.

    The rendered date is computed fresh on every call (date.today(), not
    captured once at Agent-construction time) — AgentCore Runtime containers
    run in UTC, so this is already the correct "today" for any traveler
    without per-session timezone collection (see prompts.py's requirements
    list for why that's out of scope). Folded onto the user message, never
    written to agent.messages/durable history — a conversation resumed on a
    later day is grounded in that day's real date, not a stale one from
    whenever the conversation started.
    """
    return ContextInjector(
        lambda context: build_current_date_context(date.today().isoformat()),
        name="travel-agent:current-date",
        trigger="userTurn",
    )


def build_code_interpreter_tool():
    """Build the AgentCore Code Interpreter tool for itinerary-related arithmetic.

    Gives the agent a real Python sandbox (AWS's AWS-managed
    "aws.codeinterpreter.v1" Code Interpreter, not a custom-provisioned one
    — no CDK resource needed beyond the IAM permissions granted to the
    Runtime execution role) instead of doing date/duration/budget math in
    its own reasoning. Directly targets a real, previously-observed failure
    class: this project's own history (see PLAN.md's post-Phase-7 fix) is
    full of long, multi-day, multi-stop itineraries where arithmetic
    mistakes (day counts, running totals) are a plausible failure mode that
    bounding tool-call loops or output tokens does not fix.

    Region is read from AWS_REGION (already set by RuntimeStack for the
    AgentCore Memory client) rather than left to fall back on the
    library's own default-region resolution, for the same reason the rest
    of this module always passes region explicitly.

    The returned .code_interpreter tool already catches AWS/session errors
    itself and returns a graceful {"status": "error", ...} tool result
    (confirmed by reading strands_tools' own
    agent_core_code_interpreter.py) rather than raising — no additional
    wrapper needed here, unlike a hand-rolled tool would require.
    """
    return AgentCoreCodeInterpreter(region=AWS_REGION).code_interpreter

# Namespace patterns must match those configured on the Memory resource in
# cdk/stacks/memory_stack.py.
USER_PREFERENCE_NAMESPACE = "/travel-agent/actor/{actorId}/preferences"
SUMMARIZATION_NAMESPACE = "/travel-agent/actor/{actorId}/session/{sessionId}/summary"

# Runtime session IDs follow the "<placeholder>___<sessionId>" convention
# used across AgentCore Runtime samples. The placeholder component is
# purely cosmetic (kept only so runtimeSessionId strings built by the web
# UI still satisfy the ">=33 chars" / "<x>___<y>" format they were already
# producing) — the real actor_id now comes from the payload (see
# get_actor_id() below), not from this string.
SESSION_ID_SEPARATOR = "___"
DEFAULT_ACTOR_ID = "anonymous-traveler"

app = BedrockAgentCoreApp()


def get_actor_id(payload: dict) -> str:
    """Derive the actor_id for Memory scoping, from a verified JWT if one is available.

    Phase 3: when the Runtime's inbound authorizer is JWT (RuntimeStack's
    Okta config is set — DESIGN.md's Phase 2 decision), the inbound
    request carries a real `Authorization: Bearer <jwt>` header, which
    AgentCore Runtime has already cryptographically verified (signature,
    issuer, expiry) before invoking this code at all — see
    _sub_from_authorization_header()'s docstring for why decoding it here
    without re-verifying is safe. This restores decision #31's original
    "derive server-side from a verified token, never from client input"
    stance for Memory scoping, which decision #37 had walked back when
    the Runtime's own inbound auth was IAM/SigV4-only (no bearer token to
    decode). The payload's own actor_id field is used only as a fallback
    when no such header is present — i.e. when the Runtime is still
    running under IAM inbound auth, in which case the caller (the hosted
    web UI's ECS task, deriving this from its own verified OIDC session)
    is trusted to supply the correct actor_id directly, per decision #37's
    original rationale.

    The raw actor_id is sanitized before use as AgentCore Memory's
    actorId, not used verbatim — see sanitize_actor_id()'s docstring.

    Falls back to DEFAULT_ACTOR_ID if neither a decodable `sub` claim nor
    a payload actor_id is available, so the agent still runs (without real
    per-user memory isolation) rather than crashing the whole request.
    """
    sub = _sub_from_authorization_header()
    if sub:
        return sanitize_actor_id(sub)

    raw = (payload or {}).get("actor_id")
    if not raw:
        logger.warning("No 'sub' claim or 'actor_id' in payload; using default actor")
        return DEFAULT_ACTOR_ID
    return sanitize_actor_id(str(raw))


def _sub_from_authorization_header() -> Optional[str]:
    """Best-effort extraction of the `sub` claim from the inbound bearer token.

    Reads the raw inbound `Authorization` header via
    BedrockAgentCoreContext.get_request_headers() (populated by
    BedrockAgentCoreApp before invoke() runs) and decodes its JWT payload
    without verifying the signature. This is safe specifically because,
    under JWT inbound auth, AgentCore Runtime's own JWT authorizer has
    *already* cryptographically validated this exact token's signature,
    issuer, and expiry before ever invoking this code — the same trust
    argument web/auth.py's _sub_from_access_token() already makes for
    Phase 2's token, just anchored at a different point in the request
    path (the Runtime's authorizer having already run, rather than the
    token having just been fetched fresh over TLS). Returns None (not an
    error) for every case where no useful `sub` can be recovered — a
    missing header, a malformed token, or plain IAM inbound auth where no
    Authorization header carrying a real JWT is expected at all — so
    get_actor_id() can fall back to the payload's own actor_id field
    without this function's caller needing to distinguish those cases.
    """
    headers = BedrockAgentCoreContext.get_request_headers() or {}
    auth_header = headers.get("Authorization") or headers.get("authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer "):]
    try:
        import jwt as _jwt

        claims = _jwt.decode(token, options={"verify_signature": False})
        sub = claims.get("sub")
        return str(sub) if sub else None
    except Exception:  # noqa: BLE001 - best-effort; caller has a fallback
        return None


# AgentCore Memory's actorId pattern: must start with an alphanumeric, then
# any run of alphanumerics/-/_/ and optional ":"-separated segments of the
# same. Confirmed against the real ListEvents API pattern
# ("[a-zA-Z0-9][a-zA-Z0-9-_/]*(?::[a-zA-Z0-9-_/]+)*[a-zA-Z0-9-_/]*").
_ACTOR_ID_DISALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9\-_/:]")


def sanitize_actor_id(raw: str) -> str:
    """Map an arbitrary actor_id string to one valid as an AgentCore Memory actorId.

    Replaces every character outside the allowed set with "-", then strips
    any leading run of non-alphanumeric characters (the pattern requires
    the first character specifically be alphanumeric). Deterministic: the
    same input always sanitizes to the same actorId, so Memory scoping
    stays stable across sessions/logins for a given user — the property
    that actually matters, not preserving the original string's exact
    shape. Needed because upstream identity claims (e.g. an OIDC `sub`,
    which may be an email address) are not guaranteed to satisfy Memory's
    actorId pattern on their own — confirmed live against a real
    deployment: this Okta org's `sub` was an email address, and passing it
    straight through caused every ListEvents/CreateEvent call to fail with
    ValidationException on actorId.
    """
    sanitized = _ACTOR_ID_DISALLOWED_CHARS.sub("-", raw)
    sanitized = sanitized.lstrip("-_/:")
    return sanitized or DEFAULT_ACTOR_ID


def extract_response_text(message: dict) -> str:
    """Extract the assistant's plain-text reply from a Strands result message.

    `result.message` is a dict like {"role": "assistant", "content": [...]}
    where `content` is a list of blocks — typically a `reasoningContent`
    block (Claude's extended thinking, not meant for the end user) followed
    by one or more `text` blocks. Concatenates only the `text` blocks, so
    callers (CLI, web UI) get the clean markdown reply the docstrings
    promise, not a stringified dict repr of the whole message.
    """
    content = message.get("content") or []
    text_parts = [block["text"] for block in content if isinstance(block, dict) and "text" in block]
    return "\n".join(text_parts).strip()


def extract_tool_result_text(tool_result: dict) -> str:
    """Extract the plain-text payload from a Strands toolResult block.

    `tool_result["content"]` is a list of blocks, normally a single
    `{"text": "..."}` block for the tools this agent uses (Web Search,
    weather, places all return text/JSON-as-text). Concatenates any text
    blocks found; falls back to a str() of the raw content list if no text
    block is present, so an unexpected tool result shape still surfaces
    something in the diagnostic stream rather than silently dropping it.
    """
    content = tool_result.get("content") or []
    text_parts = [block["text"] for block in content if isinstance(block, dict) and "text" in block]
    if text_parts:
        return "\n".join(text_parts)
    return str(content) if content else ""


def _turn_cache_usage(result: Any) -> tuple[Optional[int], Optional[int]]:
    """Best-effort read of this turn's accumulated prompt-cache token usage.

    Returns (cache_read_input_tokens, cache_write_input_tokens), or (None,
    None) when no cache usage data is present at all (e.g. prompt caching
    isn't enabled for this turn's serving candidate, or an older Strands
    version's AgentResult doesn't carry this field).

    Reads result.metrics.accumulated_usage — a Strands EventLoopMetrics
    Usage mapping keyed "cacheReadInputTokens"/"cacheWriteInputTokens"
    (camelCase; confirmed against the installed strands-agents source and
    Strands' own OpenTelemetry tracer, which reads this exact same field
    for its "gen_ai.usage.cache_read_input_tokens" span attribute) — NOT
    strands.hooks.AfterModelCallEvent.stop_response, which was checked
    directly and confirmed to expose only {message, stop_reason}, no usage
    data at all.

    accumulated_usage is a TURN-level total (EventLoopMetrics accumulates
    across every model-call cycle within this one turn — the classifier
    call plus however many serving-candidate/tool-loop cycles ran), not a
    per-model-call breakdown. Since this project's classifier is not cache-
    enabled (see build_model_router()'s scope decision), a nonzero read
    here reliably indicates the serving candidate's own cache actually hit
    — but if a future change enables caching on more than one role, this
    would no longer cleanly attribute cache activity to one specific model
    call. Deliberately not resolved here (see DESIGN.md's prompt-caching
    decision): this project's stated verification bar for this feature is
    "did caching demonstrably activate at all," not "exactly which call."
    """
    metrics = getattr(result, "metrics", None)
    usage = getattr(metrics, "accumulated_usage", None) or {}
    cache_read = usage.get("cacheReadInputTokens")
    cache_write = usage.get("cacheWriteInputTokens")
    if cache_read is None and cache_write is None:
        return None, None
    return cache_read, cache_write


async def stream_agent_turn(agent: Agent, user_message: str):
    """Run one turn, yielding labeled diagnostic events as they occur.

    Translates strands.Agent.stream_async()'s raw event stream into a small,
    stable set of labeled events for the web UI's diagnostic panel and live
    chat bubble: {"type": "reasoning" | "text" | "tool_use" | "tool_result"
    | "done" | "error", "data": ...}. Full raw tool-result payloads are
    passed through verbatim (no truncation) — this is a diagnostics feature,
    so completeness matters more than payload size; the frontend is
    responsible for making large payloads collapsible rather than this
    layer summarizing them away.

    Real strands.Agent.stream_async() event shapes (confirmed against the
    installed strands-agents==1.52.0 source directly, not assumed):
      - text delta:      "data" in event                         -> event["data"]
      - reasoning delta: event.get("reasoning") truthy           -> event.get("reasoningText")
      - tool-use delta:  "current_tool_use" in event              -> {toolUseId, name, input}
                         (input accumulates across deltas as the model streams
                         the tool call's JSON arguments as a raw string, one
                         fragment at a time; there's no explicit "done"
                         signal for a given toolUseId, so this is only
                         forwarded once `input` has accumulated into a
                         complete, parseable JSON *object* — parsing alone
                         isn't enough, since an in-progress fragment can
                         coincidentally parse as a valid non-object JSON
                         value, such as a bare string, before the real
                         arguments object is complete)
      - tool result:     "message" in event, message["role"] == "user",
                         content list contains a "toolResult" block
      - final result:    "result" in event -> AgentResult (message has the
                         final assistant text; used for the "done" event and,
                         by the caller, for the MaxTokensReachedException
                         partial-response path)
    Lifecycle events (init_event_loop/start_event_loop/force_stop) and bare
    delta-only chunks with nothing new to show are intentionally skipped —
    they carry no information the diagnostic panel doesn't already get from
    the events above.

    Handles strands.types.exceptions.MaxTokensReachedException the same way
    run_agent_turn() used to for the non-streaming path: if the model runs
    out of its output token budget mid-response, Strands has already
    appended the partial assistant message to `agent.messages` before
    raising, so the partial text (already streamed to the client via prior
    "text" events) is followed by a final "error" event with a cut-off note,
    instead of letting the exception propagate out of the AgentCore Runtime
    entrypoint as an opaque failure.

    Also handles strands.types.exceptions.ModelThrottledException: Strands'
    own ModelRetryStrategy already retries a throttled model call
    transparently, so this only fires once retries are exhausted (sustained
    throttling, not a brief burst) — e.g. a Gateway-side token-per-minute
    rate limit (see DESIGN.md) being hit consistently. There's no partial
    response to preserve here (the failure is at the model-call layer,
    before any tokens for that attempt are yielded), so this yields a plain
    "error" event with a retry-later note rather than propagating the
    exception out of the entrypoint as an opaque failure.

    Also passes limits=Limits(turns=AGENT_MAX_TURNS) to stream_async() as a
    backstop against a non-converging agent loop (see AGENT_MAX_TURNS).
    Unlike the two exceptions above, a tripped turns cap is not an
    exception — Strands ends the loop gracefully and the final "result"
    event carries stop_reason == "limit_turns", checked explicitly below
    and translated into the same "error" event shape as the other two
    cutoff cases, with whatever partial answer the model had already
    written (there's no separate exception handler needed for this path).

    Emits one "routing" event ({"type": "routing", "data": {"candidate":
    "cheap" | "capable"}}) the first time this turn's ModelRouter
    selection becomes observable, via an AfterModelCallEvent hook
    registered on this agent — see _selected_candidate_name()'s docstring
    for why this reads Strands' internal invocation_state rather than a
    public API (none exists for this yet). Also logged via logger.info()
    at the same point, matching this module's existing OBO-cache logging
    convention, for durable/queryable evidence beyond the live diagnostic
    panel. The hook is registered unconditionally, not gated on
    isinstance(agent.model, ModelRouter) — found live (not assumed) that
    Agent.__init__ immediately resolves agent.model to the router's
    *default* candidate model, never the ModelRouter instance itself (its
    own module docstring says as much: "routing applies to
    InvokeModelStage, so agent.model stays the first declared
    candidate"), so that isinstance check would silently never be true
    even for a genuinely routed agent. _selected_candidate_name() already
    returns None gracefully when there's no routing state to find (a
    non-routed agent), so no "routing" event is ever emitted in that case
    — this is intentionally silent, not an error.

    Emits one "cache_usage" event ({"type": "cache_usage", "data":
    {"cache_read_input_tokens": int, "cache_write_input_tokens": int}})
    immediately before the final "done" event, when this turn's
    AgentResult carries any prompt-cache usage data at all — see
    _turn_cache_usage()'s own docstring for the exact field this reads and
    why it's a turn-level total, not a per-model-call breakdown. Silently
    omitted (no event at all) when neither field is present, the same
    intentional-silence convention as the "routing" event above — e.g. a
    turn served entirely by the classifier's own non-cache-enabled request
    with no serving-candidate call at all would have nothing to report.
    """
    routing_state: dict[str, Optional[str]] = {"candidate": None, "emitted": False}

    async def _on_after_model_call(event: AfterModelCallEvent) -> None:
        if routing_state["candidate"] is None:
            name = _selected_candidate_name(event)
            if name is not None:
                routing_state["candidate"] = name

    agent.hooks.add_callback(AfterModelCallEvent, _on_after_model_call)

    seen_tool_use_ids: set[str] = set()
    try:
        async for event in agent.stream_async(
            user_message, limits=Limits(turns=AGENT_MAX_TURNS)
        ):
            if not routing_state["emitted"] and routing_state["candidate"] is not None:
                routing_state["emitted"] = True
                logger.info(
                    "ModelRouter selected candidate=%r for this turn",
                    routing_state["candidate"],
                )
                yield {"type": "routing", "data": {"candidate": routing_state["candidate"]}}
            if event.get("reasoning") and event.get("reasoningText"):
                yield {"type": "reasoning", "data": event["reasoningText"]}
            elif "data" in event:
                yield {"type": "text", "data": event["data"]}
            elif "current_tool_use" in event:
                tool_use = event["current_tool_use"]
                tool_use_id = tool_use.get("toolUseId")
                name = tool_use.get("name")
                raw_input = tool_use.get("input")
                if name and tool_use_id and tool_use_id not in seen_tool_use_ids:
                    # `input` is the tool call's JSON arguments, streamed in
                    # as a raw string fragment-by-fragment (starts as "" on
                    # the same event that first carries the tool name, then
                    # grows with each subsequent delta). There's no distinct
                    # "tool_use finished" event, so completeness is detected
                    # by the input string having become a complete, valid
                    # JSON *object* — the same check Strands itself uses
                    # before invoking the tool. Checking for "parses as any
                    # JSON value" is not sufficient: an in-progress fragment
                    # can coincidentally parse as a valid (but wrong-typed)
                    # JSON value before the object itself is complete —
                    # e.g. a fragment sequence that closes a quoted string
                    # early parses as a bare JSON string ("") rather than
                    # the args object, which was observed live producing an
                    # empty-looking tool_use event in the diagnostic panel.
                    # Tool arguments are always a JSON object, never a bare
                    # string/number/etc., so requiring a dict rules that out.
                    parsed_input = None
                    if raw_input:
                        try:
                            candidate = json.loads(raw_input)
                        except (TypeError, ValueError):
                            candidate = None
                        if isinstance(candidate, dict):
                            parsed_input = candidate
                    if parsed_input is not None:
                        seen_tool_use_ids.add(tool_use_id)
                        yield {
                            "type": "tool_use",
                            "data": {
                                "toolUseId": tool_use_id,
                                "name": name,
                                "input": parsed_input,
                            },
                        }
            elif "message" in event:
                message = event["message"]
                if message.get("role") == "user":
                    for block in message.get("content") or []:
                        if isinstance(block, dict) and "toolResult" in block:
                            tool_result = block["toolResult"]
                            yield {
                                "type": "tool_result",
                                "data": {
                                    "toolUseId": tool_result.get("toolUseId"),
                                    "status": tool_result.get("status"),
                                    "text": extract_tool_result_text(tool_result),
                                },
                            }
            elif "result" in event:
                result = event["result"]
                if result.stop_reason == "limit_turns":
                    # Graceful cutoff, not an exception — see AGENT_MAX_TURNS.
                    # agent.messages already has whatever the model wrote up
                    # to the last completed turn (if it had started writing
                    # a final answer when the cap tripped); surface that as
                    # partial text alongside the cutoff note, the same shape
                    # as the MaxTokensReachedException handler below.
                    logger.warning(
                        "Agent loop hit turns cap (%d) without converging",
                        AGENT_MAX_TURNS,
                    )
                    partial_text = extract_response_text(result.message)
                    note = (
                        "That request needed more steps than I'm allowed to "
                        "take in one go — try breaking it into smaller "
                        "requests (e.g. one destination or a shorter date "
                        "range at a time)."
                    )
                    yield {
                        "type": "error",
                        "data": {"partial_text": partial_text, "note": note},
                    }
                else:
                    _cache_read, _cache_write = _turn_cache_usage(result)
                    if _cache_read is not None or _cache_write is not None:
                        yield {
                            "type": "cache_usage",
                            "data": {
                                "cache_read_input_tokens": _cache_read or 0,
                                "cache_write_input_tokens": _cache_write or 0,
                            },
                        }
                    yield {"type": "done", "data": extract_response_text(result.message)}
    except MaxTokensReachedException:
        logger.warning("Model hit max_tokens mid-response; ending stream with partial reply")
        partial_text = extract_response_text(agent.messages[-1])
        note = (
            "That response got cut off — it was longer than I could send in one "
            "go. Ask me to continue, or ask for a shorter version, and I'll pick up "
            "where I left off."
        )
        yield {"type": "error", "data": {"partial_text": partial_text, "note": note}}
    except ModelThrottledException:
        # Strands' own ModelRetryStrategy already retries a throttled model
        # call transparently (exponential backoff, 6 attempts by default —
        # see strands.event_loop._retry.ModelRetryStrategy), so this only
        # fires once every retry has been exhausted, i.e. sustained
        # throttling rather than a brief burst. Relevant now that Gateway-
        # routed inference (see build_model()) can be capped by a
        # CfnGatewayRateLimit TPM budget (DESIGN.md's rate-limiting
        # decision) — without this handler, the exception propagated out
        # of the AgentCore Runtime entrypoint uncaught, surfacing to the
        # web UI as an opaque stream failure rather than a legible message.
        logger.warning("Model call throttled after exhausting retries")
        note = (
            "I'm getting rate-limited right now — please try again in a "
            "minute or two."
        )
        yield {"type": "error", "data": {"note": note}}


def parse_session_id(runtime_session_id: Optional[str]) -> str:
    """Extract the bare session_id component from a Runtime session ID.

    Runtime session IDs are still formatted as "<placeholder>___<sessionId>"
    (see SESSION_ID_SEPARATOR) for compatibility with the existing
    AgentCore Runtime convention and the >=33-character requirement, but
    only the session_id half is meaningful here now — the actor_id half is
    derived separately from the verified JWT (see get_actor_id(), DESIGN.md
    decision #31), not from this string.

    Falls back to the raw session id (or a default) if the expected
    separator isn't present, so a misconfigured caller degrades gracefully
    rather than crashing the whole request.
    """
    if not runtime_session_id:
        return "default-session"

    if SESSION_ID_SEPARATOR in runtime_session_id:
        _, session_id = runtime_session_id.split(SESSION_ID_SEPARATOR, 1)
        return session_id

    logger.warning(
        "runtime session id %r missing '%s' separator; using it as-is",
        runtime_session_id,
        SESSION_ID_SEPARATOR,
    )
    return runtime_session_id


def build_session_manager(actor_id: str, session_id: str) -> Optional[AgentCoreMemorySessionManager]:
    """Build the AgentCore Memory session manager for this request.

    Returns None if MEMORY_ID isn't configured, so the agent can still run
    (without persistent memory) rather than failing outright.
    """
    if not MEMORY_ID:
        logger.warning("MEMORY_ID not set; running without AgentCore Memory")
        return None

    memory_config = AgentCoreMemoryConfig(
        memory_id=MEMORY_ID,
        session_id=session_id,
        actor_id=actor_id,
        retrieval_config={
            # relevance_score=0.0 is set explicitly, NOT omitted. Omitting it
            # would fall back to RetrievalConfig's own default of 0.2 — still a
            # nonzero threshold. bedrock-agentcore==1.21.0's
            # AgentCoreMemorySessionManager.retrieve_customer_context() filters
            # retrieved records with `m.get("score", 0.0) >= relevance_score`,
            # but retrieve_memory_records's real memoryRecordSummaries objects
            # carry no "score" field at all (confirmed against live records via
            # list-memory-records) — every record defaults to score 0.0, so any
            # positive threshold discards every record unconditionally. This
            # silently dropped every retrieved memory before it could be
            # injected into the model's context, even though an earlier,
            # unconditional "Retrieved N memories from namespace" log line (in
            # MemoryClient.retrieve_memories, logged before this filter runs)
            # made it look like retrieval had succeeded. relevance_score=0.0
            # disables the filter (the library's own `if
            # retrieval_config.relevance_score:` guard treats 0.0 as falsy);
            # top_k alone still bounds how many records come back.
            USER_PREFERENCE_NAMESPACE: RetrievalConfig(top_k=5, relevance_score=0.0),
            SUMMARIZATION_NAMESPACE: RetrievalConfig(top_k=3, relevance_score=0.0),
        },
    )
    return AgentCoreMemorySessionManager(
        agentcore_memory_config=memory_config,
        region_name=AWS_REGION,
    )


def _bedrock_model_id_for_gateway(model_id: str) -> str:
    """Map a bedrock-runtime-style model ID to its bedrock-mantle equivalent.

    These are genuinely different ID namespaces, confirmed against AWS's
    own per-model documentation (the "Programmatic Access" table on each
    model's Bedrock model-card page) rather than assumed to be a single
    shared transformation:
      - Sonnet 5: bedrock-runtime is "anthropic.claude-sonnet-5" (no cross-
        region prefix on the bare ID); bedrock-mantle is the SAME string,
        "anthropic.claude-sonnet-5". Stripping MODEL_ID's "us." cross-
        region-inference-profile prefix happens to be sufficient for this
        one model — found live (decision "Gateway-routed inference
        mandatory") — but that is a coincidence of this model's ID shape,
        not a rule that generalizes.
      - Haiku 4.5: bedrock-runtime is
        "anthropic.claude-haiku-4-5-20251001-v1:0" (an older-style, date-
        and-version-suffixed ID); bedrock-mantle is the SHORTER, DISTINCT
        alias "anthropic.claude-haiku-4-5" — not a substring or prefix-
        stripped form of the bedrock-runtime ID at all (the
        "-20251001-v1:0" suffix is dropped entirely, not just a "us."
        prefix). Found live: sending the bedrock-runtime-shaped ID through
        this Gateway's bedrock-mantle connector target returned a real
        Anthropic 400 ("Model ID contains invalid characters." — the
        literal ":0" is what's rejected) from the classifier's forced-
        tool-use call, which ClassifierStrategy silently caught and
        treated as a classification failure, always falling back to the
        router's default candidate — this is what made every real chat
        turn route to "capable" regardless of message content, discovered
        via a temporary raw-HTTP diagnostic probe added to
        build_model_router() and removed once root-caused.

    A single "strip a known prefix" rule cannot express both cases
    correctly (Sonnet needs a prefix stripped; Haiku needs a suffix
    stripped and is a categorically different ID shape) — so this is an
    explicit per-model mapping, not a shared string transformation.
    Raises ValueError for any model_id not in the mapping, rather than
    silently guessing at a transformation for a model this project hasn't
    verified against AWS's own docs — a wrong guess here fails the same
    way this bug did (a real request rejected by bedrock-mantle), just
    louder and sooner.
    """
    mapping = {
        "us.anthropic.claude-sonnet-5": "anthropic.claude-sonnet-5",
        "anthropic.claude-sonnet-5": "anthropic.claude-sonnet-5",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0": "anthropic.claude-haiku-4-5",
        "anthropic.claude-haiku-4-5-20251001-v1:0": "anthropic.claude-haiku-4-5",
    }
    if model_id not in mapping:
        raise ValueError(
            f"model_id={model_id!r} has no known bedrock-mantle equivalent — "
            "verify the correct mapping against this model's AWS Bedrock "
            "model-card page (\"Programmatic Access\" table, bedrock-mantle "
            "row) before adding it here; do not guess a string "
            "transformation, since bedrock-mantle's aliasing convention "
            "differs per model (see this function's own docstring)."
        )
    return mapping[model_id]


def _build_gateway_anthropic_model(
    model_id: str,
    gateway_token: str,
    *,
    max_tokens: int,
    enable_adaptive_thinking: bool = True,
    enable_prompt_caching: bool = False,
) -> AnthropicModel:
    """Build one Gateway-routed AnthropicModel candidate from an already-exchanged token.

    Shared by every model role build_model_router() constructs (both
    serving candidates and the classifier) — each is a stateless
    AnthropicModel differing only in model_id, pointed at the same
    Gateway inference target with the same auth_token. Candidates must
    be stateless per ModelRouter's own construction guard (conversation
    history lives on the Agent, not the model provider) — a plain
    AnthropicModel already satisfies this, so no extra care is needed
    here beyond not sharing one instance across two roles (ModelRouter
    rejects duplicate model instances — see its own construction guards).

    enable_adaptive_thinking defaults to True (matching this project's
    original single-model behavior) but MUST be set False for any Haiku
    4.5 construction — found live, the real root cause of a persistent
    403 that survived the earlier model-ID-mapping fix: Claude Haiku 4.5
    does not support extended/adaptive thinking at all, and Anthropic's
    API correctly rejects any request carrying the "thinking" param with
    "400 invalid_request_error: adaptive thinking is not supported on
    this model" — surfaced through ClassifierStrategy as a caught,
    silently-swallowed classification failure (only the exception's type
    name is logged, not its message), which is why every real chat turn
    kept routing to "capable" even after the model-ID mapping was fixed.
    Confirmed via a temporary diagnostic that called
    classifier_model.structured_output() directly and logged the real
    exception body — removed once root-caused. The two serving
    candidates keep adaptive thinking enabled (Sonnet does support it,
    and it's what powers the "reasoning" stream_agent_turn() event for
    the diagnostic panel); only the classifier construction passes
    enable_adaptive_thinking=False, since its job is a one-field
    structured-output decision that never needs to think out loud.

    enable_prompt_caching (default False) adds two Anthropic-native
    ("ephemeral") cache breakpoints, matching this project's educational
    scope decision to cache the two ModelRouter serving candidates only,
    not the classifier (see build_model_router()'s own docstring for why
    caching the classifier's own request is a separate, unverified
    question left for a future follow-up rather than solved here):

    - cache_config=CacheConfig(strategy="anthropic", system_prompt_ttl=True,
      ttl=PROMPT_CACHE_TTL) — auto-injects a cache point at the end of the
      system prompt (SYSTEM_PROMPT is the only system content this project
      sends, so this is unambiguous). strategy="anthropic" (not "auto") is
      deliberate: "auto" would additionally probe model support and could
      silently no-op on an unsupported model, which isn't a concern here
      since both serving candidates are confirmed-cacheable Claude models —
      "anthropic" injects unconditionally in Anthropic-compatible format,
      matching what this function already knows to be true.
    - cache_tools=CacheToolsConfig(ttl=PROMPT_CACHE_TTL) — a second,
      independent cache point after the tool-definitions block (the
      Gateway MCP tools plus the code interpreter tool — see invoke()'s
      Agent(tools=...) construction). Confirmed via a local token-count
      measurement (not assumed) that SYSTEM_PROMPT plus this agent's real
      tool definitions clears both Claude Sonnet 4.5's (1,024) and Claude
      Haiku 4.5's (4,096) minimum per-cache-point token thresholds with
      substantial margin — SYSTEM_PROMPT alone (~700 estimated tokens)
      would NOT have cleared Haiku's threshold on its own, which is why
      this project's scope decision was "system prompt + tools" together,
      not system-prompt-only.

    Whether cache_control actually survives being proxied through the
    Gateway's bedrock-mantle connector unmodified was, at the time this was
    written, an open, unverified question — AWS's own bedrock-mantle
    documentation describes the Anthropic Messages API as a first-class
    supported request format for this connector (not merely an incidental
    passthrough), but this project's own history with this exact connector
    (see _bedrock_model_id_for_gateway()'s docstring) already found real,
    previously-undocumented quirks that only surfaced via live testing —
    so this was verified live, not assumed: see DESIGN.md's prompt-caching
    decision for the actual deployed result.
    """
    params = (
        {
            # display="summarized" is required to get any
            # reasoningText.text at all (confirmed by testing the raw
            # Bedrock Converse API directly, bypassing Strands, on
            # 2026-08-24: with just {"type": "adaptive"},
            # reasoningText.text came back empty even on a turn that did
            # produce a reasoningContent block).
            "thinking": {"type": "adaptive", "display": "summarized"},
        }
        if enable_adaptive_thinking
        else None
    )
    model_config: dict[str, Any] = {
        "client_args": {
            # auth_token sends "Authorization: Bearer <token>" instead of
            # Anthropic's own "x-api-key" header — required here since the
            # Gateway's inbound authorizer validates a JWT bearer token,
            # not an Anthropic API key (confirmed against the Anthropic
            # Python SDK's own client parameters).
            "auth_token": gateway_token,
            "base_url": GATEWAY_INFERENCE_URL,
        },
        "model_id": _bedrock_model_id_for_gateway(model_id),
        "max_tokens": max_tokens,
        # Enables the "reasoning" stream_agent_turn() event (Claude's
        # extended-thinking content) for the diagnostic panel — see this
        # function's own docstring for why this is conditional, not
        # always applied.
        "params": params,
    }
    if enable_prompt_caching:
        model_config["cache_config"] = CacheConfig(
            strategy="anthropic", system_prompt_ttl=True, ttl=PROMPT_CACHE_TTL
        )
        model_config["cache_tools"] = CacheToolsConfig(ttl=PROMPT_CACHE_TTL)
    return AnthropicModel(**model_config)


async def build_model_router() -> ModelRouter:
    """Build this request's model provider — a tiered ModelRouter over the Gateway's inference target.

    Educational feature exercising Strands' ModelRouter/ClassifierStrategy
    API directly (strands-agents>=1.54.0, the version that introduced
    ClassifierStrategy — confirmed via a binary-search of PyPI wheels,
    since 1.52.0/1.53.0 export no such class at all). Not a load-bearing
    production capability — see DESIGN.md's model-routing decision for
    the full rationale, including why a custom RoutingStrategy was
    rejected in favor of the real built-in class.

    Three model roles, all Gateway-routed AnthropicModel instances built
    from the SAME already-exchanged OBO token (see invoke(), which fetches
    it once via _get_cached_or_exchange_gateway_token() before calling
    this function — not once per role): a cheap serving candidate
    (HAIKU_MODEL_ID), the capable serving candidate (MODEL_ID, the
    project's existing default model), and a dedicated classifier model
    (also HAIKU_MODEL_ID, matching Strands' own docs example of using a
    small/cheap/deterministic model purely for the routing decision, not
    real answers). One token authenticates all three regardless of which
    ends up serving the turn — the Gateway's OBO exchange is scoped to
    the caller's identity, not to any one model, so this is not three
    separate exchange costs the way a naive per-candidate implementation
    might assume.

    ClassifierStrategy(..., temperature/max_tokens/streaming kwargs are
    NOT accepted directly by its constructor — those are properties of
    the classifier Model instance itself, not the strategy) is configured
    with a small max_tokens (64, matching Strands' own docs example: the
    classifier's only job is a one-field structured-output decision, not
    real generation) and this module's own _ROUTING_POLICY_SYSTEM_PROMPT
    (extending ClassifierStrategy's built-in default policy with explicit
    tool-triggering escalation signals specific to this agent — see that
    constant's own docstring).

    Raises RuntimeError if GATEWAY_INFERENCE_URL is unset — this is a
    deploy-time misconfiguration (RuntimeStack always wires this from
    GatewayStack's inference target), not something a request can recover
    from, so failing loudly here is preferable to silently falling back
    to a call path this project no longer supports.

    Also raises RuntimeError (does not catch) if the Runtime's own
    inbound authorizer isn't JWT-configured (no workload access token
    available) — same fail-loud rationale as build_mcp_client()'s
    identical check: that combination means the OBO exchange this path
    depends on has no subject token to work from.

    A third, previously-confirmed 403 source for this path (distinct from
    the model-ID-mapping and adaptive-thinking cases documented in
    _build_gateway_anthropic_model()'s own docstring): ClassifierStrategy
    copies the parent Agent's full system_prompt (prompts.py's
    SYSTEM_PROMPT, not just _ROUTING_POLICY_SYSTEM_PROMPT above) verbatim
    into every classifier call, and JSON-escapes '<'/'>' as \u003c/\u003e
    — so any literal text in SYSTEM_PROMPT ending in dots immediately
    before a tag (e.g. an ellipsis-style "<tag>...</tag>" example) can
    produce a literal "..\" sequence that an undocumented AWS-managed WAF
    in front of the Gateway's inference endpoint blocks with a bare,
    unlogged 403 — 100% reproducing, with zero Gateway app log or
    CloudTrail correlation, since the block happens at an edge/ALB layer
    before the request reaches Gateway application code. If
    ClassifierStrategy starts failing this way again, check SYSTEM_PROMPT
    for this pattern before assuming a model/auth regression — see
    prompts.py's comment above SYSTEM_PROMPT and
    .kiro/notes/agentcore-gateway-waf-403-root-cause.md for the full
    investigation.
    """
    if not GATEWAY_INFERENCE_URL:
        raise RuntimeError(
            "GATEWAY_INFERENCE_URL is not set — the agent has no model-call "
            "path without it (there is no direct-bedrock-runtime fallback). "
            "RuntimeStack should always wire this from GatewayStack's "
            "inference target; check the Runtime's environment variables."
        )

    workload_access_token = BedrockAgentCoreContext.get_workload_access_token()
    if not workload_access_token:
        raise RuntimeError(
            "GATEWAY_INFERENCE_URL is set but no workload access token is "
            "available in context — the Runtime's own inbound authorizer must "
            "be JWT-configured (RuntimeStack's Okta config) for the Gateway "
            "OBO exchange this path depends on to have a subject token to "
            "work from."
        )
    # Fetched once here, not once per model role — see this function's own
    # docstring on why one token serves all three constructions below.
    gateway_token = await _get_cached_or_exchange_gateway_token(workload_access_token)

    cheap_candidate = RoutingCandidate(
        _build_gateway_anthropic_model(
            HAIKU_MODEL_ID,
            gateway_token,
            max_tokens=MAX_OUTPUT_TOKENS,
            enable_adaptive_thinking=False,
            enable_prompt_caching=True,
        ),
        name="cheap",
        description=(
            "Lower-cost, lower-latency model for purely conversational turns that need no "
            "tool use or research — greetings, small talk, recalling something already "
            "stated in this conversation, or simple clarifying-question exchanges."
        ),
    )
    capable_candidate = RoutingCandidate(
        _build_gateway_anthropic_model(
            MODEL_ID, gateway_token, max_tokens=MAX_OUTPUT_TOKENS, enable_prompt_caching=True
        ),
        name="capable",
        description=(
            "Higher-capability model for turns likely to need tool calls (weather, places, "
            "web search, code interpreter) or multi-step reasoning — itinerary generation, "
            "anything mentioning specific dates/places/weather, or budget arithmetic."
        ),
    )
    # enable_prompt_caching intentionally omitted here — caching the
    # classifier's own request is a separate, unverified question (does
    # ClassifierStrategy expose any control over the request it builds
    # internally?) explicitly left out of this project's educational
    # prompt-caching scope; see build_model_router()'s own module-level
    # decision record in DESIGN.md.
    classifier_model = _build_gateway_anthropic_model(
        HAIKU_MODEL_ID, gateway_token, max_tokens=64, enable_adaptive_thinking=False
    )

    return ModelRouter(
        # First-declared candidate is the router's own default (served
        # when the strategy declines) — "capable" first, not "cheap", so
        # a classifier failure/timeout fails toward the safer, more
        # capable option rather than silently under-serving a request
        # (mirrors this agent's own "when uncertain, prefer the capable
        # candidate" instruction in _ROUTING_POLICY_SYSTEM_PROMPT).
        models=[capable_candidate, cheap_candidate],
        strategy=ClassifierStrategy(
            classifier_model,
            system_prompt=_ROUTING_POLICY_SYSTEM_PROMPT,
        ),
    )


_ROUTING_STATE_KEY_SUBSTRING = "model_routing"


def _selected_candidate_name(event: AfterModelCallEvent) -> Optional[str]:
    """Best-effort read of which RoutingCandidate served this model call.

    Strands' ModelRouter stores its per-invocation selection in
    event.invocation_state under a key containing "model_routing" (see
    strands.models.routing.router's own _ROUTING_KEY_PREFIX constant) —
    there is no public API for reading the selected candidate back, so
    this reads the same invocation_state dict every hook callback already
    receives rather than reconstructing the router's private key format
    exactly (which also depends on id()-based object identity this module
    has no access to). Confirmed working via direct experimentation
    against strands-agents==1.54.0 before relying on it — not assumed
    from reading source alone. Returns None (never raises) if the shape
    ever changes upstream, so a Strands internals rename degrades this
    purely-observational feature gracefully rather than breaking a real
    chat turn.
    """
    try:
        for key, value in event.invocation_state.items():
            if _ROUTING_STATE_KEY_SUBSTRING in key:
                candidate = getattr(value, "candidate", None)
                if candidate is not None:
                    return candidate.name
    except Exception:  # noqa: BLE001 - purely observational, must never break a turn
        pass
    return None


async def build_mcp_client() -> Optional[MCPClient]:
    """Build the MCP client for the AgentCore Gateway.

    Returns None if GATEWAY_URL isn't configured, so the agent can still run
    (without tools) rather than failing outright.

    Two auth modes, mirroring GatewayStack's own IAM/JWT authorizer switch
    (mutually exclusive per AWS's docs — see this module's own docstring):

    - Default (GATEWAY_OBO_PROVIDER_NAME empty): requests are SigV4-signed
      (aws_iam_streamablehttp_client) using the Runtime's execution role
      credentials, which is what actually authorizes against a Gateway
      configured with GatewayAuthorizer.using_aws_iam() — the Runtime's
      execution role IAM policy alone is not sufficient; the request
      itself must be signed, or the Gateway returns 401 Unauthorized.
    - Phase 3 (GATEWAY_OBO_PROVIDER_NAME set): performs an RFC 8693
      On-Behalf-Of token exchange via AgentCore Identity
      (bedrock_agentcore.identity.auth.IdentityClient.get_token(),
      auth_flow="ON_BEHALF_OF_TOKEN_EXCHANGE") using the workload access
      token Runtime already delivered for this request (read via
      BedrockAgentCoreContext.get_workload_access_token() — never fetched
      by this code itself; see RuntimeStack's docstring for why
      GetWorkloadAccessToken* is deliberately not granted to this
      Runtime's execution role), then presents the resulting Gateway-
      audienced JWT as a plain `Authorization: Bearer` header via the
      base mcp library's streamablehttp_client — no AWS-specific request
      signing, since this Gateway is JWT-authorized once configured this
      way, not IAM-authorized. Passes custom_parameters={"subject_token_type":
      GATEWAY_OBO_SUBJECT_TOKEN_TYPE} — confirmed live (Okta System Log:
      "invalid_subject_token_type") that AgentCore's default
      subject_token_type ("...token-type:jwt") is rejected by this Okta
      org's custom authorization server, which only accepts
      "...token-type:access_token" for the TOKEN_EXCHANGE grant; see the
      constant's own comment for the full citation. The actual exchange
      call, and its per-`sub` caching, live in
      _get_cached_or_exchange_gateway_token() — see that function's
      docstring for why caching is scoped per-caller rather than global.

    Raises (does not catch) if the workload access token is missing when
    GATEWAY_OBO_PROVIDER_NAME is set — that combination means the Runtime's
    own inbound auth isn't actually JWT-configured to match, a
    configuration mismatch worth failing loudly on rather than silently
    falling back to an unauthenticated/misconfigured Gateway call.
    """
    if not GATEWAY_URL:
        logger.warning("GATEWAY_URL not set; running without Gateway tools")
        return None

    if not GATEWAY_OBO_PROVIDER_NAME:
        return MCPClient(
            lambda: aws_iam_streamablehttp_client(
                endpoint=GATEWAY_URL,
                aws_service="bedrock-agentcore",
                aws_region=AWS_REGION,
            )
        )

    workload_access_token = BedrockAgentCoreContext.get_workload_access_token()
    if not workload_access_token:
        raise RuntimeError(
            "GATEWAY_OBO_PROVIDER_NAME is set but no workload access token is "
            "available in context — the Runtime's own inbound authorizer must "
            "be JWT-configured (RuntimeStack's Okta config) for this OBO "
            "exchange to have a subject token to work from."
        )

    gateway_token = await _get_cached_or_exchange_gateway_token(workload_access_token)

    return MCPClient(
        lambda: streamablehttp_client(
            url=GATEWAY_URL,
            headers={"Authorization": f"Bearer {gateway_token}"},
        )
    )


async def _get_cached_or_exchange_gateway_token(workload_access_token: str) -> str:
    """Return a cached Gateway OBO JWT for this request's caller, or exchange a new one.

    Cache key is the *inbound* token's `sub` claim (via
    _sub_from_authorization_header()) — not the workload access token
    itself, which AgentCore mints per-invocation and so would never hit
    the cache. Correctness (not just efficiency) depends on caching being
    scoped to `sub`: see _GATEWAY_OBO_TOKEN_CACHE's own comment.

    Falls back to skipping the cache entirely (always exchanges fresh) if
    `sub` can't be determined — this should only happen if inbound JWT
    auth isn't actually configured to match GATEWAY_OBO_PROVIDER_NAME
    (build_mcp_client() already raises above if there's no workload token
    at all, but a present-but-unparseable inbound token is a narrower edge
    case not worth failing the whole request over).
    """
    cache_key = _sub_from_authorization_header()
    now = time.time()
    _raw_headers = BedrockAgentCoreContext.get_request_headers()
    logger.info(
        "Gateway OBO cache: resolved cache_key=%r (header keys present=%r)",
        cache_key,
        sorted(_raw_headers.keys()) if _raw_headers else _raw_headers,
    )

    if cache_key is not None:
        with _GATEWAY_OBO_TOKEN_CACHE_LOCK:
            # Opportunistic sweep of every expired entry, not just this
            # caller's — keeps the dict from growing unbounded across many
            # distinct users over a long-lived container's lifetime.
            stale_keys = [
                key for key, (_, expires_at) in _GATEWAY_OBO_TOKEN_CACHE.items() if expires_at <= now
            ]
            for stale_key in stale_keys:
                del _GATEWAY_OBO_TOKEN_CACHE[stale_key]
            if stale_keys:
                logger.info("Gateway OBO cache: swept %d expired entr(y/ies)", len(stale_keys))

            cached = _GATEWAY_OBO_TOKEN_CACHE.get(cache_key)
            if cached is not None:
                token, expires_at = cached
                if expires_at > now:
                    logger.info(
                        "Gateway OBO cache: HIT for cache_key=%r (expires in %.1fs)",
                        cache_key,
                        expires_at - now,
                    )
                    return token
                logger.info(
                    "Gateway OBO cache: STALE entry for cache_key=%r (expired %.1fs ago)",
                    cache_key,
                    now - expires_at,
                )
            else:
                logger.info("Gateway OBO cache: MISS for cache_key=%r (no entry)", cache_key)
    else:
        logger.info("Gateway OBO cache: bypassed — no cache_key (sub not resolvable)")

    logger.info("Gateway OBO cache: performing token exchange (provider=%s)", GATEWAY_OBO_PROVIDER_NAME)
    identity_client = IdentityClient(AWS_REGION)
    gateway_token = await identity_client.get_token(
        provider_name=GATEWAY_OBO_PROVIDER_NAME,
        scopes=[GATEWAY_OBO_SCOPE],
        audiences=[GATEWAY_OBO_AUDIENCE],
        agent_identity_token=workload_access_token,
        auth_flow="ON_BEHALF_OF_TOKEN_EXCHANGE",
        custom_parameters={"subject_token_type": GATEWAY_OBO_SUBJECT_TOKEN_TYPE},
    )

    if cache_key is not None:
        expires_at = _jwt_expiry(gateway_token)
        if expires_at is not None:
            with _GATEWAY_OBO_TOKEN_CACHE_LOCK:
                _GATEWAY_OBO_TOKEN_CACHE[cache_key] = (
                    gateway_token,
                    expires_at - GATEWAY_OBO_TOKEN_REFRESH_SKEW_SECONDS,
                )
            logger.info(
                "Gateway OBO cache: STORED for cache_key=%r (raw exp in %.1fs, "
                "cached TTL %.1fs after %ds skew)",
                cache_key,
                expires_at - now,
                expires_at - now - GATEWAY_OBO_TOKEN_REFRESH_SKEW_SECONDS,
                GATEWAY_OBO_TOKEN_REFRESH_SKEW_SECONDS,
            )
        else:
            logger.info(
                "Gateway OBO cache: NOT STORED for cache_key=%r — exchanged token has no "
                "decodable `exp` claim",
                cache_key,
            )

    return gateway_token


def _jwt_expiry(token: str) -> Optional[float]:
    """Best-effort extraction of a JWT's `exp` claim as a Unix timestamp.

    Decoded without verifying the signature — this is AgentCore Identity's
    own freshly-minted response, not untrusted client input, so there's
    nothing to verify against here; this is purely reading a claim off a
    token this process just received directly from a trusted AWS API call.
    Returns None if the token isn't a decodable JWT or has no `exp`, so
    the caller can fall back to not caching that token at all rather than
    caching it with a made-up TTL.
    """
    try:
        import jwt as _jwt

        claims = _jwt.decode(token, options={"verify_signature": False})
        exp = claims.get("exp")
        if exp is None:
            logger.info("Gateway OBO cache: exchanged token has no `exp` claim")
        return float(exp) if exp is not None else None
    except Exception as exc:  # noqa: BLE001 - best-effort; caller skips caching on failure
        logger.info("Gateway OBO cache: failed to decode exchanged token for exp: %r", exc)
        return None


def build_conversation_manager() -> SummarizingConversationManager:
    """Build the conversation manager that bounds in-session context growth.

    Without this, Strands' own default (an unconfigured SlidingWindowConversationManager,
    window_size=40) silently drops the oldest messages once a session passes ~15-20 turns,
    with no summarization and no protection for anything said early in the conversation —
    a real risk here, since a traveler's opening constraints (dates, party, budget, hard
    must-avoids) need to still hold at turn 40 of a long itinerary-planning session.

    - proactive_compression=True: compress ahead of a hard overflow (at ~70% of the
      context window used) rather than waiting for a ContextWindowOverflowException,
      which is SummarizingConversationManager's default (reactive-only) behavior.
    - pin_first=6: permanently protects the first 6 messages (the traveler's opening
      request and the agent's initial clarifying exchange) from summarization or eviction.
      This is a blunt "protect the prefix" instrument, not a semantic one — constraints
      stated later in a long clarifying back-and-forth aren't covered by it.
    - summarization_agent is intentionally left unset: this reuses the same Sonnet model
      already configured for the agent's normal turns (Strands calls agent.model directly
      for the summarization call in that case). A separate, possibly cheaper model for
      summarization specifically is a deliberate backlog item, not implemented here.
    """
    return SummarizingConversationManager(proactive_compression=True, pin_first=6)


@app.entrypoint
async def invoke(payload: dict, context: Any = None):
    """AgentCore Runtime entrypoint: one turn of the itinerary conversation.

    Payload: {"prompt": "<user message>", "actor_id": "<caller-supplied id>"}
    "actor_id" is optional (falls back to DEFAULT_ACTOR_ID — see
    get_actor_id()) but should always be supplied by a real caller: the
    hosted web UI's ECS task derives it from its ALB's verified OIDC claims
    (DESIGN.md decision #37).

    Streams labeled diagnostic events as an SSE response — BedrockAgentCoreApp
    auto-detects that this is an async generator (confirmed against real
    source: inspect.isasyncgen(result) in _handle_invocation) and wraps it as
    a text/event-stream StreamingResponse, converting each yielded dict to a
    "data: <json>\n\n" frame automatically. See stream_agent_turn() for the
    event shapes yielded: reasoning | text | tool_use | tool_result | routing | done | error.

    A malformed request (missing prompt) still needs a single event, not a
    plain dict return — BedrockAgentCoreApp's streaming detection is based
    on the entrypoint function itself being a generator, and an async
    generator function always returns an async generator object even on an
    early "return" (which just ends iteration after zero yields), so the
    error case below yields one error event rather than returning a dict.
    """
    user_message = (payload or {}).get("prompt", "").strip()
    if not user_message:
        yield {"type": "error", "data": {"note": "'prompt' is required"}}
        return

    runtime_session_id = getattr(context, "session_id", None) if context else None
    session_id = parse_session_id(runtime_session_id)
    actor_id = get_actor_id(payload)

    session_manager = build_session_manager(actor_id, session_id)
    mcp_client = await build_mcp_client()
    model = await build_model_router()

    if mcp_client is None:
        agent = Agent(
            model=model,
            tools=[build_code_interpreter_tool()],
            system_prompt=SYSTEM_PROMPT,
            session_manager=session_manager,
            conversation_manager=build_conversation_manager(),
            plugins=[build_skills_plugin(), build_date_context_injector()],
        )
        async for event in stream_agent_turn(agent, user_message):
            yield event
        return

    with mcp_client:
        tools = mcp_client.list_tools_sync()
        logger.info("Loaded %d tools from Gateway", len(tools))

        agent = Agent(
            model=model,
            tools=[build_code_interpreter_tool()] + tools,
            system_prompt=SYSTEM_PROMPT,
            session_manager=session_manager,
            conversation_manager=build_conversation_manager(),
            plugins=[build_skills_plugin(), build_date_context_injector()],
        )
        async for event in stream_agent_turn(agent, user_message):
            yield event


if __name__ == "__main__":
    app.run()
