"""Unit tests for agent/agent.py's pure-function helpers.

Covers extract_response_text(), run_agent_turn(), and
parse_runtime_session_id(). run_agent_turn() is tested with a fake Agent
double (no real Strands Agent, no AWS/network dependencies) so the
MaxTokensReachedException handling path can be exercised deterministically.
"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_AGENT_DIR = Path(__file__).resolve().parents[1] / "agent"
_AGENT_PATH = _AGENT_DIR / "agent.py"
sys.path.insert(0, str(_AGENT_DIR))

# agent.py reads MEMORY_ID/GATEWAY_URL etc. from the environment at import
# time via os.environ.get(...) with safe defaults, so no env setup is
# required beyond what's already handled by the module itself.
os.environ.setdefault("AWS_REGION", "us-east-1")

_spec = importlib.util.spec_from_file_location("travel_agent", _AGENT_PATH)
travel_agent = importlib.util.module_from_spec(_spec)
sys.modules["travel_agent"] = travel_agent
_spec.loader.exec_module(travel_agent)


class ExtractResponseTextTests(unittest.TestCase):
    def test_extracts_text_block(self):
        message = {"role": "assistant", "content": [{"text": "Here's your itinerary."}]}

        self.assertEqual(
            travel_agent.extract_response_text(message), "Here's your itinerary."
        )

    def test_skips_reasoning_content_block(self):
        message = {
            "role": "assistant",
            "content": [
                {"reasoningContent": {"reasoningText": {"text": "internal thoughts", "signature": "abc"}}},
                {"text": "Here's your itinerary."},
            ],
        }

        self.assertEqual(
            travel_agent.extract_response_text(message), "Here's your itinerary."
        )

    def test_joins_multiple_text_blocks(self):
        message = {
            "role": "assistant",
            "content": [{"text": "Part one."}, {"text": "Part two."}],
        }

        self.assertEqual(
            travel_agent.extract_response_text(message), "Part one.\nPart two."
        )

    def test_empty_content_returns_empty_string(self):
        self.assertEqual(travel_agent.extract_response_text({"role": "assistant", "content": []}), "")

    def test_missing_content_key_returns_empty_string(self):
        self.assertEqual(travel_agent.extract_response_text({"role": "assistant"}), "")


class ParseRuntimeSessionIdTests(unittest.TestCase):
    def test_splits_actor_and_session(self):
        actor_id, session_id = travel_agent.parse_runtime_session_id("ken___abc123")

        self.assertEqual(actor_id, "ken")
        self.assertEqual(session_id, "abc123")

    def test_falls_back_when_no_separator(self):
        actor_id, session_id = travel_agent.parse_runtime_session_id("no-separator-here")

        self.assertEqual(actor_id, travel_agent.DEFAULT_ACTOR_ID)

    def test_falls_back_when_none(self):
        actor_id, session_id = travel_agent.parse_runtime_session_id(None)

        self.assertEqual(actor_id, travel_agent.DEFAULT_ACTOR_ID)
        self.assertEqual(session_id, "default-session")


class RunAgentTurnTests(unittest.TestCase):
    def test_returns_text_on_success(self):
        fake_agent = MagicMock()
        fake_agent.return_value.message = {
            "role": "assistant",
            "content": [{"text": "Here's your itinerary."}],
        }

        result = travel_agent.run_agent_turn(fake_agent, "Plan a trip")

        self.assertEqual(result, "Here's your itinerary.")
        fake_agent.assert_called_once_with("Plan a trip")

    def test_returns_partial_text_with_note_on_max_tokens(self):
        fake_agent = MagicMock()
        fake_agent.side_effect = travel_agent.MaxTokensReachedException("truncated")
        # Per Strands: the partial message is appended to agent.messages
        # before the exception is raised.
        fake_agent.messages = [
            {"role": "assistant", "content": [{"text": "Here's your partial itinerary..."}]}
        ]

        result = travel_agent.run_agent_turn(fake_agent, "Plan a big trip")

        self.assertIn("Here's your partial itinerary...", result)
        self.assertIn("cut off", result)

    def test_returns_fallback_message_when_partial_text_is_empty(self):
        fake_agent = MagicMock()
        fake_agent.side_effect = travel_agent.MaxTokensReachedException("truncated")
        fake_agent.messages = [{"role": "assistant", "content": []}]

        result = travel_agent.run_agent_turn(fake_agent, "Plan a big trip")

        self.assertIn("cut off", result)
        self.assertNotIn("None", result)


if __name__ == "__main__":
    unittest.main()
