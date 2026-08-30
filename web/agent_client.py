"""Client logic for invoking the Travel Planning Agent's AgentCore Runtime.

Used by `web/server.py` — the hosted web UI is the only supported client
for this agent (the local CLI REPL was removed; see DESIGN.md's CLI
removal decision). This module implements session-ID construction and the
actual Runtime invocation call.

The Runtime's entrypoint (`agent/agent.py`'s `invoke()`) is a streaming
async generator, so every invocation response comes back as an SSE-shaped
body — there is no non-streaming path to fall back to. `stream_agent_events()`
does incremental reads of that body and yields one parsed event dict per
frame, matching the `{"type": ..., "data": ...}` shape `agent/agent.py`'s
`stream_agent_turn()` produces.

Auth: JWT bearer token (Phase 2 auth rearchitecture), via a raw HTTPS
POST — NOT boto3. AWS's own docs are explicit that this is a hard
requirement, not a style choice: "An AgentCore Runtime can support
either IAM SigV4 or JWT Bearer Token based inbound auth, but not both
simultaneously," and "boto3 doesn't support invocation with bearer
tokens, you'll need to use an HTTP client like the requests library."
(https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-oauth.html,
confirmed directly against AWS's documentation before writing this
module — this is not an assumption). This reverts DESIGN.md decision #37
(IAM/SigV4, chosen when the Runtime had no other trust boundary) now that
Phase 1 gave the web server a real, verified Okta identity to exchange
for a Runtime-scoped JWT (see web/auth.py's exchange_token_for_runtime()).

The caller (web/server.py's /api/chat route) resolves the bearer token
per-request from the caller's own session (via
auth.get_or_exchange_runtime_token()) — this module has no knowledge of
Okta, cookies, or token exchange; it only knows how to send a bearer
token it's handed to the Runtime's invocation URL.

`actor_id` is passed explicitly in the invocation payload (see
`build_invoke_payload()`), not derived server-side from a bearer token —
the caller is trusted to supply the correct value; this predates and is
unrelated to the bearer-token change above (the Runtime's JWT authorizer
only validates that *some* valid, correctly-scoped token was presented,
it does not participate in deriving actor_id — see agent/agent.py's
get_actor_id(), which still reads it from the payload).

No retry/backoff logic is implemented for this HTTPS call for
documented-retryable responses (ThrottlingException, the 409
RetryableConflictException) — boto3 previously handled retries for these
transparently; dropping boto3 for the JWT-bearer-token requirement above
also drops that behavior. This is a known, intentional gap (not an
oversight) — see DESIGN.md's Phase 2 decision for why it was scoped out
rather than reimplemented here.
"""
import json
import urllib.parse
import uuid
from typing import Iterator, Optional

import requests

# InvokeAgentRuntime requires runtimeSessionId to be 33-256 characters.
MIN_SESSION_ID_LENGTH = 33
SESSION_ID_SEPARATOR = "___"

# Runtime session IDs are still formatted as "<placeholder>___<sessionId>"
# for compatibility with the existing AgentCore Runtime convention and the
# >=33-character requirement — the placeholder component is purely
# cosmetic; it is not used for Memory scoping (see build_invoke_payload()).
_SESSION_ID_PLACEHOLDER = "session"

# Timeout for establishing the connection and receiving the first byte of
# the streaming response — generous since real itinerary-generation turns
# have taken up to ~56s in practice (see README's ALB idle-timeout note);
# this is a per-attempt connect+first-byte timeout, not a total-response
# timeout (streaming reads have no overall deadline once started).
_REQUEST_TIMEOUT_SECONDS = 60.0


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

    actor_id is required here (not optional) at this layer — the caller
    (web/server.py, from the verified Okta session established in
    Phase 1) must decide who the user is before invoking, since
    agent/agent.py's get_actor_id() only falls back to a shared default
    actor when the field is entirely absent, which would silently merge
    every caller's long-term memory together.
    """
    return {"prompt": prompt, "actor_id": actor_id}


def _build_invocation_url(agent_runtime_arn: str, region: str, qualifier: Optional[str]) -> str:
    """Build the direct-Runtime HTTPS invocation URL.

    Matches the exact form AWS's own docs use for a bearer-token
    invocation with no fronting Gateway (this project's Gateway sits
    between the agent process and its own tools, not in front of the
    Runtime — see DESIGN.md's Phase 2 decision: Gateway auth is
    deliberately untouched by this change):
    https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{url-encoded-arn}/invocations?qualifier=...
    """
    escaped_arn = urllib.parse.quote(agent_runtime_arn, safe="")
    url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{escaped_arn}/invocations"
    if qualifier:
        url += f"?{urllib.parse.urlencode({'qualifier': qualifier})}"
    return url


def stream_agent_events(
    agent_runtime_arn: str,
    region: str,
    runtime_session_id: str,
    prompt: str,
    actor_id: str,
    bearer_token: str,
    qualifier: Optional[str] = None,
) -> Iterator[dict]:
    """Send one turn to the agent and yield its labeled events as they arrive.

    Each yielded dict matches agent/agent.py's stream_agent_turn() shape:
    {"type": "reasoning" | "text" | "tool_use" | "tool_result" | "done" | "error",
     "data": ...}.

    Raw HTTPS + JWT bearer token (Phase 2 — see this module's docstring
    for why boto3 cannot be used here at all once the Runtime's inbound
    auth is JWT-based, not IAM). `bearer_token` is the Runtime-audienced
    JWT already resolved by the caller (web/server.py, via
    auth.get_or_exchange_runtime_token()) — this function does not
    itself check its validity/expiry.

    Raises RuntimeError with a user-facing message on any failure (a
    network/HTTP failure, a non-2xx response, or a malformed SSE frame).
    Does not raise on an in-band {"type": "error"} event from the agent
    itself — that's a normal part of the stream (e.g. the
    MaxTokensReachedException cutoff case) and callers should handle it
    like any other event, not treat it as a transport failure.
    """
    url = _build_invocation_url(agent_runtime_arn, region, qualifier)
    payload = build_invoke_payload(prompt, actor_id)
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": runtime_session_id,
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            data=json.dumps(payload).encode("utf-8"),
            stream=True,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"Failed to invoke agent: {e}") from e

    if not response.ok:
        # Mirrors boto3's ClientError-on-non-2xx behavior — the response
        # body for an error is JSON, not an SSE stream, so read it
        # directly rather than treating it as a streaming body.
        try:
            detail = response.text
        except Exception:  # noqa: BLE001 - best-effort error detail only
            detail = "<no response body>"
        raise RuntimeError(f"Failed to invoke agent ({response.status_code}): {detail}")

    content_type = response.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        raise RuntimeError(
            f"Agent did not return a streaming response (contentType={content_type!r})"
        )

    for raw_line in response.iter_lines():
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

