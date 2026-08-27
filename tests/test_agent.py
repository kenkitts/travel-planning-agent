"""Unit tests for agent/agent.py's pure-function helpers.

Covers extract_response_text(), extract_tool_result_text(),
stream_agent_turn(), parse_session_id(), and get_actor_id().
stream_agent_turn() is tested with a fake Agent double (an async generator
standing in for Agent.stream_async(), no real Strands Agent, no AWS/
network dependencies) so the event-translation and
MaxTokensReachedException handling paths can be exercised
deterministically. get_actor_id() is tested with real (unsigned-key)
PyJWT-encoded tokens, since signature verification is intentionally
skipped in the code under test (the Runtime's JWT authorizer already
validated it) — see DESIGN.md decision #31/#35.
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


def _run_async(coro_or_agen):
    """Drain an async generator into a list, using a fresh event loop.

    Small local helper instead of pulling in pytest-asyncio: these tests
    only need to fully consume a short async generator and assert on the
    collected events, not interleave with other async work.
    """
    import asyncio

    async def _collect():
        return [event async for event in coro_or_agen]

    return asyncio.run(_collect())


class _FakeAgent:
    """Stand-in for strands.Agent exposing only what stream_agent_turn() uses."""

    def __init__(self, events, messages=None):
        self._events = events
        self.messages = messages or []

    async def stream_async(self, _user_message):
        for event in self._events:
            yield event


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


class ExtractToolResultTextTests(unittest.TestCase):
    def test_extracts_text_block(self):
        tool_result = {"content": [{"text": "72F, sunny"}]}

        self.assertEqual(travel_agent.extract_tool_result_text(tool_result), "72F, sunny")

    def test_joins_multiple_text_blocks(self):
        tool_result = {"content": [{"text": "Part one."}, {"text": "Part two."}]}

        self.assertEqual(
            travel_agent.extract_tool_result_text(tool_result), "Part one.\nPart two."
        )

    def test_falls_back_to_str_for_non_text_content(self):
        tool_result = {"content": [{"json": {"temp": 72}}]}

        self.assertIn("temp", travel_agent.extract_tool_result_text(tool_result))

    def test_empty_content_returns_empty_string(self):
        self.assertEqual(travel_agent.extract_tool_result_text({"content": []}), "")


class ParseSessionIdTests(unittest.TestCase):
    def test_extracts_session_component(self):
        session_id = travel_agent.parse_session_id("cli-user___abc123")

        self.assertEqual(session_id, "abc123")

    def test_falls_back_to_raw_value_when_no_separator(self):
        session_id = travel_agent.parse_session_id("no-separator-here")

        self.assertEqual(session_id, "no-separator-here")

    def test_falls_back_to_default_when_none(self):
        session_id = travel_agent.parse_session_id(None)

        self.assertEqual(session_id, "default-session")


class SanitizeActorIdTests(unittest.TestCase):
    def test_leaves_already_valid_id_unchanged(self):
        self.assertEqual(travel_agent.sanitize_actor_id("00u1a2b3c4example"), "00u1a2b3c4example")

    def test_replaces_email_special_characters(self):
        # Real-world case, confirmed live: this Okta org's `sub` is an
        # email address, which AgentCore Memory's actorId pattern rejects
        # verbatim (no "@" or ".").
        self.assertEqual(
            travel_agent.sanitize_actor_id("kenkitts@amazon.com"), "kenkitts-amazon-com"
        )

    def test_strips_leading_disallowed_characters(self):
        # actorId must start with an alphanumeric per the real API pattern.
        self.assertEqual(travel_agent.sanitize_actor_id("@user123"), "user123")

    def test_preserves_allowed_punctuation(self):
        self.assertEqual(travel_agent.sanitize_actor_id("user-name_1/2:3"), "user-name_1/2:3")

    def test_falls_back_to_default_when_fully_sanitized_away(self):
        self.assertEqual(travel_agent.sanitize_actor_id("@@@"), travel_agent.DEFAULT_ACTOR_ID)

    def test_is_deterministic(self):
        self.assertEqual(
            travel_agent.sanitize_actor_id("kenkitts@amazon.com"),
            travel_agent.sanitize_actor_id("kenkitts@amazon.com"),
        )


class GetActorIdTests(unittest.TestCase):
    @staticmethod
    def _make_context(auth_header):
        context = MagicMock()
        context.request_headers = {"Authorization": auth_header} if auth_header else {}
        return context

    @staticmethod
    def _make_token(**claims):
        import jwt as pyjwt

        return pyjwt.encode(claims, "unused-secret", algorithm="HS256")

    def test_extracts_sub_claim_from_bearer_token(self):
        token = self._make_token(sub="00u1a2b3c4example")
        context = self._make_context(f"Bearer {token}")

        self.assertEqual(travel_agent.get_actor_id(context), "00u1a2b3c4example")

    def test_accepts_token_without_bearer_prefix(self):
        token = self._make_token(sub="00u1a2b3c4example")
        context = self._make_context(token)

        self.assertEqual(travel_agent.get_actor_id(context), "00u1a2b3c4example")

    def test_falls_back_when_no_authorization_header(self):
        context = self._make_context(None)

        self.assertEqual(travel_agent.get_actor_id(context), travel_agent.DEFAULT_ACTOR_ID)

    def test_falls_back_when_context_is_none(self):
        self.assertEqual(travel_agent.get_actor_id(None), travel_agent.DEFAULT_ACTOR_ID)

    def test_falls_back_when_token_has_no_sub_claim(self):
        token = self._make_token(client_id="some-client")
        context = self._make_context(f"Bearer {token}")

        self.assertEqual(travel_agent.get_actor_id(context), travel_agent.DEFAULT_ACTOR_ID)

    def test_falls_back_when_token_is_malformed(self):
        context = self._make_context("Bearer not-a-real-jwt")

        self.assertEqual(travel_agent.get_actor_id(context), travel_agent.DEFAULT_ACTOR_ID)

    def test_does_not_verify_signature(self):
        # Signature verification is intentionally skipped (the Runtime's
        # JWT authorizer already validated it) — a token signed with an
        # unknown/arbitrary key must still decode successfully here.
        token = self._make_token(sub="some-user")
        context = self._make_context(f"Bearer {token}")

        self.assertEqual(travel_agent.get_actor_id(context), "some-user")

    def test_sanitizes_email_shaped_sub_claim(self):
        # Regression test: confirmed live against a real deployment — this
        # Okta org's `sub` is an email address, and AgentCore Memory's
        # actorId pattern rejects "@"/"." verbatim, causing every
        # ListEvents/CreateEvent call to fail with ValidationException
        # until get_actor_id() started sanitizing the claim.
        token = self._make_token(sub="kenkitts@amazon.com")
        context = self._make_context(f"Bearer {token}")

        self.assertEqual(travel_agent.get_actor_id(context), "kenkitts-amazon-com")


class StreamAgentTurnTests(unittest.TestCase):
    def test_streams_text_deltas(self):
        fake_agent = _FakeAgent([{"data": "Here's "}, {"data": "your itinerary."}])

        events = _run_async(travel_agent.stream_agent_turn(fake_agent, "Plan a trip"))

        self.assertEqual(
            events,
            [
                {"type": "text", "data": "Here's "},
                {"type": "text", "data": "your itinerary."},
            ],
        )

    def test_streams_reasoning_deltas(self):
        fake_agent = _FakeAgent(
            [{"reasoning": True, "reasoningText": "Let me think..."}]
        )

        events = _run_async(travel_agent.stream_agent_turn(fake_agent, "Plan a trip"))

        self.assertEqual(events, [{"type": "reasoning", "data": "Let me think..."}])

    def test_streams_tool_use_once_per_tool_use_id_when_input_json_completes(self):
        fake_agent = _FakeAgent(
            [
                # Content-block-start: name present, input starts as "".
                {
                    "current_tool_use": {
                        "toolUseId": "t1",
                        "name": "weather",
                        "input": "",
                    }
                },
                # Partial JSON fragment: not yet parseable, skipped.
                {
                    "current_tool_use": {
                        "toolUseId": "t1",
                        "name": "weather",
                        "input": '{"city": "Port',
                    }
                },
                # Complete, parseable JSON: yielded with parsed input.
                {
                    "current_tool_use": {
                        "toolUseId": "t1",
                        "name": "weather",
                        "input": '{"city": "Portland", "days": 3}',
                    }
                },
                # Same toolUseId again after completion: not re-yielded.
                {
                    "current_tool_use": {
                        "toolUseId": "t1",
                        "name": "weather",
                        "input": '{"city": "Portland", "days": 3}',
                    }
                },
            ]
        )

        events = _run_async(travel_agent.stream_agent_turn(fake_agent, "Weather in Portland"))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "tool_use")
        self.assertEqual(events[0]["data"]["toolUseId"], "t1")
        self.assertEqual(events[0]["data"]["name"], "weather")
        self.assertEqual(events[0]["data"]["input"], {"city": "Portland", "days": 3})

    def test_streams_tool_use_with_no_arguments(self):
        # A zero-argument tool call still closes with "{}", not "".
        fake_agent = _FakeAgent(
            [
                {"current_tool_use": {"toolUseId": "t1", "name": "ping", "input": ""}},
                {"current_tool_use": {"toolUseId": "t1", "name": "ping", "input": "{}"}},
            ]
        )

        events = _run_async(travel_agent.stream_agent_turn(fake_agent, "Ping"))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["input"], {})

    def test_ignores_premature_non_object_json_fragment(self):
        # Regression test: an in-progress input fragment can coincidentally
        # parse as valid (but non-object) JSON, such as a bare string,
        # before the real arguments object is complete. This was observed
        # live producing a tool_use event with input="" in the diagnostic
        # panel — the fix requires the parsed value to be a dict.
        fake_agent = _FakeAgent(
            [
                {"current_tool_use": {"toolUseId": "t1", "name": "weather", "input": ""}},
                # Coincidentally valid JSON (a bare string), but not an
                # object — must NOT be yielded.
                {"current_tool_use": {"toolUseId": "t1", "name": "weather", "input": '""'}},
                # The real object finally completes.
                {
                    "current_tool_use": {
                        "toolUseId": "t1",
                        "name": "weather",
                        "input": '{"location": "Portland"}',
                    }
                },
            ]
        )

        events = _run_async(travel_agent.stream_agent_turn(fake_agent, "Weather in Portland"))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["input"], {"location": "Portland"})

    def test_streams_tool_result(self):
        fake_agent = _FakeAgent(
            [
                {
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "toolResult": {
                                    "toolUseId": "t1",
                                    "status": "success",
                                    "content": [{"text": "72F, sunny"}],
                                }
                            }
                        ],
                    }
                }
            ]
        )

        events = _run_async(travel_agent.stream_agent_turn(fake_agent, "Weather in Portland"))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "tool_result")
        self.assertEqual(events[0]["data"]["toolUseId"], "t1")
        self.assertEqual(events[0]["data"]["status"], "success")
        self.assertEqual(events[0]["data"]["text"], "72F, sunny")

    def test_ignores_assistant_message_events(self):
        # Only "message" events with role == "user" (tool results) matter;
        # assistant message-complete events carry nothing new to show.
        fake_agent = _FakeAgent(
            [{"message": {"role": "assistant", "content": [{"text": "done"}]}}]
        )

        events = _run_async(travel_agent.stream_agent_turn(fake_agent, "Plan a trip"))

        self.assertEqual(events, [])

    def test_streams_done_event_with_final_text(self):
        class _FakeResult:
            message = {"role": "assistant", "content": [{"text": "Final answer."}]}

        fake_agent = _FakeAgent([{"data": "Final "}, {"result": _FakeResult()}])

        events = _run_async(travel_agent.stream_agent_turn(fake_agent, "Plan a trip"))

        self.assertEqual(events[-1], {"type": "done", "data": "Final answer."})

    def test_ignores_lifecycle_events(self):
        fake_agent = _FakeAgent(
            [{"init_event_loop": True}, {"start_event_loop": True}, {"data": "hi"}]
        )

        events = _run_async(travel_agent.stream_agent_turn(fake_agent, "hi"))

        self.assertEqual(events, [{"type": "text", "data": "hi"}])

    def test_max_tokens_reached_yields_error_event_with_partial_text(self):
        class _RaisingAgent(_FakeAgent):
            async def stream_async(self, _user_message):
                yield {"data": "Here's your partial itinerary..."}
                raise travel_agent.MaxTokensReachedException("truncated")

        fake_agent = _RaisingAgent(
            [],
            messages=[
                {"role": "assistant", "content": [{"text": "Here's your partial itinerary..."}]}
            ],
        )

        events = _run_async(travel_agent.stream_agent_turn(fake_agent, "Plan a big trip"))

        self.assertEqual(events[0], {"type": "text", "data": "Here's your partial itinerary..."})
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("Here's your partial itinerary...", events[-1]["data"]["partial_text"])
        self.assertIn("cut off", events[-1]["data"]["note"])

    def test_max_tokens_reached_with_empty_partial_text(self):
        class _RaisingAgent(_FakeAgent):
            async def stream_async(self, _user_message):
                raise travel_agent.MaxTokensReachedException("truncated")
                yield  # pragma: no cover - unreachable, makes this an async generator

        fake_agent = _RaisingAgent([], messages=[{"role": "assistant", "content": []}])

        events = _run_async(travel_agent.stream_agent_turn(fake_agent, "Plan a big trip"))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertEqual(events[0]["data"]["partial_text"], "")
        self.assertIn("cut off", events[0]["data"]["note"])


if __name__ == "__main__":
    unittest.main()
