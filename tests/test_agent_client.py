"""Unit tests for cli/agent_client.py.

Covers build_runtime_session_id(), build_invoke_payload(), and
stream_agent_events(). No live network access or real AWS credentials —
boto3's bedrock-agentcore client is mocked.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli"))

import agent_client  # noqa: E402


def _make_streaming_response(lines: list[bytes], content_type: str = "text/event-stream"):
    """Build a mock invoke_agent_runtime() response with a StreamingBody-like body."""
    mock_body = MagicMock()
    mock_body.iter_lines.return_value = iter(lines)
    return {"contentType": content_type, "response": mock_body}


class BuildRuntimeSessionIdTests(unittest.TestCase):
    def test_meets_minimum_length(self):
        session_id = agent_client.build_runtime_session_id()

        self.assertGreaterEqual(len(session_id), agent_client.MIN_SESSION_ID_LENGTH)

    def test_is_unique_per_call(self):
        first = agent_client.build_runtime_session_id()
        second = agent_client.build_runtime_session_id()

        self.assertNotEqual(first, second)

    def test_includes_separator(self):
        session_id = agent_client.build_runtime_session_id()

        self.assertIn(agent_client.SESSION_ID_SEPARATOR, session_id)


class BuildInvokePayloadTests(unittest.TestCase):
    def test_includes_prompt_and_actor_id(self):
        payload = agent_client.build_invoke_payload("Plan a trip", "ken")

        self.assertEqual(payload, {"prompt": "Plan a trip", "actor_id": "ken"})


class StreamAgentEventsTests(unittest.TestCase):
    def test_yields_parsed_events_in_order(self):
        response = _make_streaming_response(
            [
                b'data: {"type": "text", "data": "Here\'s "}',
                b"",
                b'data: {"type": "text", "data": "your itinerary."}',
                b"",
                b'data: {"type": "done", "data": "Here\'s your itinerary."}',
            ]
        )
        mock_client = MagicMock()
        mock_client.invoke_agent_runtime.return_value = response

        with patch("agent_client.boto3.client", return_value=mock_client):
            events = list(
                agent_client.stream_agent_events(
                    "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
                    "us-east-1",
                    "session___" + "a" * 30,
                    "Plan a trip",
                    "ken",
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

    def test_sends_actor_id_and_prompt_in_payload(self):
        response = _make_streaming_response([b'data: {"type": "done", "data": "ok"}'])
        mock_client = MagicMock()
        mock_client.invoke_agent_runtime.return_value = response

        with patch("agent_client.boto3.client", return_value=mock_client):
            list(
                agent_client.stream_agent_events(
                    "arn:test", "us-east-1", "session-id-here-1234567890123", "hello", "ken"
                )
            )

        call_kwargs = mock_client.invoke_agent_runtime.call_args.kwargs
        import json

        sent_payload = json.loads(call_kwargs["payload"])
        self.assertEqual(sent_payload, {"prompt": "hello", "actor_id": "ken"})
        self.assertEqual(call_kwargs["runtimeSessionId"], "session-id-here-1234567890123")
        self.assertEqual(call_kwargs["accept"], "text/event-stream")

    def test_includes_qualifier_when_provided(self):
        response = _make_streaming_response([b'data: {"type": "done", "data": "ok"}'])
        mock_client = MagicMock()
        mock_client.invoke_agent_runtime.return_value = response

        with patch("agent_client.boto3.client", return_value=mock_client):
            list(
                agent_client.stream_agent_events(
                    "arn:test", "us-east-1", "session-id", "hello", "ken", "PROD"
                )
            )

        call_kwargs = mock_client.invoke_agent_runtime.call_args.kwargs
        self.assertEqual(call_kwargs["qualifier"], "PROD")

    def test_omits_qualifier_when_not_provided(self):
        response = _make_streaming_response([b'data: {"type": "done", "data": "ok"}'])
        mock_client = MagicMock()
        mock_client.invoke_agent_runtime.return_value = response

        with patch("agent_client.boto3.client", return_value=mock_client):
            list(
                agent_client.stream_agent_events(
                    "arn:test", "us-east-1", "session-id", "hello", "ken"
                )
            )

        call_kwargs = mock_client.invoke_agent_runtime.call_args.kwargs
        self.assertNotIn("qualifier", call_kwargs)

    def test_skips_blank_lines(self):
        response = _make_streaming_response(
            [b"", b'data: {"type": "done", "data": "ok"}', b""]
        )
        mock_client = MagicMock()
        mock_client.invoke_agent_runtime.return_value = response

        with patch("agent_client.boto3.client", return_value=mock_client):
            events = list(
                agent_client.stream_agent_events(
                    "arn:...", "us-east-1", "session-id", "hi", "ken"
                )
            )

        self.assertEqual(events, [{"type": "done", "data": "ok"}])

    def test_ignores_non_data_lines(self):
        response = _make_streaming_response(
            [b": keepalive", b'data: {"type": "done", "data": "ok"}']
        )
        mock_client = MagicMock()
        mock_client.invoke_agent_runtime.return_value = response

        with patch("agent_client.boto3.client", return_value=mock_client):
            events = list(
                agent_client.stream_agent_events(
                    "arn:...", "us-east-1", "session-id", "hi", "ken"
                )
            )

        self.assertEqual(events, [{"type": "done", "data": "ok"}])

    def test_raises_on_client_error(self):
        mock_client = MagicMock()
        mock_client.invoke_agent_runtime.side_effect = Exception("boom")

        with patch("agent_client.boto3.client", return_value=mock_client):
            with self.assertRaises(RuntimeError) as ctx:
                list(
                    agent_client.stream_agent_events(
                        "arn:...", "us-east-1", "session-id", "hi", "ken"
                    )
                )

        self.assertIn("Failed to invoke agent", str(ctx.exception))

    def test_raises_when_response_is_not_streaming(self):
        response = _make_streaming_response([], content_type="application/json")
        mock_client = MagicMock()
        mock_client.invoke_agent_runtime.return_value = response

        with patch("agent_client.boto3.client", return_value=mock_client):
            with self.assertRaises(RuntimeError) as ctx:
                list(
                    agent_client.stream_agent_events(
                        "arn:...", "us-east-1", "session-id", "hi", "ken"
                    )
                )

        self.assertIn("did not return a streaming response", str(ctx.exception))

    def test_raises_on_malformed_sse_json(self):
        response = _make_streaming_response([b"data: not-json"])
        mock_client = MagicMock()
        mock_client.invoke_agent_runtime.return_value = response

        with patch("agent_client.boto3.client", return_value=mock_client):
            with self.assertRaises(RuntimeError) as ctx:
                list(
                    agent_client.stream_agent_events(
                        "arn:...", "us-east-1", "session-id", "hi", "ken"
                    )
                )

        self.assertIn("malformed SSE frame", str(ctx.exception))

    def test_raises_when_event_missing_type(self):
        response = _make_streaming_response([b'data: {"data": "no type field"}'])
        mock_client = MagicMock()
        mock_client.invoke_agent_runtime.return_value = response

        with patch("agent_client.boto3.client", return_value=mock_client):
            with self.assertRaises(RuntimeError) as ctx:
                list(
                    agent_client.stream_agent_events(
                        "arn:...", "us-east-1", "session-id", "hi", "ken"
                    )
                )

        self.assertIn("missing 'type' field", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
