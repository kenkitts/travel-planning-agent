"""Unit tests for cli/agent_client.py.

Covers build_runtime_session_id(), get_okta_access_token(), and
stream_agent_events(). No live network access, real AWS credentials, or
real Okta login — httpx.stream() and the token-helper subprocess are
mocked.
"""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli"))

import agent_client  # noqa: E402


def _make_streaming_response(status_code: int, lines: list[str], content_type: str = "text/event-stream"):
    """Build a mock object matching httpx.stream()'s context-manager response."""
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.headers = {"content-type": content_type}
    mock_response.iter_lines.return_value = iter(lines)
    mock_response.text = ""

    mock_ctx = MagicMock()
    mock_ctx.__enter__.return_value = mock_response
    mock_ctx.__exit__.return_value = False
    return mock_ctx


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


class GetOktaAccessTokenTests(unittest.TestCase):
    def _env(self, **overrides):
        env = {
            "OKTA_ISSUER": "https://example.okta.com/oauth2/default",
            "OKTA_CLIENT_ID": "test-client-id",
            "OKTA_SCOPES": "groups offline_access",
            "OKTA_REDIRECT_PORT": "5309",
        }
        env.update(overrides)
        return env

    @patch("agent_client.load_dotenv")
    @patch("agent_client.subprocess.run")
    @patch.dict("agent_client.os.environ", {}, clear=True)
    def test_raises_when_config_missing(self, mock_run, mock_dotenv):
        with self.assertRaises(RuntimeError) as ctx:
            agent_client.get_okta_access_token()

        self.assertIn("Missing required Okta config", str(ctx.exception))
        mock_run.assert_not_called()

    @patch("agent_client.load_dotenv")
    @patch("agent_client.subprocess.run")
    def test_returns_stdout_token_on_success(self, mock_run, mock_dotenv):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="fake.jwt.token\n", stderr=""
        )
        with patch.dict("agent_client.os.environ", self._env(), clear=True):
            token = agent_client.get_okta_access_token()

        self.assertEqual(token, "fake.jwt.token")

    @patch("agent_client.load_dotenv")
    @patch("agent_client.subprocess.run")
    def test_passes_okta_env_vars_to_subprocess(self, mock_run, mock_dotenv):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="tok\n", stderr=""
        )
        with patch.dict("agent_client.os.environ", self._env(), clear=True):
            agent_client.get_okta_access_token()

        call_kwargs = mock_run.call_args.kwargs
        passed_env = call_kwargs["env"]
        self.assertEqual(passed_env["OKTA_ISSUER"], "https://example.okta.com/oauth2/default")
        self.assertEqual(passed_env["OKTA_CLIENT_ID"], "test-client-id")

    @patch("agent_client.load_dotenv")
    @patch("agent_client.subprocess.run")
    def test_raises_with_stderr_on_nonzero_exit(self, mock_run, mock_dotenv):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="refusing to open browser"
        )
        with patch.dict("agent_client.os.environ", self._env(), clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                agent_client.get_okta_access_token()

        self.assertIn("refusing to open browser", str(ctx.exception))

    @patch("agent_client.load_dotenv")
    @patch("agent_client.subprocess.run")
    def test_raises_when_stdout_empty(self, mock_run, mock_dotenv):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.dict("agent_client.os.environ", self._env(), clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                agent_client.get_okta_access_token()

        self.assertIn("printed no token", str(ctx.exception))

    @patch("agent_client.load_dotenv")
    @patch("agent_client.subprocess.run", side_effect=FileNotFoundError())
    def test_raises_when_helper_script_missing(self, mock_run, mock_dotenv):
        with patch.dict("agent_client.os.environ", self._env(), clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                agent_client.get_okta_access_token()

        self.assertIn("not found", str(ctx.exception))


class StreamAgentEventsTests(unittest.TestCase):
    def test_yields_parsed_events_in_order(self):
        mock_ctx = _make_streaming_response(
            200,
            [
                'data: {"type": "text", "data": "Here\'s "}',
                "",
                'data: {"type": "text", "data": "your itinerary."}',
                "",
                'data: {"type": "done", "data": "Here\'s your itinerary."}',
            ],
        )

        with patch("agent_client.httpx.stream", return_value=mock_ctx) as mock_stream:
            events = list(
                agent_client.stream_agent_events(
                    "tok",
                    "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
                    "us-east-1",
                    "session___" + "a" * 30,
                    "Plan a trip",
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
        mock_stream.assert_called_once()

    def test_sends_bearer_token_and_session_header(self):
        mock_ctx = _make_streaming_response(200, ['data: {"type": "done", "data": "ok"}'])

        with patch("agent_client.httpx.stream", return_value=mock_ctx) as mock_stream:
            list(
                agent_client.stream_agent_events(
                    "my-access-token", "arn:...", "us-east-1", "session-id-here-1234567890123", "hello"
                )
            )

        call_args, call_kwargs = mock_stream.call_args
        headers = call_kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer my-access-token")
        self.assertEqual(
            headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"],
            "session-id-here-1234567890123",
        )
        self.assertEqual(headers["Accept"], "text/event-stream")

    def test_includes_qualifier_in_url_when_provided(self):
        mock_ctx = _make_streaming_response(200, ['data: {"type": "done", "data": "ok"}'])

        with patch("agent_client.httpx.stream", return_value=mock_ctx) as mock_stream:
            list(
                agent_client.stream_agent_events(
                    "tok", "arn:test", "us-east-1", "session-id", "hello", "PROD"
                )
            )

        call_args = mock_stream.call_args.args
        self.assertIn("qualifier=PROD", call_args[1])

    def test_omits_qualifier_when_not_provided(self):
        mock_ctx = _make_streaming_response(200, ['data: {"type": "done", "data": "ok"}'])

        with patch("agent_client.httpx.stream", return_value=mock_ctx) as mock_stream:
            list(
                agent_client.stream_agent_events(
                    "tok", "arn:test", "us-east-1", "session-id", "hello"
                )
            )

        call_args = mock_stream.call_args.args
        self.assertNotIn("qualifier", call_args[1])

    def test_skips_blank_lines(self):
        mock_ctx = _make_streaming_response(
            200, ["", 'data: {"type": "done", "data": "ok"}', ""]
        )

        with patch("agent_client.httpx.stream", return_value=mock_ctx):
            events = list(
                agent_client.stream_agent_events("tok", "arn:...", "us-east-1", "session-id", "hi")
            )

        self.assertEqual(events, [{"type": "done", "data": "ok"}])

    def test_ignores_non_data_lines(self):
        mock_ctx = _make_streaming_response(
            200, [": keepalive", 'data: {"type": "done", "data": "ok"}']
        )

        with patch("agent_client.httpx.stream", return_value=mock_ctx):
            events = list(
                agent_client.stream_agent_events("tok", "arn:...", "us-east-1", "session-id", "hi")
            )

        self.assertEqual(events, [{"type": "done", "data": "ok"}])

    def test_raises_on_http_error_status(self):
        mock_ctx = _make_streaming_response(401, [])
        mock_ctx.__enter__.return_value.text = "Unauthorized: invalid token"

        with patch("agent_client.httpx.stream", return_value=mock_ctx):
            with self.assertRaises(RuntimeError) as ctx:
                list(
                    agent_client.stream_agent_events(
                        "bad-tok", "arn:...", "us-east-1", "session-id", "hi"
                    )
                )

        self.assertIn("401", str(ctx.exception))

    def test_raises_when_response_is_not_streaming(self):
        mock_ctx = _make_streaming_response(200, [], content_type="application/json")

        with patch("agent_client.httpx.stream", return_value=mock_ctx):
            with self.assertRaises(RuntimeError) as ctx:
                list(
                    agent_client.stream_agent_events(
                        "tok", "arn:...", "us-east-1", "session-id", "hi"
                    )
                )

        self.assertIn("did not return a streaming response", str(ctx.exception))

    def test_raises_on_malformed_sse_json(self):
        mock_ctx = _make_streaming_response(200, ["data: not-json"])

        with patch("agent_client.httpx.stream", return_value=mock_ctx):
            with self.assertRaises(RuntimeError) as ctx:
                list(
                    agent_client.stream_agent_events(
                        "tok", "arn:...", "us-east-1", "session-id", "hi"
                    )
                )

        self.assertIn("malformed SSE frame", str(ctx.exception))

    def test_raises_when_event_missing_type(self):
        mock_ctx = _make_streaming_response(200, ['data: {"data": "no type field"}'])

        with patch("agent_client.httpx.stream", return_value=mock_ctx):
            with self.assertRaises(RuntimeError) as ctx:
                list(
                    agent_client.stream_agent_events(
                        "tok", "arn:...", "us-east-1", "session-id", "hi"
                    )
                )

        self.assertIn("missing 'type' field", str(ctx.exception))

    def test_raises_on_httpx_transport_error(self):
        with patch("agent_client.httpx.stream", side_effect=httpx.ConnectError("boom")):
            with self.assertRaises(RuntimeError) as ctx:
                list(
                    agent_client.stream_agent_events(
                        "tok", "arn:...", "us-east-1", "session-id", "hi"
                    )
                )

        self.assertIn("Failed to invoke agent", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
