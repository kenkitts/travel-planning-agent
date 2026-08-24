"""Shared client logic for invoking the Travel Planning Agent's AgentCore Runtime.

Used by both `cli/chat.py` (REPL) and `web/server.py` (local web UI) so the
two clients share one implementation of session-ID construction and the
`InvokeAgentRuntime` call, rather than maintaining duplicate copies.

The Runtime's entrypoint (`agent/agent.py`'s `invoke()`) is a streaming
async generator, so every InvokeAgentRuntime response comes back as an SSE
body (`contentType: text/event-stream`) — there is no non-streaming path to
fall back to. `stream_agent_events()` does incremental reads of that body
(`response["response"].iter_lines(...)`, per AWS's own documented pattern
for this API) and yields one parsed event dict per SSE frame, matching the
`{"type": ..., "data": ...}` shape `agent/agent.py`'s `stream_agent_turn()`
produces.

Auth: standard AWS credential resolution (env vars, profile, IAM role).
The invoking principal must have `bedrock-agentcore:InvokeAgentRuntime`
permission on the target agent runtime (IAM-only auth, per DESIGN.md
decision #15 — there is no separate API key or bearer token).
"""
import json
import uuid
from typing import Iterator, Optional

from botocore.exceptions import BotoCoreError, ClientError

# InvokeAgentRuntime requires runtimeSessionId to be 33-256 characters.
MIN_SESSION_ID_LENGTH = 33
SESSION_ID_SEPARATOR = "___"

# SSE frames are "data: <json>\n\n" (see BedrockAgentCoreApp._convert_to_sse
# in the agent's dependencies) — this is the wire-format prefix to strip.
_SSE_DATA_PREFIX = "data: "


def build_runtime_session_id(actor_id: str) -> str:
    """Build a runtime session ID as "<actorId>___<sessionId>".

    A fresh UUID4 hex (32 chars) is used as the session component. Padded
    if needed so the full string always satisfies AgentCore Runtime's
    33-character minimum for runtimeSessionId, regardless of actor_id length.
    """
    session_component = uuid.uuid4().hex
    session_id = f"{actor_id}{SESSION_ID_SEPARATOR}{session_component}"
    if len(session_id) < MIN_SESSION_ID_LENGTH:
        session_id = session_id.ljust(MIN_SESSION_ID_LENGTH, "0")
    return session_id


def stream_agent_events(
    client,
    agent_runtime_arn: str,
    runtime_session_id: str,
    prompt: str,
    qualifier: Optional[str] = None,
) -> Iterator[dict]:
    """Send one turn to the agent and yield its labeled events as they arrive.

    Each yielded dict matches agent/agent.py's stream_agent_turn() shape:
    {"type": "reasoning" | "text" | "tool_use" | "tool_result" | "done" | "error",
     "data": ...}.

    Raises RuntimeError with a user-facing message on any failure (AWS
    error, a non-streaming/unexpected contentType, or a malformed SSE
    frame). Does not raise on an in-band {"type": "error"} event from the
    agent itself — that's a normal part of the stream (e.g. the
    MaxTokensReachedException cutoff case) and callers should handle it
    like any other event, not treat it as a transport failure.
    """
    payload = json.dumps({"prompt": prompt}).encode("utf-8")

    kwargs = {
        "agentRuntimeArn": agent_runtime_arn,
        "runtimeSessionId": runtime_session_id,
        "payload": payload,
        "contentType": "application/json",
        "accept": "text/event-stream",
    }
    if qualifier:
        kwargs["qualifier"] = qualifier

    try:
        result = client.invoke_agent_runtime(**kwargs)
    except (ClientError, BotoCoreError) as e:
        raise RuntimeError(f"Failed to invoke agent: {e}") from e

    content_type = result.get("contentType", "")
    if "text/event-stream" not in content_type:
        raise RuntimeError(
            f"Agent did not return a streaming response (contentType={content_type!r})"
        )

    for line in result["response"].iter_lines(chunk_size=10):
        if not line:
            continue
        decoded = line.decode("utf-8") if isinstance(line, bytes) else line
        if not decoded.startswith(_SSE_DATA_PREFIX):
            continue
        data_str = decoded[len(_SSE_DATA_PREFIX):]
        try:
            event = json.loads(data_str)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Agent returned a malformed SSE frame: {data_str!r}") from e
        if not isinstance(event, dict) or "type" not in event:
            raise RuntimeError(f"Agent event missing 'type' field: {event!r}")
        yield event
