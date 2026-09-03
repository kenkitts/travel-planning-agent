"""Travel Planning Agent — Strands Agent hosted on Amazon Bedrock AgentCore Runtime.

Wires together:
  - Claude Sonnet via Bedrock — either directly (strands.models.BedrockModel,
    the default) or, when GATEWAY_INFERENCE_URL is set, via the Gateway's
    own inference target (strands.models.anthropic.AnthropicModel, pointed
    at the Gateway's /inference/v1/messages path instead of calling
    bedrock-runtime directly) for centralized governance/rate-limiting —
    see DESIGN.md's Gateway-routed-inference decision and build_model()
    below. Uses the same OBO-cached Gateway token as the tools path.
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
diagnostic events (reasoning/text/tool_use/tool_result/done/error) as an
SSE response, one per turn of agent.stream_async(). BedrockAgentCoreApp
auto-detects the async-generator return and wraps it as a
text/event-stream StreamingResponse — see stream_agent_turn() for the
event-shape translation and MaxTokensReachedException handling.

Configuration is read from environment variables set by RuntimeStack:
  GATEWAY_URL               - the AgentCore Gateway's MCP endpoint
  MEMORY_ID                 - the AgentCore Memory resource ID
  AWS_REGION                - region for the Memory client (falls back to boto3 default)
  MODEL_ID                  - Bedrock model ID for Claude Sonnet (has a sane default)
  GATEWAY_OBO_PROVIDER_NAME - name of the AgentCore Identity OAuth2 credential
                              provider used for the Gateway's RFC 8693 On-Behalf-Of
                              token exchange (see build_mcp_client() below); empty
                              string if GatewayStack's JWT authorizer isn't configured
  GATEWAY_INFERENCE_URL     - base URL of the Gateway's inference target
                              (".../inference"); when non-empty, routes model calls
                              through it (AnthropicModel) instead of calling
                              bedrock-runtime directly (BedrockModel) — see
                              build_model() below. Always wired by RuntimeStack
                              (the target always exists); empty means "don't use it",
                              not "not configured" — opting in is this env var's
                              only job

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
full design.

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
from strands.models import BedrockModel
from strands.models.anthropic import AnthropicModel
from strands.types.exceptions import MaxTokensReachedException, ModelThrottledException
from strands.tools.mcp.mcp_client import MCPClient

from prompts import build_system_prompt

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
# The Gateway-routed path (build_model()'s AnthropicModel branch) needs
# the bare foundation-model ID, not the "us." cross-region-inference-
# profile-prefixed form MODEL_ID uses for the direct BedrockModel/
# Converse path. Found live: routing a request for
# "us.anthropic.claude-sonnet-5" through the Gateway's bedrock-mantle
# connector target returned a real Anthropic-shaped 404 ("Model
# 'us.anthropic.claude-sonnet-5' not found on any target") — confirmed
# via `aws bedrock list-foundation-models` that the canonical model ID
# is the bare "anthropic.claude-sonnet-5" (inferenceTypesSupported:
# INFERENCE_PROFILE — the "us." prefix is specifically a cross-region-
# inference-profile wrapper meaningful to bedrock-runtime's own
# Converse/InvokeModel APIs, not a concept bedrock-mantle's model
# routing resolves). Stripping the "us." prefix here (not a general
# regex/split on MODEL_ID, since that would silently mis-strip a
# differently-shaped future MODEL_ID) — this constant exists
# specifically for this one known prefix on this one known model.
GATEWAY_INFERENCE_MODEL_ID = (
    MODEL_ID[len("us.") :] if MODEL_ID.startswith("us.") else MODEL_ID
)
# Bedrock's Converse API defaults to a fairly low per-model max output token
# limit if maxTokens is omitted from the request (BedrockModel only sets it
# when max_tokens is explicitly configured). A real multi-day, tool-grounded
# itinerary response can be long — confirmed in production: a 3-day trip
# request that made 13 tool calls before writing its answer hit
# strands.types.exceptions.MaxTokensReachedException partway through the
# itinerary, which surfaced to callers as an opaque
# InvokeAgentRuntime 500 error. 8192 gives long itineraries realistic room
# to complete; MAX_TOKENS_REACHED is still handled gracefully in invoke()
# below in case an even longer response exceeds this.
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "8192"))

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
    """
    seen_tool_use_ids: set[str] = set()
    try:
        async for event in agent.stream_async(user_message):
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


async def build_model() -> BedrockModel | AnthropicModel:
    """Build this request's model provider.

    Default: BedrockModel calling bedrock-runtime directly (unchanged
    behavior). When GATEWAY_INFERENCE_URL is set, returns an
    AnthropicModel pointed at the Gateway's own inference target instead
    — see this module's docstring and DESIGN.md's Gateway-routed-
    inference decision for the full rationale (why bedrock-runtime over
    bedrock-mantle, why the Anthropic Messages API path specifically).

    Both paths configure the identical adaptive-thinking request shape
    (see the shared _THINKING_REQUEST_FIELDS comment below) — Anthropic's
    Messages API and Bedrock's Converse API accept the same
    `{"type": "adaptive", "display": "summarized"}` value for this field
    (confirmed against Anthropic's own docs), just under a different
    Strands parameter name per provider (`additional_request_fields` for
    BedrockModel, `params` for AnthropicModel, which merges directly into
    the outgoing request body).

    The Gateway-routed path reuses the same OBO-cached Gateway token as
    build_mcp_client() (Question 1 of this feature's design interview:
    one token, one cache entry per caller, no separate exchange) — this
    call is a second, independent cache lookup/exchange for the same
    request, not a shared object with the MCP client's own call, but
    _get_cached_or_exchange_gateway_token()'s cache means only the first
    of the two actually round-trips to AgentCore Identity/Okta.

    Raises (does not catch) if GATEWAY_INFERENCE_URL is set but no
    workload access token is available — same fail-loud rationale as
    build_mcp_client()'s identical check: that combination means the
    Runtime's own inbound auth isn't actually JWT-configured to match.
    """
    if not GATEWAY_INFERENCE_URL:
        return BedrockModel(
            model_id=MODEL_ID,
            region_name=AWS_REGION,
            max_tokens=MAX_OUTPUT_TOKENS,
            # Enables the "reasoning" stream_agent_turn() event (Claude's
            # extended-thinking content) for the diagnostic panel. Off by
            # default on Bedrock — without this, Claude never emits
            # reasoningContent at all, so the "reasoning" branch below has
            # nothing to translate (confirmed live: a real turn produced
            # tool_use/tool_result/text/done but zero reasoning events before
            # this was added). Claude Sonnet 5 specifically requires the
            # *adaptive* form — the older manual form
            # (thinking: {"type": "enabled", "budget_tokens": N}) is removed on
            # this model and returns a 400 error (confirmed against AWS's own
            # Claude migration-guide docs); adaptive thinking lets the model
            # decide per-request whether/how much to think, with no token
            # budget to tune. This means some turns will now spend extra tokens
            # (cost/latency) thinking that previously spent none — an accepted
            # tradeoff for the diagnostic visibility this project wants.
            #
            # display="summarized" is required to get any reasoningText.text
            # at all — confirmed by testing the raw Bedrock Converse API
            # directly (bypassing Strands) on 2026-08-24: with just
            # {"type": "adaptive"}, reasoningText.text came back as an empty
            # string even on a turn that did produce a reasoningContent block —
            # the actual thinking was locked in an opaque, non-text `signature`
            # blob. Adding display="summarized" made the same kind of prompt
            # return real, human-readable summarized reasoning text.
            #
            # REMAINING LIMITATION (still real, unaffected by display): Claude
            # Sonnet 5 decides per-request whether to think at all — a
            # tool-heavy, multi-step itinerary-planning turn produced zero
            # reasoningContent blocks even with display="summarized" set, while
            # a plain multi-step reasoning question did produce one. So the
            # "reasoning" event in stream_agent_turn() will still be
            # inconsistent turn-to-turn on this model; that's expected, not a
            # bug — it now actually has text to show on the turns where the
            # model chooses to think.
            additional_request_fields={"thinking": {"type": "adaptive", "display": "summarized"}},
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
    gateway_token = await _get_cached_or_exchange_gateway_token(workload_access_token)

    return AnthropicModel(
        client_args={
            # auth_token sends "Authorization: Bearer <token>" instead of
            # Anthropic's own "x-api-key" header — required here since the
            # Gateway's inbound authorizer validates a JWT bearer token,
            # not an Anthropic API key (confirmed against the Anthropic
            # Python SDK's own client parameters).
            "auth_token": gateway_token,
            "base_url": GATEWAY_INFERENCE_URL,
        },
        model_id=GATEWAY_INFERENCE_MODEL_ID,
        max_tokens=MAX_OUTPUT_TOKENS,
        # Same adaptive-thinking config as the BedrockModel path above,
        # under AnthropicModel's own "params" passthrough (merged directly
        # into the Messages API request body) rather than
        # "additional_request_fields" — see this function's docstring.
        params={"thinking": {"type": "adaptive", "display": "summarized"}},
    )


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
    event shapes yielded: reasoning | text | tool_use | tool_result | done | error.

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
    model = await build_model()
    # UTC "today" — there's no per-traveler timezone collected from the
    # conversation (see prompts.py's requirements list), so this is the only
    # unambiguous default. AgentCore Runtime containers run in UTC, so
    # date.today() is already UTC here. At day granularity this only
    # misaligns with a traveler's actual local date within a few hours of
    # UTC midnight, which doesn't meaningfully affect itinerary date math.
    system_prompt = build_system_prompt(date.today().isoformat())

    if mcp_client is None:
        agent = Agent(
            model=model,
            system_prompt=system_prompt,
            session_manager=session_manager,
            conversation_manager=build_conversation_manager(),
        )
        async for event in stream_agent_turn(agent, user_message):
            yield event
        return

    with mcp_client:
        tools = mcp_client.list_tools_sync()
        logger.info("Loaded %d tools from Gateway", len(tools))

        agent = Agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            session_manager=session_manager,
            conversation_manager=build_conversation_manager(),
        )
        async for event in stream_agent_turn(agent, user_message):
            yield event


if __name__ == "__main__":
    app.run()
