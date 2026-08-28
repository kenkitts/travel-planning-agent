#!/usr/bin/env python3
"""Local CLI REPL client for the Travel Planning Agent.

Invokes the deployed AgentCore Runtime agent over IAM/SigV4
(`boto3.invoke_agent_runtime`, DESIGN.md decision #37), maintaining a
single runtime session ID across turns so the agent's short-term memory
and conversation context carry over within one CLI session.

Session-ID construction and the actual Runtime invocation call live in
`agent_client.py`, shared with `web/server.py`. The Runtime always streams
its response (agent/agent.py's entrypoint is a streaming async
generator); this CLI has no diagnostic UI, so `_consume_stream()` drains
the event stream internally and prints only the final reply, preserving
the CLI's original one-line "agent> <response>" UX.

Usage:
    python chat.py --agent-runtime-arn <arn> --actor-id <id> [--region <region>]

Auth: your local AWS credentials (the default boto3 credential chain —
e.g. `aws sso login` or an equivalent credential process), same as every
other AWS CLI/SDK call in this project. `--actor-id` scopes long-term
memory to whichever value you supply — pick something stable per person
(e.g. your own name or username) if multiple people share this CLI.
"""
import argparse
import sys
from typing import Optional

from agent_client import build_runtime_session_id, stream_agent_events


def _consume_stream(agent_runtime_arn, region, runtime_session_id, user_input, actor_id, qualifier):
    """Drain one turn's event stream and return the final reply text.

    Only the web UI needs live/diagnostic streaming (reasoning, tool_use,
    tool_result events); the CLI's existing UX is a single "agent> <reply>"
    line printed once the full response is ready, so this collects "text"
    deltas as they arrive and returns the joined result — or, if the stream
    ends in an {"type": "error"} event (e.g. the MaxTokensReachedException
    cutoff case), whatever partial text streamed plus the note.
    """
    text_parts: list[str] = []
    for event in stream_agent_events(
        agent_runtime_arn, region, runtime_session_id, user_input, actor_id, qualifier
    ):
        event_type = event.get("type")
        if event_type == "text":
            text_parts.append(event["data"])
        elif event_type == "done":
            # "done" carries the full final text (redundant with the joined
            # deltas in the normal case) — prefer it since it's guaranteed
            # complete even if a delta was somehow missed.
            final_text = event.get("data") or "".join(text_parts)
            return final_text
        elif event_type == "error":
            data = event.get("data")
            if isinstance(data, dict):
                partial = data.get("partial_text") or "".join(text_parts)
                note = data.get("note", "")
                return f"{partial}\n\n*({note})*" if note else partial
            return data or "".join(text_parts) or "Sorry, something went wrong."
    return "".join(text_parts)


def run_repl(
    agent_runtime_arn: str,
    region: str,
    actor_id: str,
    qualifier: Optional[str] = None,
) -> None:
    """Run the interactive chat loop until the user exits."""
    runtime_session_id = build_runtime_session_id()

    print("Travel Planning Agent — CLI chat")
    print(f"Session: {runtime_session_id}")
    print(f"Actor: {actor_id}")
    print("Type your message and press Enter. Type 'exit' or 'quit' to leave.\n")

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            return

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("Goodbye!")
            return

        try:
            response_text = _consume_stream(
                agent_runtime_arn, region, runtime_session_id, user_input, actor_id, qualifier
            )
        except RuntimeError as e:
            print(f"[error] {e}\n")
            continue

        print(f"agent> {response_text}\n")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Interactive CLI chat client for the Travel Planning Agent "
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
        required=True,
        help="Identifier scoping this session's long-term AgentCore Memory "
        "(preferences, cross-session recall). Pick something stable per "
        "person if multiple people share this CLI.",
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
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    run_repl(
        agent_runtime_arn=args.agent_runtime_arn,
        region=args.region,
        actor_id=args.actor_id,
        qualifier=args.qualifier,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
