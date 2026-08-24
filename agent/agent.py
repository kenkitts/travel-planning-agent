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

Configuration is read from environment variables set by RuntimeStack:
  GATEWAY_URL  - the AgentCore Gateway's MCP endpoint
  MEMORY_ID    - the AgentCore Memory resource ID
  AWS_REGION   - region for the Memory client (falls back to boto3 default)
  MODEL_ID     - Bedrock model ID for Claude Sonnet (has a sane default)

A fresh MCPClient and Agent are built per request (not shared globally),
following the documented safe pattern for AgentCore Runtime: it avoids
cross-request state leakage and thread-safety issues if concurrent
invocations land on the same container.
"""
import logging
import os
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

# Runtime session IDs follow the "<actorId>___<sessionId>" convention used
# across AgentCore Runtime samples.
SESSION_ID_SEPARATOR = "___"
DEFAULT_ACTOR_ID = "anonymous-traveler"

app = BedrockAgentCoreApp()


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


def run_agent_turn(agent: Agent, user_message: str) -> str:
    """Run one turn and return the assistant's plain-text reply.

    Handles strands.types.exceptions.MaxTokensReachedException gracefully:
    if the model runs out of its output token budget mid-response (confirmed
    in production for a long, tool-heavy multi-day itinerary), Strands has
    already appended the partial assistant message to `agent.messages`
    before raising — rather than letting that exception propagate out of
    the AgentCore Runtime entrypoint as an opaque InvokeAgentRuntime 500
    error, return the partial text with a note so the traveler still gets a
    usable (if truncated) answer and knows to ask for more.
    """
    try:
        result = agent(user_message)
        return extract_response_text(result.message)
    except MaxTokensReachedException:
        logger.warning("Model hit max_tokens mid-response; returning partial reply")
        partial_text = extract_response_text(agent.messages[-1])
        note = (
            "\n\n*(That response got cut off — it was longer than I could send in one "
            "go. Ask me to continue, or ask for a shorter version, and I'll pick up "
            "where I left off.)*"
        )
        return (partial_text + note) if partial_text else (
            "Sorry, that response got cut off before I could write anything back to "
            "you. Could you try again, maybe asking for a shorter answer?"
        )


def parse_runtime_session_id(runtime_session_id: Optional[str]) -> tuple[str, str]:
    """Split a Runtime session ID into (actor_id, session_id).

    Falls back to a default actor and the raw session id if the expected
    separator isn't present, so a misconfigured caller degrades gracefully
    rather than crashing the whole request.
    """
    if not runtime_session_id:
        return DEFAULT_ACTOR_ID, "default-session"

    if SESSION_ID_SEPARATOR in runtime_session_id:
        actor_id, session_id = runtime_session_id.split(SESSION_ID_SEPARATOR, 1)
        return actor_id, session_id

    logger.warning(
        "runtime session id %r missing '%s' separator; using default actor",
        runtime_session_id,
        SESSION_ID_SEPARATOR,
    )
    return DEFAULT_ACTOR_ID, runtime_session_id


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
def invoke(payload: dict, context: Any = None) -> dict:
    """AgentCore Runtime entrypoint: one turn of the itinerary conversation.

    Payload: {"prompt": "<user message>"}
    Returns: {"response": "<assistant markdown reply>"}
    """
    user_message = (payload or {}).get("prompt", "").strip()
    if not user_message:
        return {"error": "'prompt' is required"}

    runtime_session_id = getattr(context, "session_id", None) if context else None
    actor_id, session_id = parse_runtime_session_id(runtime_session_id)

    session_manager = build_session_manager(actor_id, session_id)
    mcp_client = build_mcp_client()
    model = BedrockModel(
        model_id=MODEL_ID, region_name=AWS_REGION, max_tokens=MAX_OUTPUT_TOKENS
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
        return {"response": run_agent_turn(agent, user_message)}

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
        return {"response": run_agent_turn(agent, user_message)}


if __name__ == "__main__":
    app.run()
