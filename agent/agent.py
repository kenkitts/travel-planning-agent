"""Travel Planning Agent — Strands Agent hosted on Amazon Bedrock AgentCore Runtime.

Wires together:
  - Claude Sonnet via Bedrock (strands.models.BedrockModel)
  - Tools from the AgentCore Gateway (Web Search + weather + places), over
    MCP with AWS SigV4 (IAM) request signing. The Runtime's execution role
    has bedrock-agentcore:InvokeGateway, but that permission only takes
    effect if the outbound request is actually SigV4-signed — the Gateway
    otherwise responds 401 Unauthorized (confirmed against a real deployment;
    the Runtime does not sign Gateway calls automatically).
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
  GATEWAY_URL  - the AgentCore Gateway's MCP endpoint
  MEMORY_ID    - the AgentCore Memory resource ID
  AWS_REGION   - region for the Memory client (falls back to boto3 default)
  MODEL_ID     - Bedrock model ID for Claude Sonnet (has a sane default)

Auth: the Runtime is configured with IAM/SigV4 auth (DESIGN.md decision
#37, reverting the Okta-JWT cutover of decisions #26-35) — every caller
signs its own request with its own AWS credentials, and there is no bearer
token to decode here. actor_id is instead read directly from the
invocation payload (see get_actor_id() below) — the caller (the web
container, deriving it from its ALB's verified OIDC claims; or the CLI,
from a --actor-id flag) is trusted to supply the right value, the same way
any other AgentCore Runtime IAM-authenticated caller is trusted with the
payload it sends. This replaces the old "<actorId>___<sessionId>"
runtime-session-id convention's actor_id half (the session_id half is
unchanged; see parse_session_id()).

A fresh MCPClient and Agent are built per request (not shared globally),
following the documented safe pattern for AgentCore Runtime: it avoids
cross-request state leakage and thread-safety issues if concurrent
invocations land on the same container.
"""
import json
import logging
import os
import re
from datetime import date
from typing import Any, Optional

from bedrock_agentcore.memory.integrations.strands.config import (
    AgentCoreMemoryConfig,
    RetrievalConfig,
)
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from strands import Agent
from strands.agent.conversation_manager import SummarizingConversationManager
from strands.models import BedrockModel
from strands.types.exceptions import MaxTokensReachedException
from strands.tools.mcp.mcp_client import MCPClient

from prompts import build_system_prompt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("GATEWAY_URL", "")
MEMORY_ID = os.environ.get("MEMORY_ID", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
# Sonnet is the design's chosen model (see DESIGN.md decision #7) for its
# multi-step reasoning and tool-use reliability across the three Gateway tools.
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-5")
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
# purely cosmetic (kept only so runtimeSessionId strings built by the CLI/
# web UI still satisfy the ">=33 chars" / "<x>___<y>" format they were
# already producing) — the real actor_id now comes from the payload (see
# get_actor_id() below), not from this string.
SESSION_ID_SEPARATOR = "___"
DEFAULT_ACTOR_ID = "anonymous-traveler"

app = BedrockAgentCoreApp()


def get_actor_id(payload: dict) -> str:
    """Derive the actor_id for Memory scoping from the invocation payload.

    DESIGN.md decision #37: with IAM/SigV4 auth, the Runtime has no bearer
    token to decode a `sub` claim from — the caller (the web container,
    deriving this from its ALB's verified OIDC claims; the CLI, from a
    --actor-id flag) is trusted to supply the correct actor_id directly in
    the payload, the same way any IAM-authenticated caller's payload is
    already trusted. This is a real trust shift from decision #31's
    "derive server-side from a cryptographically verified token, never
    from client input" stance — accepted because the callers that can
    reach this Runtime at all are now either (a) this project's own web
    container, sitting behind an OIDC-authenticated ALB that already did
    real identity verification before the container ever sees the request,
    or (b) a local CLI user invoking with their own AWS credentials, who
    was already trusted with full IAM access to this Runtime to begin
    with. There is no untrusted public path that can supply an arbitrary
    actor_id — see DESIGN.md decision #37 for the full rationale.

    The raw actor_id is sanitized before use as AgentCore Memory's
    actorId, not used verbatim — see sanitize_actor_id()'s docstring.

    Falls back to DEFAULT_ACTOR_ID if the payload doesn't include one, so
    the agent still runs (without real per-user memory isolation) rather
    than crashing the whole request.
    """
    raw = (payload or {}).get("actor_id")
    if not raw:
        logger.warning("No 'actor_id' in payload; using default actor")
        return DEFAULT_ACTOR_ID
    return sanitize_actor_id(str(raw))


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


def build_mcp_client() -> Optional[MCPClient]:
    """Build the MCP client for the AgentCore Gateway.

    Returns None if GATEWAY_URL isn't configured, so the agent can still run
    (without tools) rather than failing outright.

    Requests are SigV4-signed (aws_iam_streamablehttp_client) using the
    Runtime's execution role credentials, which is what actually authorizes
    against a Gateway configured with GatewayAuthorizer.using_aws_iam() —
    the Runtime's execution role IAM policy alone is not sufficient; the
    request itself must be signed, or the Gateway returns 401 Unauthorized.
    """
    if not GATEWAY_URL:
        logger.warning("GATEWAY_URL not set; running without Gateway tools")
        return None

    return MCPClient(
        lambda: aws_iam_streamablehttp_client(
            endpoint=GATEWAY_URL,
            aws_service="bedrock-agentcore",
            aws_region=AWS_REGION,
        )
    )


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
    get_actor_id()) but should always be supplied by a real caller: the web
    container derives it from its ALB's verified OIDC claims, and the CLI
    takes it from a --actor-id flag (DESIGN.md decision #37).

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
    mcp_client = build_mcp_client()
    model = BedrockModel(
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
