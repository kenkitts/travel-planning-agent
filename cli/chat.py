#!/usr/bin/env python3
"""Local CLI REPL client for the Travel Planning Agent.

Invokes the deployed AgentCore Runtime agent over HTTPS with an Okta-issued
JWT bearer token (DESIGN.md decisions #26-35), maintaining a single runtime
session ID across turns so the agent's short-term memory and conversation
context carry over within one CLI session.

Session-ID construction, Okta token acquisition, and the actual Runtime
invocation call live in `agent_client.py`, shared with `web/server.py`. The
Runtime always streams its response (agent/agent.py's entrypoint is a
streaming async generator); this CLI has no diagnostic UI, so
`_consume_stream()` drains the event stream internally and prints only the
final reply, preserving the CLI's original one-line "agent> <response>" UX.

Usage:
    python chat.py --agent-runtime-arn <arn> [--region <region>]

Auth: Okta login via the okta-claude-code-token-helper script (run once
manually the first time — see that script's README — so a browser login
can complete before this CLI's own non-interactive re-invocations of it).
There is no --actor-id flag: long-term memory is scoped server-side to
whichever Okta identity's token is presented, derived from the JWT's `sub`
claim (DESIGN.md decision #31) — not a client-supplied value.
"""
import argparse
import sys
from typing import Optional

from agent_client import build_runtime_session_id, get_okta_access_token, stream_agent_events

# Session IDs are still formatted as "<placeholder>___<uuid>" for
# compatibility with the existing convention (see
# agent_client.build_runtime_session_id's docstring) — this placeholder is
# purely cosmetic now; the Runtime derives the real actor_id from the JWT.
_SESSION_ID_PLACEHOLDER = "cli-user"


def _consume_stream(access_token, agent_runtime_arn, region, runtime_session_id, user_input, qualifier):
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
        access_token, agent_runtime_arn, region, runtime_session_id, user_input, qualifier
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
    qualifier: Optional[str] = None,
) -> None:
    """Run the interactive chat loop until the user exits."""
    runtime_session_id = build_runtime_session_id(_SESSION_ID_PLACEHOLDER)

    print("Travel Planning Agent — CLI chat")
    print(f"Session: {runtime_session_id}")
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
            access_token = get_okta_access_token()
            response_text = _consume_stream(
                access_token, agent_runtime_arn, region, runtime_session_id, user_input, qualifier
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
        qualifier=args.qualifier,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
