"""Unit tests for cli/agent_client.py.

Covers build_runtime_session_id() and stream_agent_events(). No live
network access or real AWS credentials — the boto3 client and its
invoke_agent_runtime response are mocked, with iter_lines() faked as a
plain Python iterator over pre-built SSE-frame byte strings.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli"))

import agent_client  # noqa: E402


def _sse_lines(*events_json: str) -> list[bytes]:
    """Build the byte lines iter_lines() would yield for the given SSE frames.

    Each event_json is one already-JSON-encoded event; wraps it in the
    "data: <json>" line form and appends a blank line, matching real SSE
    framing ("data: <json>\\n\\n") as produced by
    BedrockAgentCoreApp._convert_to_sse.
    """
    lines: list[bytes] = []
    for event_json in events_json:
        lines.append(f"data: {event_json}".encode("utf-8"))
        lines.append(b"")  # blank line between frames
    return lines


def _make_streaming_response(lines: list[bytes]):
    """Build the invoke_agent_runtime() return value for a streaming reply."""
    mock_body = MagicMock()
    mock_body.iter_lines.return_value = iter(lines)
    return {"contentType": "text/event-stream", "response": mock_body}


class BuildRuntimeSessionIdTests(unittest.TestCase):
    def test_includes_actor_id_and_separator(self):
        session_id = agent_client.build_runtime_session_id("ken")

        self.assertTrue(session_id.startswith("ken___"))

    def test_meets_minimum_length(self):
        session_id = agent_client.build_runtime_session_id("a")

        self.assertGreaterEqual(len(session_id), agent_client.MIN_SESSION_ID_LENGTH)

    def test_is_unique_per_call(self):
        first = agent_client.build_runtime_session_id("ken")
        second = agent_client.build_runtime_session_id("ken")

        self.assertNotEqual(first, second)


class StreamAgentEventsTests(unittest.TestCase):
    def test_yields_parsed_events_in_order(self):
        client = MagicMock()
        client.invoke_agent_runtime.return_value = _make_streaming_response(
            _sse_lines(
                '{"type": "text", "data": "Here\'s "}',
                '{"type": "text", "data": "your itinerary."}',
                '{"type": "done", "data": "Here\'s your itinerary."}',
            )
        )

        events = list(
            agent_client.stream_agent_events(
                client, "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
                "session___" + "a" * 30, "Plan a trip",
            )
        )

        self.assertEqual(
            events,
            [
                {"type": "text", "data": "Here's "},
                {"type": "text", "data": "your itinerary."},
                {"type": "done", "data": "Here's your itinerary."},
            ],
        )

    def test_passes_prompt_and_session_id_to_invoke_agent_runtime(self):
        client = MagicMock()
        client.invoke_agent_runtime.return_value = _make_streaming_response(
            _sse_lines('{"type": "done", "data": "ok"}')
        )

        list(
            agent_client.stream_agent_events(
                client, "arn:...", "session-id-here", "hello", None
            )
        )

        call_kwargs = client.invoke_agent_runtime.call_args.kwargs
        self.assertEqual(call_kwargs["agentRuntimeArn"], "arn:...")
        self.assertEqual(call_kwargs["runtimeSessionId"], "session-id-here")
        self.assertEqual(call_kwargs["accept"], "text/event-stream")
        self.assertNotIn("qualifier", call_kwargs)

    def test_includes_qualifier_when_provided(self):
        client = MagicMock()
        client.invoke_agent_runtime.return_value = _make_streaming_response(
            _sse_lines('{"type": "done", "data": "ok"}')
        )

        list(
            agent_client.stream_agent_events(
                client, "arn:...", "session-id-here", "hello", "PROD"
            )
        )

        call_kwargs = client.invoke_agent_runtime.call_args.kwargs
        self.assertEqual(call_kwargs["qualifier"], "PROD")

    def test_skips_blank_lines(self):
        client = MagicMock()
        mock_body = MagicMock()
        mock_body.iter_lines.return_value = iter(
            [b"", b'data: {"type": "done", "data": "ok"}', b""]
        )
        client.invoke_agent_runtime.return_value = {
            "contentType": "text/event-stream",
            "response": mock_body,
        }

        events = list(
            agent_client.stream_agent_events(client, "arn:...", "session-id", "hi", None)
        )

        self.assertEqual(events, [{"type": "done", "data": "ok"}])

    def test_ignores_non_data_lines(self):
        client = MagicMock()
        mock_body = MagicMock()
        mock_body.iter_lines.return_value = iter(
            [b": keepalive", b'data: {"type": "done", "data": "ok"}']
        )
        client.invoke_agent_runtime.return_value = {
            "contentType": "text/event-stream",
            "response": mock_body,
        }

        events = list(
            agent_client.stream_agent_events(client, "arn:...", "session-id", "hi", None)
        )

        self.assertEqual(events, [{"type": "done", "data": "ok"}])

    def test_raises_on_client_error(self):
        client = MagicMock()
        client.invoke_agent_runtime.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "InvokeAgentRuntime",
        )

        with self.assertRaises(RuntimeError) as ctx:
            list(agent_client.stream_agent_events(client, "arn:...", "session-id", "hi", None))

        self.assertIn("Failed to invoke agent", str(ctx.exception))

    def test_raises_when_response_is_not_streaming(self):
        client = MagicMock()
        client.invoke_agent_runtime.return_value = {
            "contentType": "application/json",
            "response": MagicMock(),
        }

        with self.assertRaises(RuntimeError) as ctx:
            list(agent_client.stream_agent_events(client, "arn:...", "session-id", "hi", None))

        self.assertIn("did not return a streaming response", str(ctx.exception))

    def test_raises_on_malformed_sse_json(self):
        client = MagicMock()
        mock_body = MagicMock()
        mock_body.iter_lines.return_value = iter([b"data: not-json"])
        client.invoke_agent_runtime.return_value = {
            "contentType": "text/event-stream",
            "response": mock_body,
        }

        with self.assertRaises(RuntimeError) as ctx:
            list(agent_client.stream_agent_events(client, "arn:...", "session-id", "hi", None))

        self.assertIn("malformed SSE frame", str(ctx.exception))

    def test_raises_when_event_missing_type(self):
        client = MagicMock()
        mock_body = MagicMock()
        mock_body.iter_lines.return_value = iter([b'data: {"data": "no type field"}'])
        client.invoke_agent_runtime.return_value = {
            "contentType": "text/event-stream",
            "response": mock_body,
        }

        with self.assertRaises(RuntimeError) as ctx:
            list(agent_client.stream_agent_events(client, "arn:...", "session-id", "hi", None))

        self.assertIn("missing 'type' field", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
