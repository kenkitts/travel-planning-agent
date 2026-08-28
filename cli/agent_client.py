"""Shared client logic for invoking the Travel Planning Agent's AgentCore Runtime.

Used by both `cli/chat.py` (REPL) and `web/server.py` (local/hosted web UI)
so the two clients share one implementation of session-ID construction and
the actual Runtime invocation call, rather than maintaining duplicate
copies.

The Runtime's entrypoint (`agent/agent.py`'s `invoke()`) is a streaming
async generator, so every invocation response comes back as an SSE-shaped
body — there is no non-streaming path to fall back to. `stream_agent_events()`
does incremental reads of that body and yields one parsed event dict per
frame, matching the `{"type": ..., "data": ...}` shape `agent/agent.py`'s
`stream_agent_turn()` produces.

Auth: IAM/SigV4, via boto3's `bedrock-agentcore` `invoke_agent_runtime`
(DESIGN.md decision #37, reverting the Okta-JWT bearer-token cutover of
decisions #26-35). This is a full reversion to the original decision #15
IAM-only auth model — every caller (this CLI, or `web/server.py`'s ECS
task) uses its own AWS credentials, and boto3's own SDK is fully
supported again (JWT/OAuth bearer auth was the one case where AWS's docs
say the SDK can't be used — see the AWS for Industries blog post "Automate
customer complaint classification with AI agents on AWS" for that specific
caveat, which no longer applies here).

`actor_id` is now passed explicitly in the invocation payload (see
`build_invoke_payload()`), not derived server-side from a bearer token —
the caller is trusted to supply the correct value, the same way it's
already trusted with full IAM access to invoke this Runtime at all.
"""
import json
import uuid
from typing import Iterator, Optional

import boto3

# InvokeAgentRuntime requires runtimeSessionId to be 33-256 characters.
MIN_SESSION_ID_LENGTH = 33
SESSION_ID_SEPARATOR = "___"

# Runtime session IDs are still formatted as "<placeholder>___<sessionId>"
# for compatibility with the existing AgentCore Runtime convention and the
# >=33-character requirement — the placeholder component is purely
# cosmetic; it is not used for Memory scoping (see build_invoke_payload()).
_SESSION_ID_PLACEHOLDER = "session"


def build_runtime_session_id() -> str:
    """Build a fresh runtime session ID as "<placeholder>___<uuid>".

    A fresh UUID4 hex (32 chars) is used as the session component. Padded
    if needed so the full string always satisfies AgentCore Runtime's
    33-character minimum for runtimeSessionId.
    """
    session_component = uuid.uuid4().hex
    session_id = f"{_SESSION_ID_PLACEHOLDER}{SESSION_ID_SEPARATOR}{session_component}"
    if len(session_id) < MIN_SESSION_ID_LENGTH:
        session_id = session_id.ljust(MIN_SESSION_ID_LENGTH, "0")
    return session_id


def build_invoke_payload(prompt: str, actor_id: str) -> dict:
    """Build the JSON payload sent to the Runtime's invoke() entrypoint.

    actor_id is required here (not optional) at this layer — callers must
    decide who the user is (a --actor-id flag for the CLI; the ALB's
    verified OIDC claims for the web UI) before invoking, since
    agent/agent.py's get_actor_id() only falls back to a shared default
    actor when the field is entirely absent, which would silently merge
    every caller's long-term memory together.
    """
    return {"prompt": prompt, "actor_id": actor_id}


def stream_agent_events(
    agent_runtime_arn: str,
    region: str,
    runtime_session_id: str,
    prompt: str,
    actor_id: str,
    qualifier: Optional[str] = None,
) -> Iterator[dict]:
    """Send one turn to the agent and yield its labeled events as they arrive.

    Each yielded dict matches agent/agent.py's stream_agent_turn() shape:
    {"type": "reasoning" | "text" | "tool_use" | "tool_result" | "done" | "error",
     "data": ...}.

    Uses boto3's invoke_agent_runtime (IAM/SigV4, DESIGN.md decision #37).
    The response's "response" field is a botocore StreamingBody — a
    file-like object whose .iter_lines() behaves the same way httpx's did
    for the previous bearer-token transport, so the SSE-frame parsing below
    is unchanged from that implementation.

    Raises RuntimeError with a user-facing message on any failure (a
    ClientError from boto3, or a malformed SSE frame). Does not raise on an
    in-band {"type": "error"} event from the agent itself — that's a normal
    part of the stream (e.g. the MaxTokensReachedException cutoff case) and
    callers should handle it like any other event, not treat it as a
    transport failure.
    """
    client = boto3.client("bedrock-agentcore", region_name=region)
    payload = build_invoke_payload(prompt, actor_id)

    kwargs = {
        "agentRuntimeArn": agent_runtime_arn,
        "runtimeSessionId": runtime_session_id,
        "contentType": "application/json",
        "accept": "text/event-stream",
        "payload": json.dumps(payload).encode("utf-8"),
    }
    if qualifier:
        kwargs["qualifier"] = qualifier

    try:
        response = client.invoke_agent_runtime(**kwargs)
    except Exception as e:  # noqa: BLE001 - surfaced as a clean RuntimeError
        raise RuntimeError(f"Failed to invoke agent: {e}") from e

    content_type = response.get("contentType", "")
    if "text/event-stream" not in content_type:
        raise RuntimeError(
            f"Agent did not return a streaming response (contentType={content_type!r})"
        )

    body = response["response"]
    for raw_line in body.iter_lines():
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        if not line:
            continue
        if not line.startswith("data: "):
            continue
        data_str = line[len("data: "):]
        try:
            event = json.loads(data_str)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Agent returned a malformed SSE frame: {data_str!r}") from e
        if not isinstance(event, dict) or "type" not in event:
            raise RuntimeError(f"Agent event missing 'type' field: {event!r}")
        yield event
