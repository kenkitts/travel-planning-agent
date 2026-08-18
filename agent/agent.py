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
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient

from prompts import SYSTEM_PROMPT

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("GATEWAY_URL", "")
MEMORY_ID = os.environ.get("MEMORY_ID", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
# Sonnet is the design's chosen model (see DESIGN.md decision #7) for its
# multi-step reasoning and tool-use reliability across the three Gateway tools.
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

# Namespace patterns must match those configured on the Memory resource in
# cdk/stacks/memory_stack.py.
USER_PREFERENCE_NAMESPACE = "/travel-agent/actor/{actorId}/preferences"
SUMMARIZATION_NAMESPACE = "/travel-agent/actor/{actorId}/session/{sessionId}/summary"

# Runtime session IDs follow the "<actorId>___<sessionId>" convention used
# across AgentCore Runtime samples.
SESSION_ID_SEPARATOR = "___"
DEFAULT_ACTOR_ID = "anonymous-traveler"

app = BedrockAgentCoreApp()


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
            USER_PREFERENCE_NAMESPACE: RetrievalConfig(top_k=5, relevance_score=0.5),
            SUMMARIZATION_NAMESPACE: RetrievalConfig(top_k=3, relevance_score=0.3),
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
    model = BedrockModel(model_id=MODEL_ID, region_name=AWS_REGION)

    if mcp_client is None:
        agent = Agent(
            model=model,
            system_prompt=SYSTEM_PROMPT,
            session_manager=session_manager,
        )
        result = agent(user_message)
        return {"response": str(result.message)}

    with mcp_client:
        tools = mcp_client.list_tools_sync()
        logger.info("Loaded %d tools from Gateway", len(tools))

        agent = Agent(
            model=model,
            tools=tools,
            system_prompt=SYSTEM_PROMPT,
            session_manager=session_manager,
        )
        result = agent(user_message)
        return {"response": str(result.message)}


if __name__ == "__main__":
    app.run()
