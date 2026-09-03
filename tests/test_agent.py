"""Unit tests for agent/agent.py's pure-function helpers.

Covers extract_response_text(), extract_tool_result_text(),
stream_agent_turn(), parse_session_id(), get_actor_id(), and
build_mcp_client(). stream_agent_turn() is tested with a fake Agent double
(an async generator standing in for Agent.stream_async(), no real Strands
Agent, no AWS/network dependencies) so the event-translation and
MaxTokensReachedException handling paths can be exercised
deterministically. get_actor_id() is tested against both the Phase 3 JWT
path (a `sub` claim decoded from a fake inbound Authorization header, via
BedrockAgentCoreContext) and the IAM-mode fallback path (a plain payload
dict — DESIGN.md decision #37). build_mcp_client() is tested against both
the default IAM/SigV4 path and the Phase 3 OBO-exchange path (a fake
IdentityClient.get_token(), no real AgentCore Identity/network calls).
"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


def asyncio_run(coro):
    """Run a plain coroutine (not an async generator) to completion."""
    import asyncio

    return asyncio.run(coro)


class _FakeAgent:
    """Stand-in for strands.Agent exposing only what stream_agent_turn() uses."""

    def __init__(self, events, messages=None):
        self._events = events
        self.messages = messages or []

    async def stream_async(self, _user_message, **_kwargs):
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
    def tearDown(self):
        # Reset the request-headers context var so tests don't leak state
        # into each other — BedrockAgentCoreContext's ContextVar has no
        # default, so explicitly setting {} here restores "no headers
        # present" for get_request_headers()'s purposes (an empty dict has
        # no "Authorization" key, same as None does for get_actor_id()).
        travel_agent.BedrockAgentCoreContext.set_request_headers({})

    @staticmethod
    def _fake_jwt(sub: str) -> str:
        """An unsigned-but-well-formed JWT carrying only a `sub` claim.

        get_actor_id()'s JWT path deliberately does not verify the
        signature (see _sub_from_authorization_header()'s docstring), so
        a real signature isn't needed to exercise it — pyjwt's own
        "none" algorithm produces a real, decodable JWT structure without
        needing a signing key.
        """
        import jwt as _jwt

        return _jwt.encode({"sub": sub}, key=None, algorithm="none")

    def test_extracts_sub_from_authorization_header_when_present(self):
        travel_agent.BedrockAgentCoreContext.set_request_headers(
            {"Authorization": f"Bearer {self._fake_jwt('kenkitts@amazon.com')}"}
        )

        self.assertEqual(
            travel_agent.get_actor_id({"prompt": "hi", "actor_id": "ignored"}),
            "kenkitts-amazon-com",
        )

    def test_prefers_sub_claim_over_payload_actor_id(self):
        # Regression: when both a real inbound JWT and a payload actor_id
        # are present (JWT inbound auth mode), the verified sub must win.
        travel_agent.BedrockAgentCoreContext.set_request_headers(
            {"Authorization": f"Bearer {self._fake_jwt('real-sub')}"}
        )

        self.assertEqual(
            travel_agent.get_actor_id({"prompt": "hi", "actor_id": "untrusted-payload-value"}),
            "real-sub",
        )

    def test_falls_back_to_payload_when_no_authorization_header(self):
        # IAM inbound auth mode: no bearer token exists at all.
        self.assertEqual(
            travel_agent.get_actor_id({"prompt": "hi", "actor_id": "ken"}), "ken"
        )

    def test_falls_back_to_payload_when_authorization_header_not_bearer(self):
        travel_agent.BedrockAgentCoreContext.set_request_headers(
            {"Authorization": "Basic dXNlcjpwYXNz"}
        )

        self.assertEqual(
            travel_agent.get_actor_id({"prompt": "hi", "actor_id": "ken"}), "ken"
        )

    def test_falls_back_to_payload_when_bearer_token_is_malformed(self):
        travel_agent.BedrockAgentCoreContext.set_request_headers(
            {"Authorization": "Bearer not-a-real-jwt"}
        )

        self.assertEqual(
            travel_agent.get_actor_id({"prompt": "hi", "actor_id": "ken"}), "ken"
        )

    def test_extracts_actor_id_from_payload(self):
        self.assertEqual(
            travel_agent.get_actor_id({"prompt": "hi", "actor_id": "ken"}), "ken"
        )

    def test_falls_back_when_actor_id_missing(self):
        self.assertEqual(
            travel_agent.get_actor_id({"prompt": "hi"}), travel_agent.DEFAULT_ACTOR_ID
        )

    def test_falls_back_when_payload_is_none(self):
        self.assertEqual(travel_agent.get_actor_id(None), travel_agent.DEFAULT_ACTOR_ID)

    def test_falls_back_when_actor_id_is_empty_string(self):
        self.assertEqual(
            travel_agent.get_actor_id({"prompt": "hi", "actor_id": ""}),
            travel_agent.DEFAULT_ACTOR_ID,
        )

    def test_sanitizes_email_shaped_actor_id(self):
        # Regression case carried over from the JWT-based implementation:
        # an upstream identity claim (e.g. an OIDC `sub`) may be an email
        # address, which AgentCore Memory's actorId pattern rejects
        # verbatim — the web container is responsible for sanitizing
        # before it ever reaches here, but get_actor_id() sanitizes
        # defensively regardless of caller.
        self.assertEqual(
            travel_agent.get_actor_id({"prompt": "hi", "actor_id": "kenkitts@amazon.com"}),
            "kenkitts-amazon-com",
        )

    def test_coerces_non_string_actor_id(self):
        self.assertEqual(
            travel_agent.get_actor_id({"prompt": "hi", "actor_id": 12345}), "12345"
        )


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
            stop_reason = "end_turn"

        fake_agent = _FakeAgent([{"data": "Final "}, {"result": _FakeResult()}])

        events = _run_async(travel_agent.stream_agent_turn(fake_agent, "Plan a trip"))

        self.assertEqual(events[-1], {"type": "done", "data": "Final answer."})

    def test_passes_turns_limit_to_stream_async(self):
        captured_kwargs = {}

        class _CapturingAgent(_FakeAgent):
            async def stream_async(self, _user_message, **kwargs):
                captured_kwargs.update(kwargs)
                yield {"data": "hi"}

        fake_agent = _CapturingAgent([])

        _run_async(travel_agent.stream_agent_turn(fake_agent, "Plan a trip"))

        self.assertEqual(
            captured_kwargs.get("limits"), {"turns": travel_agent.AGENT_MAX_TURNS}
        )

    def test_turns_limit_reached_yields_error_event_with_partial_text(self):
        # Regression test: a non-converging agent loop (repeated tool calls
        # that never reach a final answer) is bounded by
        # Limits(turns=AGENT_MAX_TURNS) — Strands ends the loop gracefully
        # with stop_reason "limit_turns" rather than raising, so this must
        # be handled explicitly in the "result" branch, not via an
        # exception handler like the two cutoff cases above it.
        class _FakeResult:
            message = {"role": "assistant", "content": [{"text": "Partial answer so far."}]}
            stop_reason = "limit_turns"

        fake_agent = _FakeAgent([{"data": "Partial "}, {"result": _FakeResult()}])

        events = _run_async(travel_agent.stream_agent_turn(fake_agent, "Plan a huge trip"))

        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(events[-1]["data"]["partial_text"], "Partial answer so far.")
        self.assertIn("more steps", events[-1]["data"]["note"])

    def test_turns_limit_reached_with_empty_partial_text(self):
        class _FakeResult:
            message = {"role": "assistant", "content": []}
            stop_reason = "limit_turns"

        fake_agent = _FakeAgent([{"result": _FakeResult()}])

        events = _run_async(travel_agent.stream_agent_turn(fake_agent, "Plan a huge trip"))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertEqual(events[0]["data"]["partial_text"], "")

    def test_ignores_lifecycle_events(self):
        fake_agent = _FakeAgent(
            [{"init_event_loop": True}, {"start_event_loop": True}, {"data": "hi"}]
        )

        events = _run_async(travel_agent.stream_agent_turn(fake_agent, "hi"))

        self.assertEqual(events, [{"type": "text", "data": "hi"}])

    def test_max_tokens_reached_yields_error_event_with_partial_text(self):
        class _RaisingAgent(_FakeAgent):
            async def stream_async(self, _user_message, **_kwargs):
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
            async def stream_async(self, _user_message, **_kwargs):
                raise travel_agent.MaxTokensReachedException("truncated")
                yield  # pragma: no cover - unreachable, makes this an async generator

        fake_agent = _RaisingAgent([], messages=[{"role": "assistant", "content": []}])

        events = _run_async(travel_agent.stream_agent_turn(fake_agent, "Plan a big trip"))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertEqual(events[0]["data"]["partial_text"], "")
        self.assertIn("cut off", events[0]["data"]["note"])

    def test_model_throttled_after_retries_exhausted_yields_error_event(self):
        # Strands' own ModelRetryStrategy retries a throttled model call
        # transparently; this exception only reaches stream_agent_turn()
        # once all retries are exhausted, so there's no partial text to
        # preserve — the whole call failed, not a mid-stream cutoff.
        class _RaisingAgent(_FakeAgent):
            async def stream_async(self, _user_message, **_kwargs):
                raise travel_agent.ModelThrottledException("rate limit exceeded")
                yield  # pragma: no cover - unreachable, makes this an async generator

        fake_agent = _RaisingAgent([])

        events = _run_async(travel_agent.stream_agent_turn(fake_agent, "Plan a trip"))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("rate-limited", events[0]["data"]["note"])
        self.assertNotIn("partial_text", events[0]["data"])


class BuildMcpClientTests(unittest.TestCase):
    def setUp(self):
        # Reset module-level config each test — build_mcp_client() reads
        # these as plain module globals (set once at import time from
        # os.environ), so tests override them directly rather than
        # through the environment.
        self._orig_gateway_url = travel_agent.GATEWAY_URL
        self._orig_obo_provider_name = travel_agent.GATEWAY_OBO_PROVIDER_NAME
        travel_agent.GATEWAY_URL = "https://example-gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"

    def tearDown(self):
        travel_agent.GATEWAY_URL = self._orig_gateway_url
        travel_agent.GATEWAY_OBO_PROVIDER_NAME = self._orig_obo_provider_name
        travel_agent.BedrockAgentCoreContext.set_workload_access_token("")
        travel_agent.BedrockAgentCoreContext.set_request_headers({})
        travel_agent._GATEWAY_OBO_TOKEN_CACHE.clear()

    def test_returns_none_when_gateway_url_not_set(self):
        travel_agent.GATEWAY_URL = ""

        client = asyncio_run(travel_agent.build_mcp_client())

        self.assertIsNone(client)

    def test_returns_iam_client_when_obo_provider_not_configured(self):
        travel_agent.GATEWAY_OBO_PROVIDER_NAME = ""

        with patch.object(travel_agent, "aws_iam_streamablehttp_client") as fake_iam_client:
            client = asyncio_run(travel_agent.build_mcp_client())
            self.assertIsNotNone(client)
            # MCPClient stores its transport factory as a private attribute;
            # call it directly to confirm the IAM path (not the OBO/bearer
            # path) is what's wired up, without needing MCPClient's full
            # background-thread connection lifecycle.
            client._transport_callable()
            fake_iam_client.assert_called_once_with(
                endpoint=travel_agent.GATEWAY_URL,
                aws_service="bedrock-agentcore",
                aws_region=travel_agent.AWS_REGION,
            )

    def test_raises_when_obo_configured_but_no_workload_token(self):
        travel_agent.GATEWAY_OBO_PROVIDER_NAME = "travel-planning-agent-gateway-obo"
        travel_agent.BedrockAgentCoreContext.set_workload_access_token("")

        with self.assertRaises(RuntimeError):
            asyncio_run(travel_agent.build_mcp_client())

    def test_obo_path_exchanges_workload_token_for_gateway_bearer_token(self):
        travel_agent.GATEWAY_OBO_PROVIDER_NAME = "travel-planning-agent-gateway-obo"
        travel_agent.BedrockAgentCoreContext.set_workload_access_token("fake-workload-token")

        fake_get_token = AsyncMock(return_value="fake-gateway-jwt")
        with patch.object(travel_agent, "IdentityClient") as fake_identity_client_cls, \
                patch.object(travel_agent, "streamablehttp_client") as fake_streamable_client:
            fake_identity_client_cls.return_value.get_token = fake_get_token

            client = asyncio_run(travel_agent.build_mcp_client())
            self.assertIsNotNone(client)
            client._transport_callable()

            fake_identity_client_cls.assert_called_once_with(travel_agent.AWS_REGION)
            fake_get_token.assert_awaited_once_with(
                provider_name="travel-planning-agent-gateway-obo",
                scopes=[travel_agent.GATEWAY_OBO_SCOPE],
                audiences=[travel_agent.GATEWAY_OBO_AUDIENCE],
                agent_identity_token="fake-workload-token",
                auth_flow="ON_BEHALF_OF_TOKEN_EXCHANGE",
                custom_parameters={
                    "subject_token_type": travel_agent.GATEWAY_OBO_SUBJECT_TOKEN_TYPE
                },
            )
            fake_streamable_client.assert_called_once_with(
                url=travel_agent.GATEWAY_URL,
                headers={"Authorization": "Bearer fake-gateway-jwt"},
            )

    @staticmethod
    def _fake_gateway_jwt(exp_offset_seconds: float) -> str:
        """An unsigned JWT with just an `exp` claim, for cache-TTL tests.

        Mirrors GetActorIdTests._fake_jwt()'s "none"-algorithm approach —
        _jwt_expiry() never verifies the signature (see its own
        docstring), so no signing key is needed here either.
        """
        import time as _time

        import jwt as _jwt

        return _jwt.encode(
            {"exp": _time.time() + exp_offset_seconds}, key=None, algorithm="none"
        )

    def test_obo_token_is_cached_per_sub_across_calls(self):
        travel_agent.GATEWAY_OBO_PROVIDER_NAME = "travel-planning-agent-gateway-obo"
        travel_agent.BedrockAgentCoreContext.set_workload_access_token("fake-workload-token")
        travel_agent.BedrockAgentCoreContext.set_request_headers(
            {"Authorization": f"Bearer {GetActorIdTests._fake_jwt('alice@example.com')}"}
        )

        fake_get_token = AsyncMock(return_value=self._fake_gateway_jwt(3600))
        with patch.object(travel_agent, "IdentityClient") as fake_identity_client_cls, \
                patch.object(travel_agent, "streamablehttp_client"):
            fake_identity_client_cls.return_value.get_token = fake_get_token

            asyncio_run(travel_agent.build_mcp_client())
            asyncio_run(travel_agent.build_mcp_client())

            # Second call hit the cache — the exchange only happened once,
            # even though build_mcp_client() (and thus a fresh MCPClient
            # per this module's own per-request-isolation docstring) was
            # invoked twice for the same caller.
            fake_get_token.assert_awaited_once()

    def test_obo_token_cache_is_scoped_per_sub(self):
        travel_agent.GATEWAY_OBO_PROVIDER_NAME = "travel-planning-agent-gateway-obo"
        travel_agent.BedrockAgentCoreContext.set_workload_access_token("fake-workload-token")

        fake_get_token = AsyncMock(
            side_effect=[self._fake_gateway_jwt(3600), self._fake_gateway_jwt(3600)]
        )
        with patch.object(travel_agent, "IdentityClient") as fake_identity_client_cls, \
                patch.object(travel_agent, "streamablehttp_client"):
            fake_identity_client_cls.return_value.get_token = fake_get_token

            travel_agent.BedrockAgentCoreContext.set_request_headers(
                {"Authorization": f"Bearer {GetActorIdTests._fake_jwt('alice@example.com')}"}
            )
            asyncio_run(travel_agent.build_mcp_client())

            travel_agent.BedrockAgentCoreContext.set_request_headers(
                {"Authorization": f"Bearer {GetActorIdTests._fake_jwt('bob@example.com')}"}
            )
            asyncio_run(travel_agent.build_mcp_client())

            # A different caller's `sub` must never hit alice's cache entry
            # — each of the two distinct users triggers its own exchange.
            self.assertEqual(fake_get_token.await_count, 2)

    def test_expired_cached_obo_token_triggers_fresh_exchange(self):
        travel_agent.GATEWAY_OBO_PROVIDER_NAME = "travel-planning-agent-gateway-obo"
        travel_agent.BedrockAgentCoreContext.set_workload_access_token("fake-workload-token")
        travel_agent.BedrockAgentCoreContext.set_request_headers(
            {"Authorization": f"Bearer {GetActorIdTests._fake_jwt('alice@example.com')}"}
        )

        # Expires in 10s, but the refresh skew (60s) means it's already
        # treated as expired by the cache — the second call must re-exchange.
        fake_get_token = AsyncMock(
            side_effect=[self._fake_gateway_jwt(10), self._fake_gateway_jwt(3600)]
        )
        with patch.object(travel_agent, "IdentityClient") as fake_identity_client_cls, \
                patch.object(travel_agent, "streamablehttp_client"):
            fake_identity_client_cls.return_value.get_token = fake_get_token

            asyncio_run(travel_agent.build_mcp_client())
            asyncio_run(travel_agent.build_mcp_client())

            self.assertEqual(fake_get_token.await_count, 2)


class BuildModelTests(unittest.TestCase):
    """Covers build_model()'s BedrockModel/AnthropicModel branch — see
    DESIGN.md's Gateway-routed-inference decision. Mirrors
    BuildMcpClientTests' setUp/tearDown/patching conventions, since both
    functions read the same module-level OBO config and token cache.
    """

    def setUp(self):
        self._orig_gateway_inference_url = travel_agent.GATEWAY_INFERENCE_URL
        self._orig_obo_provider_name = travel_agent.GATEWAY_OBO_PROVIDER_NAME

    def tearDown(self):
        travel_agent.GATEWAY_INFERENCE_URL = self._orig_gateway_inference_url
        travel_agent.GATEWAY_OBO_PROVIDER_NAME = self._orig_obo_provider_name
        travel_agent.BedrockAgentCoreContext.set_workload_access_token("")
        travel_agent.BedrockAgentCoreContext.set_request_headers({})
        travel_agent._GATEWAY_OBO_TOKEN_CACHE.clear()

    def test_returns_bedrock_model_by_default(self):
        travel_agent.GATEWAY_INFERENCE_URL = ""

        model = asyncio_run(travel_agent.build_model())

        self.assertIsInstance(model, travel_agent.BedrockModel)

    def test_raises_when_inference_url_set_but_no_workload_token(self):
        travel_agent.GATEWAY_INFERENCE_URL = "https://example-gateway.gateway.bedrock-agentcore.us-east-1.amazonaws.com/inference"
        travel_agent.BedrockAgentCoreContext.set_workload_access_token("")

        with self.assertRaises(RuntimeError):
            asyncio_run(travel_agent.build_model())

    def test_returns_anthropic_model_via_gateway_when_inference_url_set(self):
        travel_agent.GATEWAY_INFERENCE_URL = (
            "https://example-gateway.gateway.bedrock-agentcore.us-east-1.amazonaws.com/inference"
        )
        travel_agent.GATEWAY_OBO_PROVIDER_NAME = "travel-planning-agent-gateway-obo"
        travel_agent.BedrockAgentCoreContext.set_workload_access_token("fake-workload-token")

        fake_get_token = AsyncMock(return_value="fake-gateway-jwt")
        with patch.object(travel_agent, "IdentityClient") as fake_identity_client_cls:
            fake_identity_client_cls.return_value.get_token = fake_get_token

            model = asyncio_run(travel_agent.build_model())

            self.assertIsInstance(model, travel_agent.AnthropicModel)
            # Gateway-routed calls need the bare foundation-model ID
            # (e.g. "anthropic.claude-sonnet-5"), not MODEL_ID's own
            # "us."-prefixed cross-region-inference-profile form — found
            # live: routing "us.anthropic.claude-sonnet-5" through the
            # Gateway's bedrock-mantle connector target returned a real
            # 404 ("Model ... not found on any target"), since that
            # prefix is a bedrock-runtime/Converse-specific concept the
            # connector's own model routing doesn't resolve.
            self.assertEqual(model.config["model_id"], travel_agent.GATEWAY_INFERENCE_MODEL_ID)
            self.assertNotEqual(model.config["model_id"], travel_agent.MODEL_ID)
            # AnthropicModel stores client_args on its underlying client
            # rather than exposing them directly — confirm the Bearer
            # token and base_url actually reached the Anthropic client
            # instance via its httpx client's configured auth/base_url,
            # which is the only externally-observable proof they were
            # passed through correctly.
            anthropic_client = model.client
            self.assertEqual(str(anthropic_client.base_url).rstrip("/"), travel_agent.GATEWAY_INFERENCE_URL)
            self.assertEqual(
                anthropic_client.auth_token,
                "fake-gateway-jwt",
            )
            fake_get_token.assert_awaited_once_with(
                provider_name="travel-planning-agent-gateway-obo",
                scopes=[travel_agent.GATEWAY_OBO_SCOPE],
                audiences=[travel_agent.GATEWAY_OBO_AUDIENCE],
                agent_identity_token="fake-workload-token",
                auth_flow="ON_BEHALF_OF_TOKEN_EXCHANGE",
                custom_parameters={
                    "subject_token_type": travel_agent.GATEWAY_OBO_SUBJECT_TOKEN_TYPE
                },
            )

    def test_reuses_cached_obo_token_across_mcp_client_and_model(self):
        """Question 1 of this feature's design interview: build_model()
        and build_mcp_client() must share the same OBO token cache entry
        for the same caller, not perform two independent exchanges.
        Requires a real request-header sub claim to resolve a cache key
        at all — mirrors BuildMcpClientTests.test_obo_token_is_cached_per_sub_across_calls()'s
        exact setup for that reason."""
        travel_agent.GATEWAY_URL = "https://example-gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
        travel_agent.GATEWAY_INFERENCE_URL = (
            "https://example-gateway.gateway.bedrock-agentcore.us-east-1.amazonaws.com/inference"
        )
        travel_agent.GATEWAY_OBO_PROVIDER_NAME = "travel-planning-agent-gateway-obo"
        travel_agent.BedrockAgentCoreContext.set_workload_access_token("fake-workload-token")
        travel_agent.BedrockAgentCoreContext.set_request_headers(
            {"Authorization": f"Bearer {GetActorIdTests._fake_jwt('alice@example.com')}"}
        )

        fake_get_token = AsyncMock(return_value=BuildMcpClientTests._fake_gateway_jwt(3600))
        with patch.object(travel_agent, "IdentityClient") as fake_identity_client_cls, \
                patch.object(travel_agent, "streamablehttp_client"):
            fake_identity_client_cls.return_value.get_token = fake_get_token

            asyncio_run(travel_agent.build_mcp_client())
            asyncio_run(travel_agent.build_model())

            # Only the first call (build_mcp_client()) actually
            # exchanges — build_model()'s call is a cache hit.
            fake_get_token.assert_awaited_once()


class BuildSkillsPluginTests(unittest.TestCase):
    """Covers build_skills_plugin() — the AgentSkills plugin wiring for
    agent/skills/. Exercises the real Strands AgentSkills/Agent classes
    (no fakes) since the behavior under test is genuine Strands plugin
    machinery (skill discovery/validation, system-prompt injection, the
    "skills" activation tool) — not agent.py's own logic, which is limited
    to pointing the plugin at SKILLS_DIR.
    """

    def test_skills_dir_points_at_agent_skills_directory(self):
        self.assertEqual(
            travel_agent.SKILLS_DIR,
            str(_AGENT_DIR / "skills"),
        )

    def test_loads_trip_pacing_skill(self):
        from strands import Agent

        plugin = travel_agent.build_skills_plugin()
        agent = Agent(system_prompt="test", plugins=[plugin], callback_handler=None)

        loaded = asyncio_run(_skills_for(plugin, agent))

        self.assertIn("trip-pacing", loaded)
        self.assertIn("day-by-day itinerary", loaded["trip-pacing"].description)

    def test_injects_skill_metadata_into_system_prompt(self):
        from strands import Agent
        from strands.hooks.events import BeforeInvocationEvent

        plugin = travel_agent.build_skills_plugin()
        agent = Agent(system_prompt="Base prompt.", plugins=[plugin], callback_handler=None)

        asyncio_run(plugin._on_before_invocation(BeforeInvocationEvent(agent=agent)))

        self.assertIn("Base prompt.", agent.system_prompt)
        self.assertIn("<name>trip-pacing</name>", agent.system_prompt)

    def test_activating_skill_returns_full_instructions(self):
        from strands import Agent
        from strands.hooks.events import BeforeInvocationEvent

        plugin = travel_agent.build_skills_plugin()
        agent = Agent(system_prompt="Base prompt.", plugins=[plugin], callback_handler=None)
        asyncio_run(plugin._on_before_invocation(BeforeInvocationEvent(agent=agent)))

        tool_context = type("_FakeToolContext", (), {"agent": agent})()
        result = asyncio_run(plugin.skills(skill_name="trip-pacing", tool_context=tool_context))

        self.assertIn("Cap outdoor activity count per day", result)


def _skills_for(plugin, agent):
    """Load an AgentSkills plugin's filesystem skills for `agent` and
    return the resulting {name: Skill} map — mirrors what the plugin does
    internally in init_agent()/_on_before_invocation(), without depending
    on the plugin's private per-agent cache attribute name directly.
    """

    async def _load():
        await plugin._load_skill_paths(agent)
        return plugin._skills_for(agent)

    return _load()


if __name__ == "__main__":
    unittest.main()
