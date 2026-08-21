"""Shared client logic for invoking the Travel Planning Agent's AgentCore Runtime.

Used by both `cli/chat.py` (REPL) and `web/server.py` (local web UI) so the
two clients share one implementation of session-ID construction and the
`InvokeAgentRuntime` call, rather than maintaining duplicate copies.

Auth: standard AWS credential resolution (env vars, profile, IAM role).
The invoking principal must have `bedrock-agentcore:InvokeAgentRuntime`
permission on the target agent runtime (IAM-only auth, per DESIGN.md
decision #15 — there is no separate API key or bearer token).
"""
import json
import uuid
from typing import Optional

from botocore.exceptions import BotoCoreError, ClientError

# InvokeAgentRuntime requires runtimeSessionId to be 33-256 characters.
MIN_SESSION_ID_LENGTH = 33
SESSION_ID_SEPARATOR = "___"


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


def invoke_agent(
    client,
    agent_runtime_arn: str,
    runtime_session_id: str,
    prompt: str,
    qualifier: Optional[str] = None,
) -> str:
    """Send one turn to the agent and return its text response.

    Raises RuntimeError with a user-facing message on any failure (AWS
    error, malformed response, or an error payload from the agent itself).
    """
    payload = json.dumps({"prompt": prompt}).encode("utf-8")

    kwargs = {
        "agentRuntimeArn": agent_runtime_arn,
        "runtimeSessionId": runtime_session_id,
        "payload": payload,
        "contentType": "application/json",
        "accept": "application/json",
    }
    if qualifier:
        kwargs["qualifier"] = qualifier

    try:
        result = client.invoke_agent_runtime(**kwargs)
    except (ClientError, BotoCoreError) as e:
        raise RuntimeError(f"Failed to invoke agent: {e}") from e

    body = result["response"].read()
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Agent returned non-JSON response: {body!r}") from e

    if "error" in data:
        raise RuntimeError(f"Agent returned an error: {data['error']}")
    if "response" not in data:
        raise RuntimeError(f"Agent response missing 'response' field: {data!r}")

    return data["response"]
