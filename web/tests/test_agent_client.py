"""Unit tests for web/agent_client.py.

Covers build_runtime_session_id(), build_invoke_payload(), and
stream_agent_events(). No live network access — requests.post() is
mocked (Phase 2 auth rearchitecture: this module was rewritten to make a
raw HTTPS call with a JWT bearer token instead of boto3's
invoke_agent_runtime(), since AWS's own docs confirm boto3 cannot invoke
a JWT-authorized Runtime at all — see agent_client.py's module
docstring).
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_client  # noqa: E402


def _make_response(lines: list[bytes], content_type: str = "text/event-stream", ok: bool = True, status_code: int = 200):
    """Build a mock requests.Response-like object for stream_agent_events()."""
    mock_response = MagicMock()
    mock_response.ok = ok
    mock_response.status_code = status_code
    mock_response.headers = {"content-type": content_type}
    mock_response.iter_lines.return_value = iter(lines)
    mock_response.text = "error body"
    return mock_response


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


class BuildInvocationUrlTests(unittest.TestCase):
    def test_url_encodes_the_arn_and_includes_qualifier(self):
        url = agent_client._build_invocation_url(
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test", "us-east-1", "PROD"
        )

        self.assertEqual(
            url,
            "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/"
            "arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A123456789012%3Aruntime%2Ftest"
            "/invocations?qualifier=PROD",
        )

    def test_omits_qualifier_param_when_not_provided(self):
        url = agent_client._build_invocation_url("arn:test", "us-east-1", None)

        self.assertNotIn("qualifier", url)


class StreamAgentEventsTests(unittest.TestCase):
    def test_yields_parsed_events_in_order(self):
        response = _make_response(
            [
                b'data: {"type": "text", "data": "Here\'s "}',
                b"",
                b'data: {"type": "text", "data": "your itinerary."}',
                b"",
                b'data: {"type": "done", "data": "Here\'s your itinerary."}',
            ]
        )

        with patch("agent_client.requests.post", return_value=response) as mock_post:
            events = list(
                agent_client.stream_agent_events(
                    "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
                    "us-east-1",
                    "session___" + "a" * 30,
                    "Plan a trip",
                    "ken",
                    "test-bearer-jwt",
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
        mock_post.assert_called_once()

    def test_sends_actor_id_prompt_and_bearer_token(self):
        response = _make_response([b'data: {"type": "done", "data": "ok"}'])

        with patch("agent_client.requests.post", return_value=response) as mock_post:
            list(
                agent_client.stream_agent_events(
                    "arn:test",
                    "us-east-1",
                    "session-id-here-1234567890123",
                    "hello",
                    "ken",
                    "test-bearer-jwt",
                )
            )

        call_kwargs = mock_post.call_args.kwargs
        sent_payload = json.loads(call_kwargs["data"])
        self.assertEqual(sent_payload, {"prompt": "hello", "actor_id": "ken"})
        self.assertEqual(
            call_kwargs["headers"]["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"],
            "session-id-here-1234567890123",
        )
        self.assertEqual(call_kwargs["headers"]["Authorization"], "Bearer test-bearer-jwt")
        self.assertEqual(call_kwargs["headers"]["Accept"], "text/event-stream")
        self.assertTrue(call_kwargs["stream"])

    def test_includes_qualifier_in_url_when_provided(self):
        response = _make_response([b'data: {"type": "done", "data": "ok"}'])

        with patch("agent_client.requests.post", return_value=response) as mock_post:
            list(
                agent_client.stream_agent_events(
                    "arn:test", "us-east-1", "session-id", "hello", "ken", "test-jwt", "PROD"
                )
            )

        url = mock_post.call_args.args[0]
        self.assertIn("qualifier=PROD", url)

    def test_omits_qualifier_when_not_provided(self):
        response = _make_response([b'data: {"type": "done", "data": "ok"}'])

        with patch("agent_client.requests.post", return_value=response) as mock_post:
            list(
                agent_client.stream_agent_events(
                    "arn:test", "us-east-1", "session-id", "hello", "ken", "test-jwt"
                )
            )

        url = mock_post.call_args.args[0]
        self.assertNotIn("qualifier", url)

    def test_skips_blank_lines(self):
        response = _make_response([b"", b'data: {"type": "done", "data": "ok"}', b""])

        with patch("agent_client.requests.post", return_value=response):
            events = list(
                agent_client.stream_agent_events(
                    "arn:...", "us-east-1", "session-id", "hi", "ken", "test-jwt"
                )
            )

        self.assertEqual(events, [{"type": "done", "data": "ok"}])

    def test_ignores_non_data_lines(self):
        response = _make_response([b": keepalive", b'data: {"type": "done", "data": "ok"}'])

        with patch("agent_client.requests.post", return_value=response):
            events = list(
                agent_client.stream_agent_events(
                    "arn:...", "us-east-1", "session-id", "hi", "ken", "test-jwt"
                )
            )

        self.assertEqual(events, [{"type": "done", "data": "ok"}])

    def test_raises_on_request_exception(self):
        import requests

        with patch("agent_client.requests.post", side_effect=requests.RequestException("boom")):
            with self.assertRaises(RuntimeError) as ctx:
                list(
                    agent_client.stream_agent_events(
                        "arn:...", "us-east-1", "session-id", "hi", "ken", "test-jwt"
                    )
                )

        self.assertIn("Failed to invoke agent", str(ctx.exception))

    def test_raises_on_non_2xx_response(self):
        response = _make_response([], ok=False, status_code=401)
        response.text = "Unauthorized: invalid or expired bearer token"

        with patch("agent_client.requests.post", return_value=response):
            with self.assertRaises(RuntimeError) as ctx:
                list(
                    agent_client.stream_agent_events(
                        "arn:...", "us-east-1", "session-id", "hi", "ken", "test-jwt"
                    )
                )

        self.assertIn("Failed to invoke agent (401)", str(ctx.exception))

    def test_raises_when_response_is_not_streaming(self):
        response = _make_response([], content_type="application/json")

        with patch("agent_client.requests.post", return_value=response):
            with self.assertRaises(RuntimeError) as ctx:
                list(
                    agent_client.stream_agent_events(
                        "arn:...", "us-east-1", "session-id", "hi", "ken", "test-jwt"
                    )
                )

        self.assertIn("did not return a streaming response", str(ctx.exception))

    def test_raises_on_malformed_sse_json(self):
        response = _make_response([b"data: not-json"])

        with patch("agent_client.requests.post", return_value=response):
            with self.assertRaises(RuntimeError) as ctx:
                list(
                    agent_client.stream_agent_events(
                        "arn:...", "us-east-1", "session-id", "hi", "ken", "test-jwt"
                    )
                )

        self.assertIn("malformed SSE frame", str(ctx.exception))

    def test_raises_when_event_missing_type(self):
        response = _make_response([b'data: {"data": "no type field"}'])

        with patch("agent_client.requests.post", return_value=response):
            with self.assertRaises(RuntimeError) as ctx:
                list(
                    agent_client.stream_agent_events(
                        "arn:...", "us-east-1", "session-id", "hi", "ken", "test-jwt"
                    )
                )

        self.assertIn("missing 'type' field", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
