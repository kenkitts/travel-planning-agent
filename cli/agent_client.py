"""Shared client logic for invoking the Travel Planning Agent's AgentCore Runtime.

Used by both `cli/chat.py` (REPL) and `web/server.py` (local web UI) so the
two clients share one implementation of session-ID construction, Okta token
acquisition, and the actual Runtime invocation call, rather than
maintaining duplicate copies.

The Runtime's entrypoint (`agent/agent.py`'s `invoke()`) is a streaming
async generator, so every invocation response comes back as an SSE body
(`contentType: text/event-stream`) — there is no non-streaming path to
fall back to. `stream_agent_events()` does incremental reads of that body
and yields one parsed event dict per SSE frame, matching the
`{"type": ..., "data": ...}` shape `agent/agent.py`'s `stream_agent_turn()`
produces.

Auth: Okta-issued JWT bearer tokens (DESIGN.md decisions #26-35), not IAM/
SigV4. This supersedes decision #15. boto3's `bedrock-agentcore` client
does not support bearer-token auth for `InvokeAgentRuntime` — confirmed
against AWS's own documentation ("Automate customer complaint
classification with AI agents on AWS", AWS for Industries blog: JWT bearer
auth requires "HTTPS requests required, not managed by AWS SDKs", unlike
IAM SigV4's full boto3/CLI support). So this module makes a raw HTTPS call
with `httpx` instead of using boto3's `invoke_agent_runtime`, following the
same URL/header pattern AWS's own docs and multiple real reference
implementations use for JWT-authenticated Runtime invocation:
  POST https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{urlencoded_arn}/invocations?qualifier={qualifier}
  Headers: Authorization: Bearer <token>, Content-Type: application/json,
           Accept: text/event-stream,
           X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: <session_id>
The SSE frame parsing (`data: <json>` lines) is unchanged from the previous
boto3-based implementation.

Token acquisition (`get_okta_access_token()`) shells out to the user's
existing `okta-claude-code-token-helper` script per DESIGN.md decision #32
— that script owns all PKCE/refresh/caching/locking logic; this module just
invokes it with this project's own Okta app config (from `.env`, see
`.env.template`) passed as real environment variables.
"""
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import quote

import httpx

# InvokeAgentRuntime requires runtimeSessionId to be 33-256 characters.
MIN_SESSION_ID_LENGTH = 33
SESSION_ID_SEPARATOR = "___"

# Read timeout for a single agent turn. Long, multi-tool-call itinerary
# turns have taken up to ~60s in practice (see PLAN.md Phase 7 notes);
# 300s matches the generous timeout used elsewhere in this project's own
# httpx-based examples for this same API.
_REQUEST_TIMEOUT_SECONDS = 300.0

# Env vars this project's own .env supplies for the token helper subprocess
# (see .env.template). OKTA_TOKEN_HELPER_PATH is consumed here directly
# (not forwarded to the subprocess); the rest are passed through verbatim.
_HELPER_ENV_VARS = ("OKTA_ISSUER", "OKTA_CLIENT_ID", "OKTA_SCOPES", "OKTA_REDIRECT_PORT")
_DEFAULT_HELPER_PATH = "~/okta-claude-code-token-helper/okta-claude-code-token.py"


def load_dotenv(path: Optional[str] = None) -> None:
    """Load simple KEY=VALUE lines from a .env file into os.environ.

    Mirrors okta-claude-code-token.py's own `_load_dotenv()` (same
    semantics: real environment variables already set are never
    overwritten). Defaults to a `.env` file at the repo root (one directory
    up from this file's parent, i.e. `cli/../.env`).
    """
    dotenv_path = Path(path) if path else Path(__file__).resolve().parent.parent / ".env"
    if not dotenv_path.is_file():
        return
    for line in dotenv_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def get_okta_access_token() -> str:
    """Acquire a bearer access token via the okta-claude-code-token-helper script.

    Invokes the helper as a subprocess with this project's own Okta app
    configuration (OKTA_ISSUER, OKTA_CLIENT_ID, OKTA_SCOPES,
    OKTA_REDIRECT_PORT — read from .env via load_dotenv(), or the real
    environment) passed explicitly, so it authenticates against the
    Travel Agent's dedicated Okta app rather than that script's own
    configured app (DESIGN.md decision #27/#32).

    Re-invoked on every call rather than cached here: the helper script
    already handles "valid cached token -> instant," "expired but
    refreshable -> silent refresh," and "neither -> interactive login,"
    with its own cross-process locking (DESIGN.md decision #33).

    Raises RuntimeError with the subprocess's stderr output if the helper
    exits nonzero (e.g. no valid/refreshable token cached and this isn't a
    real interactive terminal — the helper's own "run manually from a real
    terminal first" failure mode).
    """
    load_dotenv()

    missing = [var for var in _HELPER_ENV_VARS if not os.environ.get(var)]
    if missing:
        raise RuntimeError(
            f"Missing required Okta config: {', '.join(missing)}. "
            "Copy .env.template to .env and fill in your Okta app's details."
        )

    helper_path = Path(
        os.environ.get("OKTA_TOKEN_HELPER_PATH", _DEFAULT_HELPER_PATH)
    ).expanduser()

    subprocess_env = dict(os.environ)
    for var in _HELPER_ENV_VARS:
        subprocess_env[var] = os.environ[var]

    try:
        result = subprocess.run(
            ["python3", str(helper_path)],
            env=subprocess_env,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            f"Okta token helper script not found at {helper_path}. Set "
            "OKTA_TOKEN_HELPER_PATH in .env if it's installed elsewhere."
        ) from e

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(
            "Failed to acquire an Okta access token"
            + (f": {stderr}" if stderr else " (see stderr above for details).")
        )

    token = result.stdout.strip()
    if not token:
        raise RuntimeError("Okta token helper exited successfully but printed no token.")
    return token


def build_runtime_session_id(actor_id: str) -> str:
    """Build a runtime session ID as "<actorId>___<sessionId>".

    A fresh UUID4 hex (32 chars) is used as the session component. Padded
    if needed so the full string always satisfies AgentCore Runtime's
    33-character minimum for runtimeSessionId, regardless of actor_id length.

    `actor_id` here is purely a session-ID-formatting convenience (kept for
    compatibility with the "<actorId>___<sessionId>" convention already
    used across this project's Memory-scoping code) — it is NOT the
    authorization boundary. The Runtime derives the real actor_id
    server-side from the verified JWT's `sub` claim (DESIGN.md decision
    #31); a client-supplied actor_id string can no longer be trusted or
    used for Memory scoping.
    """
    session_component = uuid.uuid4().hex
    session_id = f"{actor_id}{SESSION_ID_SEPARATOR}{session_component}"
    if len(session_id) < MIN_SESSION_ID_LENGTH:
        session_id = session_id.ljust(MIN_SESSION_ID_LENGTH, "0")
    return session_id


def _invocation_url(agent_runtime_arn: str, region: str, qualifier: Optional[str]) -> str:
    """Build the raw AgentCore Runtime invocation URL for a JWT-authenticated call.

    Matches the URL shape documented by AWS and used by every reference
    implementation found for bearer-token InvokeAgentRuntime calls:
    https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{urlencoded_arn}/invocations
    """
    encoded_arn = quote(agent_runtime_arn, safe="")
    url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations"
    if qualifier:
        url += f"?qualifier={quote(qualifier, safe='')}"
    return url


def stream_agent_events(
    access_token: str,
    agent_runtime_arn: str,
    region: str,
    runtime_session_id: str,
    prompt: str,
    qualifier: Optional[str] = None,
) -> Iterator[dict]:
    """Send one turn to the agent and yield its labeled events as they arrive.

    Each yielded dict matches agent/agent.py's stream_agent_turn() shape:
    {"type": "reasoning" | "text" | "tool_use" | "tool_result" | "done" | "error",
     "data": ...}.

    Raises RuntimeError with a user-facing message on any failure (HTTP
    error, a non-streaming/unexpected content-type, or a malformed SSE
    frame). Does not raise on an in-band {"type": "error"} event from the
    agent itself — that's a normal part of the stream (e.g. the
    MaxTokensReachedException cutoff case) and callers should handle it
    like any other event, not treat it as a transport failure.
    """
    url = _invocation_url(agent_runtime_arn, region, qualifier)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": runtime_session_id,
    }
    payload = {"prompt": prompt}

    try:
        with httpx.stream(
            "POST", url, headers=headers, json=payload, timeout=_REQUEST_TIMEOUT_SECONDS
        ) as response:
            if response.status_code != 200:
                response.read()
                raise RuntimeError(
                    f"Failed to invoke agent: HTTP {response.status_code}: {response.text}"
                )

            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                raise RuntimeError(
                    f"Agent did not return a streaming response (contentType={content_type!r})"
                )

            for line in response.iter_lines():
                if not line:
                    continue
                if not line.startswith("data: "):
                    continue
                data_str = line[len("data: "):]
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError as e:
                    raise RuntimeError(
                        f"Agent returned a malformed SSE frame: {data_str!r}"
                    ) from e
                if not isinstance(event, dict) or "type" not in event:
                    raise RuntimeError(f"Agent event missing 'type' field: {event!r}")
                yield event
    except httpx.HTTPError as e:
        raise RuntimeError(f"Failed to invoke agent: {e}") from e
