#!/usr/bin/env python3
"""Local web UI backend for the Travel Planning Agent.

Runs entirely on localhost — no new AWS infrastructure. Serves a small
static chat UI and a JSON API that invokes the deployed AgentCore Runtime
agent using the same `agent_client` module `cli/chat.py` uses (boto3's
`bedrock-agentcore` client, standard AWS credential resolution), plus
read-only endpoints that list and replay past conversations directly from
AgentCore Memory (no separate local storage — Memory is the source of
truth). Not designed for multi-user or public exposure: there is no login,
and the agent's `actor_id` is fixed per server process via `--actor-id`.

Usage:
    python server.py --agent-runtime-arn <arn> --memory-id <id> \\
        [--actor-id <id>] [--region <region>] [--qualifier <qualifier>] \\
        [--port <port>]

Then open http://localhost:<port> in a browser.

Auth: standard AWS credential resolution (env vars, profile, IAM role).
The invoking principal must have `bedrock-agentcore:InvokeAgentRuntime`
permission on the target agent runtime, and `bedrock-agentcore:ListSessions`
+ `bedrock-agentcore:ListEvents` on the Memory resource for the
conversation-history endpoints (IAM-only auth, per DESIGN.md decision #15 —
there is no separate API key or bearer token). Because this server holds
real AWS credentials and proxies them into agent calls, it must not be
exposed beyond localhost.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import boto3
import uvicorn
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Reuse the same session-ID + InvokeAgentRuntime logic as cli/chat.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))
from agent_client import invoke_agent  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Longest prompt fragment shown as a conversation's preview in the sidebar.
PREVIEW_MAX_CHARS = 80

# Matches cli/agent_client.py's SESSION_ID_SEPARATOR: a runtimeSessionId is
# "<actorId>___<sessionId>", but AgentCore Memory's ListSessions/ListEvents
# APIs are already actor-scoped and only ever return/accept the bare
# <sessionId> component (confirmed live: ListSessions returned
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


class ChatResponse(BaseModel):
    """Response body for POST /api/chat."""

    response: str


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


def create_app(
    agent_runtime_arn: str,
    region: str,
    actor_id: str,
    qualifier: Optional[str] = None,
    memory_id: Optional[str] = None,
) -> FastAPI:
    """Build the FastAPI app, wiring in the boto3 client and CLI args."""
    client = boto3.client("bedrock-agentcore", region_name=region)
    app = FastAPI(title="Travel Planning Agent — Web UI")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/config")
    def config() -> dict:
        # Lets the frontend seed its localStorage session ID with the
        # actor_id this server process was started with, so long-term
        # memory stays scoped consistently across page reloads. Tells the
        # frontend whether to show the conversation-history sidebar at all.
        return {"actor_id": actor_id, "history_enabled": memory_id is not None}

    @app.post("/api/chat", response_model=ChatResponse)
    def chat(request: ChatRequest) -> ChatResponse:
        prompt = request.prompt.strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt must not be empty")
        # session_id (the AgentCore runtimeSessionId) is generated and
        # persisted client-side (localStorage) so a page reload continues
        # the same conversation; the server does not track sessions itself.
        if len(request.session_id) < 33:
            raise HTTPException(
                status_code=400,
                detail="session_id must be at least 33 characters "
                "(AgentCore Runtime requirement)",
            )

        try:
            response_text = invoke_agent(
                client, agent_runtime_arn, request.session_id, prompt, qualifier
            )
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e

        return ChatResponse(response=response_text)

    @app.get("/api/conversations", response_model=list[ConversationSummary])
    def list_conversations() -> list[ConversationSummary]:
        if memory_id is None:
            raise HTTPException(
                status_code=404,
                detail="Conversation history is unavailable: server was started "
                "without --memory-id",
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
    def get_conversation(session_id: str) -> ConversationDetail:
        if memory_id is None:
            raise HTTPException(
                status_code=404,
                detail="Conversation history is unavailable: server was started "
                "without --memory-id",
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
    def set_conversation_title(session_id: str, request: SetTitleRequest) -> dict:
        if memory_id is None:
            raise HTTPException(
                status_code=404,
                detail="Conversation history is unavailable: server was started "
                "without --memory-id",
            )

        title = " ".join(request.title.split())
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

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local web UI backend for the Travel Planning Agent "
        "hosted on Amazon Bedrock AgentCore Runtime.",
    )
    parser.add_argument(
        "--agent-runtime-arn",
        required=True,
        help="Full ARN of the deployed AgentCore Runtime agent "
        "(see the TravelAgentRuntimeStack CloudFormation outputs).",
    )
    parser.add_argument(
        "--actor-id",
        default="web-user",
        help="Identifier for the traveler using this server, used to scope "
        "long-term memory (default: %(default)s). Sent by the browser as "
        "part of its persisted session ID, not passed here directly.",
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region the agent is deployed in (default: %(default)s).",
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
        "(GET /api/conversations); omit to run without it. Requires the "
        "caller's AWS credentials to have bedrock-agentcore:ListSessions "
        "and bedrock-agentcore:ListEvents on this Memory resource.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind the local server to (default: %(default)s, "
        "i.e. localhost only).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8420,
        help="Port to bind the local server to (default: %(default)s).",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    app = create_app(
        agent_runtime_arn=args.agent_runtime_arn,
        region=args.region,
        actor_id=args.actor_id,
        qualifier=args.qualifier,
        memory_id=args.memory_id,
    )

    print(f"Travel Planning Agent — Web UI running at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
