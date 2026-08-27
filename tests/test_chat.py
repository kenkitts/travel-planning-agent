"""Unit tests for cli/chat.py's _consume_stream() helper.

Mocks agent_client.stream_agent_events() (imported into chat's namespace)
so no real AWS/Okta calls or network access are needed.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli"))

import chat  # noqa: E402


class ConsumeStreamTests(unittest.TestCase):
    @patch("chat.stream_agent_events")
    def test_joins_text_deltas_when_no_done_event(self, mock_stream):
        mock_stream.return_value = iter(
            [{"type": "text", "data": "Here's "}, {"type": "text", "data": "your plan."}]
        )

        result = chat._consume_stream("tok", "arn:...", "us-east-1", "session-id", "hi", None)

        self.assertEqual(result, "Here's your plan.")

    @patch("chat.stream_agent_events")
    def test_prefers_done_events_full_text(self, mock_stream):
        mock_stream.return_value = iter(
            [
                {"type": "text", "data": "Here's "},
                {"type": "done", "data": "Here's your complete plan."},
            ]
        )

        result = chat._consume_stream("tok", "arn:...", "us-east-1", "session-id", "hi", None)

        self.assertEqual(result, "Here's your complete plan.")

    @patch("chat.stream_agent_events")
    def test_error_event_returns_partial_text_with_note(self, mock_stream):
        mock_stream.return_value = iter(
            [
                {"type": "text", "data": "Here's your partial itinerary..."},
                {
                    "type": "error",
                    "data": {
                        "partial_text": "Here's your partial itinerary...",
                        "note": "That response got cut off.",
                    },
                },
            ]
        )

        result = chat._consume_stream("tok", "arn:...", "us-east-1", "session-id", "hi", None)

        self.assertIn("Here's your partial itinerary...", result)
        self.assertIn("cut off", result)

    @patch("chat.stream_agent_events")
    def test_error_event_with_no_prior_text_returns_fallback(self, mock_stream):
        mock_stream.return_value = iter(
            [{"type": "error", "data": "'prompt' is required"}]
        )

        result = chat._consume_stream("tok", "arn:...", "us-east-1", "session-id", "", None)

        self.assertEqual(result, "'prompt' is required")

    @patch("chat.stream_agent_events")
    def test_ignores_diagnostic_only_events(self, mock_stream):
        mock_stream.return_value = iter(
            [
                {"type": "reasoning", "data": "thinking..."},
                {"type": "tool_use", "data": {"name": "weather"}},
                {"type": "tool_result", "data": {"text": "72F"}},
                {"type": "text", "data": "It'll be warm."},
                {"type": "done", "data": "It'll be warm."},
            ]
        )

        result = chat._consume_stream("tok", "arn:...", "us-east-1", "session-id", "weather?", None)

        self.assertEqual(result, "It'll be warm.")


if __name__ == "__main__":
    unittest.main()
