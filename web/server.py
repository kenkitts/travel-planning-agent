#!/usr/bin/env python3
"""Local web UI backend for the Travel Planning Agent.

Runs entirely on localhost — no new AWS infrastructure. Serves a small
static chat UI and a single JSON API endpoint that invokes the deployed
AgentCore Runtime agent using the same `agent_client` module `cli/chat.py`
uses (boto3's `bedrock-agentcore` client, standard AWS credential
resolution). Not designed for multi-user or public exposure: there is
no login, and the agent's `actor_id` is fixed per server process via
`--actor-id`.

Usage:
    python server.py --agent-runtime-arn <arn> [--actor-id <id>] \\
        [--region <region>] [--qualifier <qualifier>] [--port <port>]

Then open http://localhost:<port> in a browser.

Auth: standard AWS credential resolution (env vars, profile, IAM role).
The invoking principal must have `bedrock-agentcore:InvokeAgentRuntime`
permission on the target agent runtime (IAM-only auth, per DESIGN.md
decision #15 — there is no separate API key or bearer token). Because
this server holds real AWS credentials and proxies them into agent
calls, it must not be exposed beyond localhost.
"""
import argparse
import sys
from pathlib import Path
from typing import Optional

import boto3
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Reuse the same session-ID + InvokeAgentRuntime logic as cli/chat.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))
from agent_client import invoke_agent  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"


class ChatRequest(BaseModel):
    """Request body for POST /api/chat."""

    prompt: str
    session_id: str


class ChatResponse(BaseModel):
    """Response body for POST /api/chat."""

    response: str


def create_app(
    agent_runtime_arn: str,
    region: str,
    actor_id: str,
    qualifier: Optional[str] = None,
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
        # memory stays scoped consistently across page reloads.
        return {"actor_id": actor_id}

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
    )

    print(f"Travel Planning Agent — Web UI running at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
