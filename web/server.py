#!/usr/bin/env python3
"""Hosted web UI backend for the Travel Planning Agent.

Runs on ECS Fargate behind a plain (non-OIDC) internet-facing Application
Load Balancer (`TravelAgentWebStack`, see DESIGN.md's Phase 1 auth
rearchitecture decision, superseding decisions #37/#38's ALB-OIDC
framing; PLAN.md Phase 10/11). This process now runs the entire OAuth 2.0
Authorization Code + PKCE flow against Okta itself (see `auth.py`) —
there is no `x-amzn-oidc-data` header to trust; the session is instead a
single, KMS-envelope-encrypted cookie this server issues, reads, and
transparently refreshes.

Serves a small chat UI and a streaming API that invokes the deployed
AgentCore Runtime agent via a JWT bearer token, RFC 8693-exchanged from
this server's own verified Okta session (`agent_client.py`/`auth.py`,
this same directory — DESIGN.md's Phase 2 auth rearchitecture decision;
this reverts decision #37's original IAM/SigV4 choice), plus
read-only endpoints that list and replay past conversations directly from
AgentCore Memory (no separate local storage — Memory is the source of
truth).

Every logged-in person gets their own `actor_id` (and therefore their own
conversation history / long-term memory), derived per-request from this
server's own verified Okta session — not from a single server-wide
identity like the old local-only version of this file, and not from an
ALB-forwarded header like the version of this file that existed between
those two.

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
        --oidc-issuer <issuer> \\
        --oidc-authorization-endpoint <url> --oidc-token-endpoint <url> \\
        --oidc-client-id <id> --oidc-client-secret-arn <secrets-manager-arn> \\
        --oidc-redirect-uri <url> --session-cookie-kms-key-id <key-id> \\
        [--region <region>] [--qualifier <qualifier>] [--port <port>]

Auth: see auth.py for the full OIDC flow, session-cookie encryption,
refresh logic, and the Phase 2 RFC 8693 Runtime-token exchange. AWS
credentials for Memory access and the session/Runtime-token cookies'
KMS/Secrets Manager calls come from this process's own IAM role (the ECS
task role in the hosted deployment; your local credentials if run
outside ECS for testing) — but Runtime invocation itself (agent_client.py)
no longer uses that IAM role at all; it presents the exchanged JWT
bearer token instead (DESIGN.md's Phase 2 decision).
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

import re

from agent_client import build_runtime_session_id, stream_agent_events
from auth import (
    AuthContext,
    AuthError,
    OktaConfig,
    RuntimeOidcConfig,
    RuntimeTokenCookieCodec,
    SessionCookieCodec,
    apply_refreshed_cookie_if_needed,
    apply_runtime_token_cookie_if_needed,
    clear_pending_login_cookie,
    clear_session_cookie,
    decode_pending_login,
    exchange_code_for_tokens,
    get_or_exchange_runtime_token,
    get_or_refresh_session,
    is_browser_navigation,
    redirect_to_login,
    set_session_cookie,
)


STATIC_DIR = Path(__file__).resolve().parent / "static"

# Longest prompt fragment shown as a conversation's preview in the sidebar.
PREVIEW_MAX_CHARS = 80

# Matches agent_client.py's SESSION_ID_SEPARATOR: a runtimeSessionId is
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


class _RedirectToLogin(Exception):
    """Internal signal: this request needs a real page-navigation redirect
    to Okta, not a 401 — raised for top-level page loads only (see
    is_browser_navigation()), caught by create_app()'s exception handler,
    which builds the actual redirect + pending-login cookie."""

    def __init__(self, return_to: str) -> None:
        super().__init__(return_to)
        self.return_to = return_to


def create_app(
    agent_runtime_arn: str,
    region: str,
    okta_config: OktaConfig,
    session_codec: SessionCookieCodec,
    runtime_oidc_config: RuntimeOidcConfig,
    runtime_token_codec: RuntimeTokenCookieCodec,
    qualifier: Optional[str] = None,
    memory_id: Optional[str] = None,
) -> FastAPI:
    """Build the FastAPI app, wiring in the boto3 clients and CLI args.

    `okta_config`/`session_codec` back every protected route's identity
    resolution — see auth.py for the full OIDC + session-cookie design.
    `runtime_oidc_config`/`runtime_token_codec` back /api/chat's own,
    separate Runtime-token exchange + cookie (Phase 2 — see auth.py's
    module docstring and DESIGN.md's Phase 2 section).
    """
    # Memory access (ListSessions/ListEvents/CreateEvent/DeleteEvent) and
    # Runtime invocation both use this process's own IAM role — the ECS
    # task role in the hosted deployment (scoped to the specific Memory
    # and Runtime ARNs; see cdk/stacks/web_stack.py), or local credentials
    # if run outside ECS.
    client = boto3.client("bedrock-agentcore", region_name=region)
    app = FastAPI(title="Travel Planning Agent — Web UI")

    def _resolve_auth(request: Request) -> AuthContext:
        """Resolve the caller's session or raise HTTPException.

        Every protected route calls this first. On success, returns an
        AuthContext (sub + tokens + whether a refresh just happened) —
        callers that mutate state successfully should call
        _finish_auth(response, context) before returning so a refreshed
        cookie actually reaches the browser.

        On failure, the response differs by request type (see
        is_browser_navigation()): a top-level page load gets redirected
        straight to Okta (a real browser navigation can follow this
        transparently); a fetch()/XHR call to /api/* gets a clean 401 so
        the frontend's own res.status check can react to it directly,
        instead of the ALB-era CORS-blocked-redirect workaround this
        replaces (DESIGN.md's Phase 1 auth rearchitecture decision).
        """
        try:
            return get_or_refresh_session(request, session_codec, okta_config)
        except AuthError as e:
            if is_browser_navigation(request):
                # A full page load can be sent straight into the OIDC
                # dance — return_to preserves where the user was headed.
                raise _RedirectToLogin(str(request.url.path)) from e
            raise HTTPException(status_code=401, detail=f"Not authenticated: {e}") from e

    def _actor_id(request: Request, response) -> str:
        """Convenience wrapper: resolve auth, apply any refreshed cookie,
        and return the sanitized actor_id — the common case for every
        route below that doesn't need the raw AuthContext itself."""
        context = _resolve_auth(request)
        apply_refreshed_cookie_if_needed(response, context, session_codec)
        return _sanitize_actor_id(context.sub)

    @app.exception_handler(_RedirectToLogin)
    def _handle_redirect_to_login(request: Request, exc: "_RedirectToLogin") -> RedirectResponse:
        return redirect_to_login(okta_config, exc.return_to)

    @app.get("/oauth2/callback")
    def oauth2_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
        """Completes the OIDC dance: exchanges the code, sets the session
        cookie, and redirects the browser to wherever it originally asked
        to go (see redirect_to_login()'s return_to)."""
        pending_cookie = request.cookies.get("travel_agent_pending_login")
        if not pending_cookie:
            raise HTTPException(status_code=400, detail="No pending login found for this callback")

        pending = decode_pending_login(pending_cookie)
        if error:
            raise HTTPException(status_code=400, detail=f"Okta returned an error: {error}")
        if not code or not state:
            raise HTTPException(status_code=400, detail="Callback missing 'code' or 'state'")
        if state != pending.get("state"):
            raise HTTPException(status_code=400, detail="State mismatch — possible CSRF")

        try:
            tokens = exchange_code_for_tokens(okta_config, code, pending["code_verifier"])
        except AuthError as e:
            raise HTTPException(status_code=400, detail=f"Login failed: {e}") from e

        return_to = pending.get("return_to") or "/"
        redirect = RedirectResponse(url=return_to, status_code=302)
        clear_pending_login_cookie(redirect)
        set_session_cookie(redirect, session_codec.encode(tokens))
        return redirect

    @app.get("/")
    def index(request: Request) -> FileResponse:
        # Must resolve auth here — unlike under the old ALB model, this
        # container no longer sits behind a network-edge gate that checks
        # every request before it arrives; this route (like every other
        # protected one) has to check for itself. A missing/expired
        # session sends a real page load straight into the OIDC redirect
        # (see _resolve_auth()/is_browser_navigation()), matching decision
        # #56's intent — this bug (the route originally had no auth check
        # at all) was caught by a live curl check immediately after the
        # first Phase 1 deploy, not by the automated test suite.
        context = _resolve_auth(request)
        file_response = FileResponse(STATIC_DIR / "index.html")
        apply_refreshed_cookie_if_needed(file_response, context, session_codec)
        return file_response

    @app.get("/favicon.ico")
    def favicon() -> FileResponse:
        # Browsers request this path directly (independent of index.html's
        # own <link rel="icon">) before a page's markup is even parsed.
        return FileResponse(STATIC_DIR / "favicon.ico")

    @app.get("/api/config")
    def config() -> dict:
        # Tells the frontend whether to show the conversation-history
        # sidebar at all. No actor_id here — unlike the old local-only
        # version, actor_id now varies per-request (derived from each
        # request's own verified session cookie), not fixed at server
        # startup.
        return {"history_enabled": memory_id is not None}

    @app.get("/api/whoami")
    def whoami(request: Request, response: Response) -> dict:
        """Debug endpoint: show the actor_id derived from this request's own
        verified session, and the raw `sub` claim it was sanitized from.

        Not linked from the UI; intended for manually confirming which
        actor_id a given login session maps to (e.g. via curl or a
        browser visit while logged in).
        """
        context = _resolve_auth(request)
        apply_refreshed_cookie_if_needed(response, context, session_codec)
        return {"sub": context.sub, "actor_id": _sanitize_actor_id(context.sub)}

    @app.post("/api/chat")
    def chat(request: Request, response: Response, body: ChatRequest) -> StreamingResponse:
        """Stream one turn's labeled events to the browser as SSE.

        Forwards agent_client.stream_agent_events()'s parsed event dicts
        (already validated to have a "type" key) back out as SSE frames —
        the browser's EventSource-equivalent fetch/reader consumes these
        directly; see static/app.js. actor_id is derived from this
        request's own verified session cookie (not cached from startup),
        so a long-running container correctly serves multiple distinct
        users without mixing up whose Memory a turn should read/write.

        Phase 2: this is the only route that resolves a Runtime-audienced
        JWT (via auth.get_or_exchange_runtime_token()) — /api/whoami and
        the conversation-history endpoints talk to AgentCore Memory
        directly via this process's own IAM role and have no reason to
        ever need it (DESIGN.md's Phase 2 decision). The exchange is
        resolved *before* the streaming response starts, so a genuine
        exchange failure (Okta's token endpoint down, misconfigured
        scope/audience) can still surface as a clean 502 rather than only
        being expressible as an in-band SSE error event after a 200 has
        already gone out.
        """
        context = _resolve_auth(request)
        apply_refreshed_cookie_if_needed(response, context, session_codec)
        actor_id = _sanitize_actor_id(context.sub)

        try:
            runtime_token_context = get_or_exchange_runtime_token(
                request, runtime_token_codec, runtime_oidc_config, context.tokens.access_token
            )
        except AuthError as e:
            raise HTTPException(
                status_code=502, detail=f"Failed to obtain a Runtime access token: {e}"
            ) from e
        apply_runtime_token_cookie_if_needed(response, runtime_token_context, runtime_token_codec)
        bearer_token = runtime_token_context.access_token

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
                    agent_runtime_arn,
                    region,
                    body.session_id,
                    prompt,
                    actor_id,
                    bearer_token,
                    qualifier,
                ):
                    yield f"data: {json.dumps(event)}\n\n"
            except RuntimeError as e:
                # A transport-level failure (an HTTP/network error,
                # malformed SSE from the Runtime) discovered mid-stream —
                # the response has already started with a 200, so this
                # can't become an HTTPException; forward it as one more
                # in-band error event instead, matching the shape of an
                # agent-raised error event.
                yield f"data: {json.dumps({'type': 'error', 'data': {'note': str(e)}})}\n\n"

        # response's headers (including any Set-Cookie from a just-
        # performed session and/or Runtime-token refresh, see above) are
        # carried onto this StreamingResponse — FastAPI merges a
        # dependency-injected Response's headers onto whatever the route
        # actually returns.
        streaming_response = StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
        for key, value in response.headers.items():
            streaming_response.headers.append(key, value)
        return streaming_response


    @app.get("/api/conversations", response_model=list[ConversationSummary])
    def list_conversations(request: Request, response: Response) -> list[ConversationSummary]:
        if memory_id is None:
            raise HTTPException(
                status_code=404,
                detail="Conversation history is unavailable: server was started "
                "without --memory-id",
            )
        actor_id = _actor_id(request, response)

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
    def get_conversation(session_id: str, request: Request, response: Response) -> ConversationDetail:
        if memory_id is None:
            raise HTTPException(
                status_code=404,
                detail="Conversation history is unavailable: server was started "
                "without --memory-id",
            )
        actor_id = _actor_id(request, response)

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
    def set_conversation_title(session_id: str, body: SetTitleRequest, request: Request, response: Response) -> dict:
        if memory_id is None:
            raise HTTPException(
                status_code=404,
                detail="Conversation history is unavailable: server was started "
                "without --memory-id",
            )
        actor_id = _actor_id(request, response)

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
    def delete_conversation(session_id: str, request: Request, response: Response) -> DeleteConversationResponse:
        if memory_id is None:
            raise HTTPException(
                status_code=404,
                detail="Conversation history is unavailable: server was started "
                "without --memory-id",
            )
        actor_id = _actor_id(request, response)

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
        "--oidc-issuer",
        required=True,
        help="Okta authorization server issuer URL for this app's dedicated "
        "Okta application (see DESIGN.md's Phase 1 auth rearchitecture "
        "decision — a separate app from any other Okta app used elsewhere "
        "in this project).",
    )
    parser.add_argument(
        "--oidc-authorization-endpoint",
        required=True,
        help="Okta's /authorize endpoint for this app.",
    )
    parser.add_argument(
        "--oidc-token-endpoint",
        required=True,
        help="Okta's /token endpoint for this app.",
    )
    parser.add_argument(
        "--oidc-client-id",
        required=True,
        help="This app's Okta client ID.",
    )
    parser.add_argument(
        "--oidc-client-secret-arn",
        required=True,
        help="Secrets Manager ARN of this app's Okta client secret (see "
        "cdk/stacks/web_stack.py's OidcClientSecret resource) — the raw "
        "secret is never passed as a plain CLI argument or environment "
        "variable.",
    )
    parser.add_argument(
        "--oidc-redirect-uri",
        required=True,
        help="The exact redirect_uri registered with Okta for this app "
        "(must match https://<this ALB's DNS name>/oauth2/callback).",
    )
    parser.add_argument(
        "--session-cookie-kms-key-id",
        required=True,
        help="KMS key ID (or ARN) used to envelope-encrypt the session "
        "cookie (see web/auth.py's SessionCookieCodec and "
        "cdk/stacks/web_stack.py's SessionCookieKey resource). Also used "
        "to encrypt the separate Runtime-token cookie (see "
        "--runtime-oidc-* below) — same KMS key, same encrypt/decrypt "
        "pattern, two independent cookies (DESIGN.md's Phase 2 decision).",
    )
    parser.add_argument(
        "--runtime-oidc-issuer",
        required=True,
        help="Issuer URL of the Token-Exchange app's Okta authorization "
        "server (documentation/debugging context only — not sent in the "
        "exchange request itself; see RuntimeOidcConfig in web/auth.py).",
    )
    parser.add_argument(
        "--runtime-oidc-token-endpoint",
        required=True,
        help="Okta's /token endpoint for the dedicated 'API Services' "
        "app used for RFC 8693 Token Exchange, exchanging a user's Okta "
        "access token for a Runtime-audienced JWT (DESIGN.md's Phase 2 "
        "auth rearchitecture decision) — a different Okta authorization "
        "server than --oidc-issuer/--oidc-token-endpoint above.",
    )
    parser.add_argument(
        "--runtime-oidc-client-id",
        required=True,
        help="Client ID of the dedicated Token-Exchange 'API Services' "
        "Okta app (Advanced grant type: Token Exchange).",
    )
    parser.add_argument(
        "--runtime-oidc-client-secret-arn",
        required=True,
        help="Secrets Manager ARN of the Token-Exchange app's client "
        "secret (see cdk/stacks/web_stack.py's RuntimeOidcClientSecret "
        "resource) — never passed as a plain CLI argument or environment "
        "variable.",
    )
    parser.add_argument(
        "--runtime-oidc-audience",
        required=True,
        help="The RFC 8693 'audience' value requested during token "
        "exchange — must match this Runtime's own JWT authorizer "
        "allowed_audience (see cdk/stacks/runtime_stack.py).",
    )
    parser.add_argument(
        "--runtime-oidc-scope",
        required=True,
        help="The RFC 8693 'scope' value requested during token exchange "
        "(a custom Okta scope, e.g. 'runtime:invoke') — must match this "
        "Runtime's own JWT authorizer allowed_scopes.",
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region the agent (and this server's own KMS key/Secrets "
        "Manager secret) is deployed in (default: %(default)s).",
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


def _fetch_secret_value(secret_arn: str, region: str) -> str:
    """Resolve a Secrets Manager secret's plaintext string value.

    Called once at process startup (not per-request) — the Okta client
    secret doesn't change without a redeploy, so there's no need to
    re-fetch it on every token exchange/refresh call.
    """
    client = boto3.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=secret_arn)
    return response["SecretString"]


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    okta_client_secret = _fetch_secret_value(args.oidc_client_secret_arn, args.region)
    okta_config = OktaConfig(
        issuer=args.oidc_issuer,
        authorization_endpoint=args.oidc_authorization_endpoint,
        token_endpoint=args.oidc_token_endpoint,
        client_id=args.oidc_client_id,
        client_secret=okta_client_secret,
        redirect_uri=args.oidc_redirect_uri,
    )
    session_codec = SessionCookieCodec(kms_key_id=args.session_cookie_kms_key_id)

    runtime_oidc_client_secret = _fetch_secret_value(
        args.runtime_oidc_client_secret_arn, args.region
    )
    runtime_oidc_config = RuntimeOidcConfig(
        issuer=args.runtime_oidc_issuer,
        token_endpoint=args.runtime_oidc_token_endpoint,
        client_id=args.runtime_oidc_client_id,
        client_secret=runtime_oidc_client_secret,
        audience=args.runtime_oidc_audience,
        scope=args.runtime_oidc_scope,
    )
    runtime_token_codec = RuntimeTokenCookieCodec(kms_key_id=args.session_cookie_kms_key_id)

    app = create_app(
        agent_runtime_arn=args.agent_runtime_arn,
        region=args.region,
        okta_config=okta_config,
        session_codec=session_codec,
        runtime_oidc_config=runtime_oidc_config,
        runtime_token_codec=runtime_token_codec,
        qualifier=args.qualifier,
        memory_id=args.memory_id,
    )

    print(f"Travel Planning Agent — Web UI running at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
