#!/usr/bin/env python3
"""Hosted web UI backend for the Travel Planning Agent.

Runs on ECS Fargate behind an internet-facing Application Load Balancer
configured with OIDC authentication (`TravelAgentWebStack`, see
DESIGN.md decision #37 and PLAN.md Phase 10). The ALB authenticates every
browser session against Okta and forwards the verified identity to this
container per-request via the signed `x-amzn-oidc-data` header — this
server verifies that header's signature itself (see
`actor_id_from_oidc_header()`) rather than trusting it blindly, since a
misconfigured security group or a direct-to-target request could
otherwise let an unauthenticated caller spoof the header.

Serves a small chat UI and a streaming API that invokes the deployed
AgentCore Runtime agent via IAM/SigV4 (`cli/agent_client.py`, shared with
the CLI — DESIGN.md decision #37, reverting the Okta-JWT-bearer-token
Runtime auth used before this stack existed), plus read-only endpoints
that list and replay past conversations directly from AgentCore Memory (no
separate local storage — Memory is the source of truth).

Every logged-in person gets their own `actor_id` (and therefore their own
conversation history / long-term memory), derived per-request from the
ALB's verified OIDC claims — not from a single server-wide identity like
the old local-only version of this file. There is no login *page* of its
own: the ALB's `authenticate-oidc` listener rule handles the entire login
flow before a request ever reaches this process.

`POST /api/chat` streams the agent's response as Server-Sent Events —
every labeled event the Runtime emits (reasoning/text/tool_use/tool_result/
done/error, see agent/agent.py's stream_agent_turn()) is forwarded to the
browser verbatim, live as the turn progresses. The frontend renders only
"text" deltas into the chat bubble by default; a diagnostic toggle (off by
default, see static/app.js) additionally shows every event — including
full raw tool-result payloads — in a collapsible panel. Past-conversation
replay (the sidebar's `GET /api/conversations/{session_id}`) is unaffected
by this — it reads a session's already-persisted transcript from AgentCore
Memory in one shot, not a re-simulated stream.

Usage:
    python server.py --agent-runtime-arn <arn> --memory-id <id> \\
        --alb-arn <alb-arn> [--region <region>] [--qualifier <qualifier>] \\
        [--port <port>]

Auth: the ALB's OIDC listener rule gates every request before it reaches
this process — see `actor_id_from_oidc_header()` for the signature
verification this server performs on the forwarded claims. AWS
credentials for Memory access and Runtime invocation come from this
process's own IAM role (the ECS task role in the hosted deployment; your
local credentials if run outside ECS for testing) — a separate,
unrelated authorization boundary from the ALB's human-facing login.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import boto3
import jwt
import requests
import uvicorn
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Reuse the same session-ID + invocation logic as cli/chat.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))
from agent_client import build_runtime_session_id, stream_agent_events  # noqa: E402


STATIC_DIR = Path(__file__).resolve().parent / "static"

# Longest prompt fragment shown as a conversation's preview in the sidebar.
PREVIEW_MAX_CHARS = 80

# Matches cli/agent_client.py's SESSION_ID_SEPARATOR: a runtimeSessionId is
# "<placeholder>___<sessionId>", but AgentCore Memory's ListSessions/
# ListEvents APIs are already actor-scoped and only ever return/accept the
# bare <sessionId> component (confirmed live: ListSessions returned
# "0000...0000" for a runtime session ID of
# "smoke-test-history___0000...0000"). The conversation-history endpoints
# below reconstruct the full runtimeSessionId before returning it to the
# frontend, since that's what /api/chat (and InvokeAgentRuntime) require.
RUNTIME_SESSION_ID_SEPARATOR = "___"

# AgentCore Memory has no native "session title" field — ListSessions only
# ever returns {sessionId, actorId, createdAt} (confirmed against the real
# API; there's no metadata/label slot on a session itself). A user-set
# title is instead stored as `metadata` on a dedicated marker event, using
# CreateEvent's per-event metadata param (a real, separate feature from
# the conversational payload — up to 15 key-value pairs, confirmed in
# bedrock_agentcore.memory.client.MemoryClient.create_event's docstring).
# `list_conversations` reads the *latest* marker event's title and prefers
# it over the first-user-message preview; `_event_turns` skips marker
# events entirely so a rename never shows up as a fake chat turn.
TITLE_METADATA_KEY = "conversationTitle"
MAX_TITLE_CHARS = 80

# ALB signs x-amzn-oidc-data with ES256, keyed by region (DESIGN.md decision
# #37 / PLAN.md Phase 10 — see the AWS docs on "User claims encoding and
# signature verification" for Application Load Balancers). GovCloud uses a
# different (S3-backed) endpoint shape and is intentionally not supported
# here — this project only targets standard AWS partitions.
_ALB_PUBLIC_KEY_URL_TEMPLATE = "https://public-keys.auth.elb.{region}.amazonaws.com/{kid}"

# Small in-process cache of ALB signing keys, keyed by kid — ALB rotates
# these infrequently, and re-fetching one from the public-keys endpoint on
# every single request would add needless latency to every chat turn.
_alb_public_key_cache: dict[str, "cryptography.hazmat.primitives.asymmetric.ec.EllipticCurvePublicKey"] = {}


class ChatRequest(BaseModel):
    """Request body for POST /api/chat."""

    prompt: str
    session_id: str


class ConversationSummary(BaseModel):
    """One entry in the GET /api/conversations list."""

    session_id: str
    created_at: Optional[str] = None
    preview: str
    title: Optional[str] = None


class ConversationTurn(BaseModel):
    """One message in a GET /api/conversations/{session_id} transcript."""

    role: str
    text: str


class ConversationDetail(BaseModel):
    """Response body for GET /api/conversations/{session_id}."""

    session_id: str
    turns: list[ConversationTurn]


class SetTitleRequest(BaseModel):
    """Request body for PUT /api/conversations/{session_id}/title."""

    title: str


class DeleteConversationResponse(BaseModel):
    """Response body for DELETE /api/conversations/{session_id}."""

    session_id: str
    deleted_events: int
    failed_events: int


def _is_title_marker_event(event: dict) -> bool:
    """True if this event is a title-rename marker, not a real chat turn.

    Marker events carry `TITLE_METADATA_KEY` in their event-level
    `metadata` (set via CreateEvent's `metadata` param, not part of the
    conversational payload) — this is enough to identify and skip them
    without needing a special payload shape.
    """
    return TITLE_METADATA_KEY in (event.get("metadata") or {})


def _event_title(event: dict) -> Optional[str]:
    """Extract the title string from a title-marker event's metadata, if any."""
    value = (event.get("metadata") or {}).get(TITLE_METADATA_KEY)
    if isinstance(value, dict):
        return value.get("stringValue")
    return None


def _event_turns(event: dict) -> list[ConversationTurn]:
    """Extract (role, text) turns from one AgentCore Memory event's payload.

    Each event's `payload` is a list of items. Only `conversational` items
    are turns — `blob` items (e.g. the agent's session/conversation-manager
    state snapshot, written by Strands' session persistence, not by
    `AgentCoreMemorySessionManager`'s conversation history) are skipped,
    and so are title-marker events (see `_is_title_marker_event`) — a
    rename must never appear as a fake chat turn in the transcript.

    A conversational item's `content.text` is not the plain reply string —
    confirmed against a real event via a live ListEvents call — it's a
    JSON-encoded Strands `SessionMessage` dump:
    `{"message": {"role": ..., "content": [{"reasoningContent": {...}}, {"text": "..."}]}}`.
    The `role` on the outer `conversational` wrapper is reliable (`USER`/
    `ASSISTANT`) and used directly; the text itself must be extracted from
    the inner message's `content` list, concatenating only `text` blocks
    and skipping others (e.g. `reasoningContent`, Claude's extended-thinking
    signature blob, not meant for display). Falls back to the raw string if
    it isn't the expected JSON shape, so an unexpected/older event format
    degrades to showing something rather than nothing.
    """
    if _is_title_marker_event(event):
        return []

    turns = []
    for item in event.get("payload") or []:
        conv = item.get("conversational")
        if not conv:
            continue
        raw_text = (conv.get("content") or {}).get("text")
        role = conv.get("role")
        if not raw_text or not role:
            continue

        text = raw_text
        try:
            parsed = json.loads(raw_text)
            inner_content = parsed["message"]["content"]
            text = "\n".join(
                block["text"]
                for block in inner_content
                if isinstance(block, dict) and "text" in block
            ).strip()
        except (json.JSONDecodeError, KeyError, TypeError):
            pass  # Not the expected wrapper shape; use raw_text as-is.

        if text:
            turns.append(ConversationTurn(role=role.lower(), text=text))
    return turns


def _latest_title(events: list[dict]) -> Optional[str]:
    """Return the most recent user-set title for a session, if any.

    `events` is newest-first (as ListEvents returns it), so the first
    marker event encountered is the latest rename — no need to compare
    timestamps.
    """
    for event in events:
        if _is_title_marker_event(event):
            title = _event_title(event)
            if title:
                return title
    return None


def _first_user_preview(events: list[dict]) -> str:
    """Build a short preview string from a session's first user message.

    `list_events` returns newest-first, so events are scanned in reverse to
    find the earliest turn. Falls back to "(empty conversation)" if a
    session has events but none are a user turn (unexpected, but safer than
    a KeyError-driven 500).
    """
    for event in reversed(events):
        for turn in _event_turns(event):
            if turn.role == "user":
                text = " ".join(turn.text.split())
                if len(text) > PREVIEW_MAX_CHARS:
                    text = text[:PREVIEW_MAX_CHARS].rstrip() + "…"
                return text
    return "(empty conversation)"


def _list_all_event_ids(client, memory_id: str, actor_id: str, session_id: str) -> list[str]:
    """Collect every eventId for a session, paginating via nextToken.

    Used by DELETE /api/conversations/{session_id}: AgentCore Memory has no
    session-level delete API (confirmed via code search across dozens of
    real implementations — every one deletes a "session" by deleting all
    of its events one at a time; there is no DeleteSession/batch-delete-
    events operation). Collecting all IDs first, then deleting, avoids
    invalidating the pagination token mid-delete.
    """
    event_ids: list[str] = []
    next_token: Optional[str] = None
    while True:
        kwargs = {
            "memoryId": memory_id,
            "actorId": actor_id,
            "sessionId": session_id,
            "maxResults": 100,
            "includePayloads": False,  # IDs only; payloads are dead weight here.
        }
        if next_token:
            kwargs["nextToken"] = next_token
        response = client.list_events(**kwargs)
        event_ids.extend(
            event["eventId"] for event in response.get("events", []) if event.get("eventId")
        )
        next_token = response.get("nextToken")
        if not next_token:
            break
    return event_ids


def _fetch_alb_public_key(region: str, kid: str):
    """Fetch (and cache) the ALB signing public key for a given kid.

    Per AWS's documented signature-verification flow for ALB OIDC/JWT
    claims: the key ID comes from the JWT header's "kid" field, and the
    corresponding EC public key is published, unauthenticated, at a
    per-region well-known endpoint — not a JWKS document, just the raw PEM
    key body for that one kid. Cached in-process since ALB rotates these
    keys infrequently and every chat turn would otherwise pay this extra
    HTTP round-trip.
    """
    if kid in _alb_public_key_cache:
        return _alb_public_key_cache[kid]

    url = _ALB_PUBLIC_KEY_URL_TEMPLATE.format(region=region, kid=kid)
    response = requests.get(url, timeout=5.0)
    response.raise_for_status()
    from cryptography.hazmat.primitives import serialization

    public_key = serialization.load_pem_public_key(response.content)
    _alb_public_key_cache[kid] = public_key
    return public_key


def _verified_oidc_sub(
    oidc_data_header: Optional[str], region: str, alb_arn: str
) -> str:
    """Verify the ALB's signed OIDC claims header and return the raw `sub` claim.

    See actor_id_from_oidc_header() for the full verification rationale and
    the step-by-step flow this implements. Split out so /api/whoami can
    show the raw `sub` claim alongside its sanitized actor_id, while
    /api/chat and friends only ever need the sanitized form.
    """
    if not oidc_data_header:
        raise HTTPException(
            status_code=401, detail="Missing x-amzn-oidc-data header (request did not "
            "pass through the ALB's OIDC authentication)"
        )

    try:
        unverified_header = jwt.get_unverified_header(oidc_data_header)
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Malformed OIDC claims header: {e}") from e

    signer = unverified_header.get("signer")
    if signer != alb_arn:
        raise HTTPException(
            status_code=401,
            detail=f"OIDC claims signer {signer!r} does not match the expected ALB ARN",
        )

    kid = unverified_header.get("kid")
    if not kid:
        raise HTTPException(status_code=401, detail="OIDC claims header missing 'kid'")

    try:
        public_key = _fetch_alb_public_key(region, kid)
        claims = jwt.decode(oidc_data_header, key=public_key, algorithms=["ES256"])
    except (jwt.InvalidTokenError, requests.RequestException) as e:
        raise HTTPException(status_code=401, detail=f"OIDC claims verification failed: {e}") from e

    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="OIDC claims have no 'sub'")
    return sub


def actor_id_from_oidc_header(
    oidc_data_header: Optional[str], region: str, alb_arn: str
) -> str:
    """Derive and sanitize the actor_id from the ALB's signed OIDC claims.

    The ALB forwards the authenticated user's claims as a JWT in the
    x-amzn-oidc-data header — but per AWS's own documentation, this header
    MUST be signature-verified before anything in it is trusted: "you must
    verify the signature of x-amzn-oidc-data and confirm that the signer
    field in the JWT header matches your Application Load Balancer's ARN."
    Without this check, a request that reached this container directly
    (bypassing the ALB — e.g. a misconfigured security group, or a bug
    that let something other than the ALB reach the target group) could
    forge an arbitrary actor_id/sub claim and read or write another user's
    long-term Memory.

    Verification, per AWS's documented flow (implemented in
    _verified_oidc_sub(), which this wraps):
      1. Decode the JWT header (without verifying yet) to read "kid" and
         "signer".
      2. Confirm "signer" matches this deployment's actual ALB ARN — a
         token signed by a different load balancer (even one legitimately
         signed by AWS) must not be accepted here.
      3. Fetch (or reuse a cached) EC public key for that kid from ALB's
         public-keys endpoint.
      4. Verify the JWT's ES256 signature against that key, and decode the
         payload for real this time.
      5. Extract the "sub" claim and sanitize it (see _sanitize_actor_id())
         before use as AgentCore Memory's actorId, since sub is only
         OIDC-guaranteed to be a stable unique string, not one that
         satisfies Memory's actorId character-set restrictions (real-world
         case: an email-address-shaped sub).

    Raises HTTPException(401) if the header is missing, malformed, signed
    by an unexpected ALB, or fails signature verification — callers should
    never fall back to a shared/default actor for this header, unlike
    agent/agent.py's get_actor_id() fallback for its own (differently
    trusted) payload-based actor_id — a forged or missing OIDC header here
    means the request didn't come through the ALB's login gate at all.
    """
    sub = _verified_oidc_sub(oidc_data_header, region, alb_arn)
    return _sanitize_actor_id(sub)


# AgentCore Memory's actorId pattern: must start with an alphanumeric, then
# any run of alphanumerics/-/_// and optional ":"-separated segments of the
# same. Confirmed against the real ListEvents API pattern
# ("[a-zA-Z0-9][a-zA-Z0-9-_/]*(?::[a-zA-Z0-9-_/]+)*[a-zA-Z0-9-_/]*") after
# a live 502 (ValidationException on actorId) surfaced this Okta org's
# `sub` being an email address, which the pattern rejects verbatim.
_ACTOR_ID_DISALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9\-_/:]")
_DEFAULT_ACTOR_ID = "anonymous-traveler"


def _sanitize_actor_id(raw: str) -> str:
    """Map an arbitrary `sub` claim to a string valid as an AgentCore Memory actorId.

    Replaces every character outside the allowed set with "-", then strips
    any leading run of non-alphanumeric characters (the pattern requires
    the first character specifically be alphanumeric). Deterministic: the
    same `sub` always sanitizes to the same actorId. Mirrors
    agent/agent.py's sanitize_actor_id() exactly — keep the two in sync.
    """
    sanitized = _ACTOR_ID_DISALLOWED_CHARS.sub("-", raw)
    sanitized = sanitized.lstrip("-_/:")
    return sanitized or _DEFAULT_ACTOR_ID


def create_app(
    agent_runtime_arn: str,
    region: str,
    alb_arn: str,
    qualifier: Optional[str] = None,
    memory_id: Optional[str] = None,
) -> FastAPI:
    """Build the FastAPI app, wiring in the boto3 clients and CLI args.

    `alb_arn` is required for OIDC header signature verification (see
    actor_id_from_oidc_header()) — every request must present a
    x-amzn-oidc-data header signed by exactly this load balancer.
    """
    # Memory access (ListSessions/ListEvents/CreateEvent/DeleteEvent) and
    # Runtime invocation both use this process's own IAM role — the ECS
    # task role in the hosted deployment (scoped to the specific Memory
    # and Runtime ARNs; see cdk/stacks/web_stack.py), or local credentials
    # if run outside ECS.
    client = boto3.client("bedrock-agentcore", region_name=region)
    app = FastAPI(title="Travel Planning Agent — Web UI")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/config")
    def config() -> dict:
        # Tells the frontend whether to show the conversation-history
        # sidebar at all. No actor_id here — unlike the old local-only
        # version, actor_id now varies per-request (derived from each
        # request's own OIDC header), not fixed at server startup.
        return {"history_enabled": memory_id is not None}

    @app.get("/api/whoami")
    def whoami(request: Request) -> dict:
        """Debug endpoint: show the actor_id derived from this request's own
        verified OIDC claims, and the raw `sub` claim it was sanitized from.

        Reuses actor_id_from_oidc_header() exactly as /api/chat does, so
        this reflects the real, signature-verified identity for whoever is
        currently logged in — not a guess or a log-scraping exercise. Not
        linked from the UI; intended for manually confirming which actor_id
        a given login session maps to (e.g. via curl or a browser visit
        while logged in).
        """
        sub = _verified_oidc_sub(request.headers.get("x-amzn-oidc-data"), region, alb_arn)
        return {"sub": sub, "actor_id": _sanitize_actor_id(sub)}

    @app.post("/api/chat")
    def chat(request: Request, body: ChatRequest) -> StreamingResponse:
        """Stream one turn's labeled events to the browser as SSE.

        Forwards agent_client.stream_agent_events()'s parsed event dicts
        (already validated to have a "type" key) back out as SSE frames —
        the browser's EventSource-equivalent fetch/reader consumes these
        directly; see static/app.js. actor_id is derived from this
        request's own verified OIDC header (not cached from startup), so a
        long-running container correctly serves multiple distinct users
        without mixing up whose Memory a turn should read/write.
        """
        actor_id = actor_id_from_oidc_header(
            request.headers.get("x-amzn-oidc-data"), region, alb_arn
        )

        prompt = body.prompt.strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt must not be empty")
        # session_id (the AgentCore runtimeSessionId) is generated and
        # persisted client-side (localStorage) so a page reload continues
        # the same conversation; the server does not track sessions itself.
        if len(body.session_id) < 33:
            raise HTTPException(
                status_code=400,
                detail="session_id must be at least 33 characters "
                "(AgentCore Runtime requirement)",
            )

        def _event_stream():
            try:
                for event in stream_agent_events(
                    agent_runtime_arn, region, body.session_id, prompt, actor_id, qualifier
                ):
                    yield f"data: {json.dumps(event)}\n\n"
            except RuntimeError as e:
                # A transport-level failure (a boto3 ClientError, malformed
                # SSE from the Runtime) discovered mid-stream — the
                # response has already started with a 200, so this can't
                # become an HTTPException; forward it as one more in-band
                # error event instead, matching the shape of an
                # agent-raised error event.
                yield f"data: {json.dumps({'type': 'error', 'data': {'note': str(e)}})}\n\n"

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @app.get("/api/conversations", response_model=list[ConversationSummary])
    def list_conversations(request: Request) -> list[ConversationSummary]:
        if memory_id is None:
            raise HTTPException(
                status_code=404,
                detail="Conversation history is unavailable: server was started "
                "without --memory-id",
            )
        actor_id = actor_id_from_oidc_header(
            request.headers.get("x-amzn-oidc-data"), region, alb_arn
        )

        try:
            sessions_response = client.list_sessions(
                memoryId=memory_id, actorId=actor_id, maxResults=100
            )
        except ClientError as e:
            # A brand-new actor (no sessions written yet) is a normal,
            # empty state, not an error — confirmed live against a real
            # Memory resource: ListSessions raises ResourceNotFoundException
            # rather than returning an empty sessionSummaries list.
            if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return []
            raise HTTPException(
                status_code=502, detail=f"Failed to list conversations: {e}"
            ) from e
        except Exception as e:  # noqa: BLE001 - surfaced as a clean 502, not a 500
            raise HTTPException(
                status_code=502, detail=f"Failed to list conversations: {e}"
            ) from e

        summaries: list[ConversationSummary] = []
        for session in sessions_response.get("sessionSummaries", []):
            bare_session_id = session.get("sessionId")
            if not bare_session_id:
                continue
            try:
                events_response = client.list_events(
                    memoryId=memory_id,
                    actorId=actor_id,
                    sessionId=bare_session_id,
                    maxResults=100,
                )
            except Exception:  # noqa: BLE001 - one bad session shouldn't break the list
                continue
            events = events_response.get("events", [])
            if not events:
                # A session with no events yet (e.g. AgentCore Runtime
                # assigns a sessionId lazily before the first turn lands)
                # isn't a real conversation to show in the sidebar.
                continue
            created_at = session.get("createdAt")
            summaries.append(
                ConversationSummary(
                    session_id=f"{actor_id}{RUNTIME_SESSION_ID_SEPARATOR}{bare_session_id}",
                    created_at=created_at.isoformat() if created_at else None,
                    preview=_first_user_preview(events),
                    title=_latest_title(events),
                )
            )

        # AgentCore's ListSessions ordering isn't documented as
        # newest-first; sort explicitly so the sidebar is stable and useful.
        summaries.sort(key=lambda s: s.created_at or "", reverse=True)
        return summaries

    @app.get(
        "/api/conversations/{session_id}", response_model=ConversationDetail
    )
    def get_conversation(session_id: str, request: Request) -> ConversationDetail:
        if memory_id is None:
            raise HTTPException(
                status_code=404,
                detail="Conversation history is unavailable: server was started "
                "without --memory-id",
            )
        actor_id = actor_id_from_oidc_header(
            request.headers.get("x-amzn-oidc-data"), region, alb_arn
        )

        # session_id here is the full runtimeSessionId ("<actorId>___<id>"),
        # matching what /api/chat takes and what the frontend stores — but
        # ListEvents wants only the bare component after the separator (see
        # RUNTIME_SESSION_ID_SEPARATOR above).
        bare_session_id = session_id.split(RUNTIME_SESSION_ID_SEPARATOR, 1)[-1]

        try:
            events_response = client.list_events(
                memoryId=memory_id,
                actorId=actor_id,
                sessionId=bare_session_id,
                maxResults=100,
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                raise HTTPException(
                    status_code=404, detail="Conversation not found"
                ) from e
            raise HTTPException(
                status_code=502, detail=f"Failed to load conversation: {e}"
            ) from e
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                status_code=502, detail=f"Failed to load conversation: {e}"
            ) from e

        events = events_response.get("events", [])
        if not events:
            raise HTTPException(status_code=404, detail="Conversation not found")

        # list_events returns newest-first; reverse for chronological replay.
        turns: list[ConversationTurn] = []
        for event in reversed(events):
            turns.extend(_event_turns(event))

        return ConversationDetail(session_id=session_id, turns=turns)

    @app.put("/api/conversations/{session_id}/title")
    def set_conversation_title(session_id: str, body: SetTitleRequest, request: Request) -> dict:
        if memory_id is None:
            raise HTTPException(
                status_code=404,
                detail="Conversation history is unavailable: server was started "
                "without --memory-id",
            )
        actor_id = actor_id_from_oidc_header(
            request.headers.get("x-amzn-oidc-data"), region, alb_arn
        )

        title = " ".join(body.title.split())
        if not title:
            raise HTTPException(status_code=400, detail="title must not be empty")
        if len(title) > MAX_TITLE_CHARS:
            title = title[: MAX_TITLE_CHARS - 1].rstrip() + "…"

        bare_session_id = session_id.split(RUNTIME_SESSION_ID_SEPARATOR, 1)[-1]

        try:
            # A marker event, not a real chat turn: extractionMode=SKIP
            # keeps it out of long-term memory extraction (it's a UI label,
            # not something the traveler said), and _is_title_marker_event
            # keeps it out of transcripts and previews. The title itself
            # lives in event-level `metadata` (CreateEvent's dedicated
            # key-value field), not the conversational payload — so a
            # rename can never be mistaken for something the user typed in
            # chat. A minimal placeholder payload is still required: the
            # API's `payload` field for a conversational event can't be
            # empty. eventTimestamp is also required by the raw API
            # (bedrock_agentcore.memory.client.MemoryClient.create_event
            # defaults it internally, but this server calls the plain
            # boto3 client directly, which does not — confirmed live: an
            # omitted eventTimestamp raises ParamValidationError, a
            # botocore.exceptions.BotoCoreError subclass, not a ClientError).
            client.create_event(
                memoryId=memory_id,
                actorId=actor_id,
                sessionId=bare_session_id,
                eventTimestamp=datetime.now(timezone.utc),
                payload=[
                    {
                        "conversational": {
                            "content": {"text": f"Conversation renamed to “{title}”"},
                            "role": "USER",
                        }
                    }
                ],
                metadata={TITLE_METADATA_KEY: {"stringValue": title}},
                extractionMode="SKIP",
            )
        except (ClientError, BotoCoreError) as e:
            raise HTTPException(
                status_code=502, detail=f"Failed to set title: {e}"
            ) from e

        return {"session_id": session_id, "title": title}

    @app.delete(
        "/api/conversations/{session_id}", response_model=DeleteConversationResponse
    )
    def delete_conversation(session_id: str, request: Request) -> DeleteConversationResponse:
        if memory_id is None:
            raise HTTPException(
                status_code=404,
                detail="Conversation history is unavailable: server was started "
                "without --memory-id",
            )
        actor_id = actor_id_from_oidc_header(
            request.headers.get("x-amzn-oidc-data"), region, alb_arn
        )

        bare_session_id = session_id.split(RUNTIME_SESSION_ID_SEPARATOR, 1)[-1]

        try:
            event_ids = _list_all_event_ids(client, memory_id, actor_id, bare_session_id)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                raise HTTPException(
                    status_code=404, detail="Conversation not found"
                ) from e
            raise HTTPException(
                status_code=502, detail=f"Failed to delete conversation: {e}"
            ) from e

        if not event_ids:
            raise HTTPException(status_code=404, detail="Conversation not found")

        # Best-effort: one failed DeleteEvent call doesn't abort the rest,
        # matching every real-world implementation found for this pattern
        # (there's no batch/atomic delete to make this transactional). A
        # session with leftover events just doesn't fully disappear from
        # the sidebar — visible and re-triable rather than a silent
        # partial/corrupt state.
        deleted = 0
        failed = 0
        for event_id in event_ids:
            try:
                client.delete_event(
                    memoryId=memory_id,
                    actorId=actor_id,
                    sessionId=bare_session_id,
                    eventId=event_id,
                )
                deleted += 1
            except (ClientError, BotoCoreError):
                failed += 1

        return DeleteConversationResponse(
            session_id=session_id, deleted_events=deleted, failed_events=failed
        )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Web UI backend for the Travel Planning Agent "
        "hosted on Amazon Bedrock AgentCore Runtime.",
    )
    parser.add_argument(
        "--agent-runtime-arn",
        required=True,
        help="Full ARN of the deployed AgentCore Runtime agent "
        "(see the TravelAgentRuntimeStack CloudFormation outputs).",
    )
    parser.add_argument(
        "--alb-arn",
        required=True,
        help="ARN of the Application Load Balancer fronting this server "
        "with OIDC authentication (see TravelAgentWebStack's outputs). "
        "Required to verify the 'signer' field of every request's "
        "x-amzn-oidc-data header.",
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region the agent (and this server's own ALB) is deployed "
        "in (default: %(default)s).",
    )
    parser.add_argument(
        "--qualifier",
        default=None,
        help="Optional AgentCore Runtime endpoint qualifier (default: the "
        "runtime's default endpoint).",
    )
    parser.add_argument(
        "--memory-id",
        default=None,
        help="AgentCore Memory resource ID (see the TravelAgentMemoryStack "
        "CloudFormation outputs). Enables the conversation-history sidebar "
        "(GET /api/conversations); omit to run without it. Requires this "
        "process's IAM role to have bedrock-agentcore:ListSessions, "
        "bedrock-agentcore:ListEvents, bedrock-agentcore:CreateEvent (for "
        "renaming), and bedrock-agentcore:DeleteEvent (for deleting) on "
        "this Memory resource.",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind the server to (default: %(default)s — the ALB "
        "reaches this container over the VPC, not localhost, so this "
        "defaults to all interfaces unlike the pre-hosting local-only "
        "version of this file).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8420,
        help="Port to bind the server to (default: %(default)s).",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    app = create_app(
        agent_runtime_arn=args.agent_runtime_arn,
        region=args.region,
        alb_arn=args.alb_arn,
        qualifier=args.qualifier,
        memory_id=args.memory_id,
    )

    print(f"Travel Planning Agent — Web UI running at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
