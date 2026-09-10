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
from types import SimpleNamespace
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
        # stream_agent_turn() registers its routing-observability hook
        # unconditionally (see agent.py) — this fixture only needs a
        # working hooks.add_callback() no-op, not a real ModelRouter, for
        # every test that isn't specifically about routing observability.
        # RoutingObservabilityTests below builds a real ModelRouter/Agent
        # pair instead of using this fixture.
        self.hooks = SimpleNamespace(add_callback=lambda *_a, **_kw: None)

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


class BedrockModelIdForGatewayTests(unittest.TestCase):
    """Covers _bedrock_model_id_for_gateway()'s explicit per-model mapping
    directly — see that function's own docstring for why this must be an
    explicit mapping, not a shared string transformation (Sonnet 5's
    bedrock-mantle ID happens to equal its bare bedrock-runtime ID; Haiku
    4.5's is a shorter, distinct alias with the date/version suffix
    dropped entirely, found live after a real 400/403 in production).
    """

    def test_maps_sonnet_five_us_prefixed_id(self):
        self.assertEqual(
            travel_agent._bedrock_model_id_for_gateway("us.anthropic.claude-sonnet-5"),
            "anthropic.claude-sonnet-5",
        )

    def test_maps_sonnet_five_bare_id(self):
        self.assertEqual(
            travel_agent._bedrock_model_id_for_gateway("anthropic.claude-sonnet-5"),
            "anthropic.claude-sonnet-5",
        )

    def test_maps_haiku_four_five_us_prefixed_id_to_shorter_alias(self):
        """The real bug: Haiku 4.5's bedrock-mantle ID drops the
        "-20251001-v1:0" suffix entirely -- it is not a substring of the
        bedrock-runtime-shaped ID, unlike Sonnet 5's case above."""
        self.assertEqual(
            travel_agent._bedrock_model_id_for_gateway(
                "us.anthropic.claude-haiku-4-5-20251001-v1:0"
            ),
            "anthropic.claude-haiku-4-5",
        )

    def test_maps_haiku_four_five_bare_id_to_shorter_alias(self):
        self.assertEqual(
            travel_agent._bedrock_model_id_for_gateway(
                "anthropic.claude-haiku-4-5-20251001-v1:0"
            ),
            "anthropic.claude-haiku-4-5",
        )

    def test_raises_for_unknown_model_id(self):
        """Fails loudly for an unmapped model ID rather than guessing a
        transformation — a wrong guess here fails the same way the real
        Haiku bug did (a live 400/403 from bedrock-mantle), just later
        and less clearly."""
        with self.assertRaises(ValueError):
            travel_agent._bedrock_model_id_for_gateway("anthropic.claude-opus-4-8")


class BuildModelRouterTests(unittest.TestCase):
    """Covers build_model_router()'s construction-time wiring — the tiered
    ModelRouter/ClassifierStrategy path that replaced the single-model
    build_model() (see DESIGN.md's model-routing decision). Mirrors
    BuildMcpClientTests' setUp/tearDown/patching conventions, since both
    functions read the same module-level OBO config and token cache.

    Per this feature's own design interview (construction-time wiring
    only, no simulated routing-decision tests): asserts on how the 3
    AnthropicModel instances (2 serving candidates + 1 classifier) were
    constructed — correct model IDs, the shared token, classifier config
    — never on which candidate ClassifierStrategy would actually pick for
    a given message. That judgment call is validated via live testing
    only, not unit tests (see PLAN.md's phase write-up for this feature).
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

    def test_raises_when_gateway_inference_url_unset(self):
        """No fallback path exists once GATEWAY_INFERENCE_URL is unset —
        this is a deploy-time misconfiguration (RuntimeStack always wires
        it from GatewayStack's inference target), distinct from the
        missing-workload-token case below (a Runtime-auth-mode
        misconfiguration), so each gets its own explicit test."""
        travel_agent.GATEWAY_INFERENCE_URL = ""

        with self.assertRaises(RuntimeError):
            asyncio_run(travel_agent.build_model_router())

    def test_raises_when_inference_url_set_but_no_workload_token(self):
        travel_agent.GATEWAY_INFERENCE_URL = "https://example-gateway.gateway.bedrock-agentcore.us-east-1.amazonaws.com/inference"
        travel_agent.BedrockAgentCoreContext.set_workload_access_token("")

        with self.assertRaises(RuntimeError):
            asyncio_run(travel_agent.build_model_router())

    def _build_router_with_fake_token(self, fake_token="fake-gateway-jwt"):
        """Shared setup: build a real ModelRouter with a mocked OBO exchange."""
        travel_agent.GATEWAY_INFERENCE_URL = (
            "https://example-gateway.gateway.bedrock-agentcore.us-east-1.amazonaws.com/inference"
        )
        travel_agent.GATEWAY_OBO_PROVIDER_NAME = "travel-planning-agent-gateway-obo"
        travel_agent.BedrockAgentCoreContext.set_workload_access_token("fake-workload-token")

        fake_get_token = AsyncMock(return_value=fake_token)
        patcher = patch.object(travel_agent, "IdentityClient")
        fake_identity_client_cls = patcher.start()
        self.addCleanup(patcher.stop)
        fake_identity_client_cls.return_value.get_token = fake_get_token
        router = asyncio_run(travel_agent.build_model_router())
        return router, fake_get_token

    def test_returns_model_router_with_two_serving_candidates(self):
        router, _ = self._build_router_with_fake_token()

        self.assertIsInstance(router, travel_agent.ModelRouter)
        self.assertEqual(len(router.candidates), 2)
        names = {candidate.name for candidate in router.candidates}
        self.assertEqual(names, {"capable", "cheap"})

    def test_capable_candidate_is_declared_first_as_router_default(self):
        """The capable (Sonnet) candidate is router.candidates[0] — the
        router's own default when a strategy declines — so a classifier
        failure fails toward the safer, more capable option rather than
        silently under-serving a request."""
        router, _ = self._build_router_with_fake_token()

        self.assertEqual(router.candidates[0].name, "capable")
        self.assertEqual(router.candidates[1].name, "cheap")

    def test_serving_candidates_use_correct_bare_model_ids(self):
        """Gateway-routed calls need each model's bedrock-mantle-specific
        ID, not the bedrock-runtime-shaped MODEL_ID/HAIKU_MODEL_ID values —
        found live (see _bedrock_model_id_for_gateway()'s docstring).
        Asserts the real, AWS-documented mapped values directly (not just
        "whatever _bedrock_model_id_for_gateway() itself returns") so a
        future accidental change to that function's mapping is actually
        caught, not just confirmed self-consistent."""
        router, _ = self._build_router_with_fake_token()

        expected_capable_id = travel_agent._bedrock_model_id_for_gateway(travel_agent.MODEL_ID)
        expected_cheap_id = travel_agent._bedrock_model_id_for_gateway(travel_agent.HAIKU_MODEL_ID)
        self.assertNotEqual(expected_capable_id, travel_agent.MODEL_ID)
        self.assertNotEqual(expected_cheap_id, travel_agent.HAIKU_MODEL_ID)
        # Real values confirmed against AWS's own per-model "Programmatic
        # Access" documentation table (bedrock-mantle row) — Sonnet 5's
        # bedrock-mantle ID happens to equal its bare bedrock-runtime ID;
        # Haiku 4.5's is a shorter, distinct alias with the date/version
        # suffix dropped entirely, not a substring of its bedrock-runtime ID.
        self.assertEqual(expected_capable_id, "anthropic.claude-sonnet-5")
        self.assertEqual(expected_cheap_id, "anthropic.claude-haiku-4-5")

        by_name = {candidate.name: candidate.model for candidate in router.candidates}
        self.assertEqual(by_name["capable"].config["model_id"], expected_capable_id)
        self.assertEqual(by_name["cheap"].config["model_id"], expected_cheap_id)

    def test_serving_candidates_share_the_gateway_token_and_url(self):
        router, fake_get_token = self._build_router_with_fake_token(fake_token="fake-gateway-jwt")

        for candidate in router.candidates:
            anthropic_client = candidate.model.client
            self.assertEqual(
                str(anthropic_client.base_url).rstrip("/"), travel_agent.GATEWAY_INFERENCE_URL
            )
            self.assertEqual(anthropic_client.auth_token, "fake-gateway-jwt")

        # One token authenticates both serving candidates AND the
        # classifier (3 model roles total) — only one exchange call, not
        # three, since the token is fetched once in build_model_router()
        # and reused for every construction (see that function's own
        # docstring on why this is correct, not just an optimization).
        fake_get_token.assert_awaited_once()

    def test_classifier_uses_cheap_model_and_small_max_tokens(self):
        """The classifier reuses HAIKU_MODEL_ID (matching Strands' own
        docs example of a small/cheap/deterministic classifier model —
        not a third, separate model) with max_tokens=64, since its only
        job is a one-field structured-output decision, not real
        generation."""
        router, _ = self._build_router_with_fake_token()

        strategy = router._strategy
        self.assertIsInstance(strategy, travel_agent.ClassifierStrategy)
        classifier_model = strategy._model
        expected_classifier_id = travel_agent._bedrock_model_id_for_gateway(
            travel_agent.HAIKU_MODEL_ID
        )
        self.assertEqual(classifier_model.config["model_id"], expected_classifier_id)
        self.assertEqual(classifier_model.config["max_tokens"], 64)

    def test_haiku_candidates_do_not_enable_adaptive_thinking(self):
        """The real live bug this feature hit: Claude Haiku 4.5 does not
        support adaptive/extended thinking at all, and Anthropic's API
        correctly 400s any request carrying the "thinking" param for it
        ("adaptive thinking is not supported on this model") -- this
        broke both the cheap serving candidate (whenever actually
        selected to serve a turn) and the classifier (every single turn,
        since it always runs first), confirmed via a temporary diagnostic
        that called classifier_model.structured_output() directly and
        logged the real exception body. Neither the cheap serving
        candidate nor the classifier model must have a "thinking" param;
        only the capable (Sonnet) candidate, which does support it."""
        router, _ = self._build_router_with_fake_token()

        by_name = {candidate.name: candidate.model for candidate in router.candidates}
        cheap_model = by_name["cheap"]
        capable_model = by_name["capable"]
        classifier_model = router._strategy._model

        self.assertIsNone(cheap_model.config.get("params"))
        self.assertIsNone(classifier_model.config.get("params"))
        self.assertIsNotNone(capable_model.config.get("params"))
        self.assertIn("thinking", capable_model.config["params"])

    def test_classifier_uses_this_agents_routing_policy_system_prompt(self):
        router, _ = self._build_router_with_fake_token()

        strategy = router._strategy
        self.assertEqual(strategy._system_prompt, travel_agent._ROUTING_POLICY_SYSTEM_PROMPT)

    def test_classifier_and_serving_candidates_are_distinct_model_instances(self):
        """ModelRouter itself rejects duplicate model instances across
        candidates (see its own construction guards) — this test would
        have failed at construction time with a ValueError if the cheap
        serving candidate and the classifier accidentally shared one
        AnthropicModel object instead of two separate ones."""
        router, _ = self._build_router_with_fake_token()

        cheap_model = next(c.model for c in router.candidates if c.name == "cheap")
        classifier_model = router._strategy._model
        self.assertIsNot(cheap_model, classifier_model)

    def test_reuses_cached_obo_token_across_mcp_client_and_router(self):
        """This feature's design interview: build_model_router() and
        build_mcp_client() must share the same OBO token cache entry for
        the same caller, not perform two independent exchanges. Requires
        a real request-header sub claim to resolve a cache key at all —
        mirrors BuildMcpClientTests.test_obo_token_is_cached_per_sub_across_calls()'s
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
            asyncio_run(travel_agent.build_model_router())

            # Only the first call (build_mcp_client()) actually
            # exchanges — build_model_router()'s call (all 3 model roles)
            # is a cache hit.
            fake_get_token.assert_awaited_once()


class RoutingObservabilityTests(unittest.TestCase):
    """Covers _selected_candidate_name() and stream_agent_turn()'s
    "routing" SSE event — the observability half of this feature's
    design (a CloudWatch log line plus a diagnostic-panel event
    surfacing which ModelRouter candidate served each turn). Uses a
    REAL ModelRouter/Agent pair (no fakes), since the behavior under
    test is reading Strands' own internal per-invocation routing state,
    which only a real router populates correctly.
    """

    @staticmethod
    def _fake_anthropic_model(label):
        """A minimal stateless Model double — real AnthropicModel
        construction needs a live client, which isn't needed here since
        only the router/hook machinery is under test, not any actual
        Anthropic API shape. Subclasses strands.models.model.Model
        directly (its real abstract base) rather than AnthropicModel
        itself."""
        from strands.models.model import Model as _StrandsModel

        class _FakeModel(_StrandsModel):
            def __init__(self):
                self._config = {"model_id": label}

            def update_config(self, **kwargs):
                self._config.update(kwargs)

            def get_config(self):
                return self._config

            async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
                yield {"output": output_model(selected_candidate_index=0)}

            async def stream(self, *_args, **_kwargs):
                yield {"messageStart": {"role": "assistant"}}
                yield {"messageStop": {"stopReason": "end_turn"}}

        return _FakeModel()

    def test_selected_candidate_name_reads_real_router_state(self):
        """End-to-end through a real strands.Agent(model=router) turn —
        confirms the private invocation_state read in
        _selected_candidate_name() actually reflects ModelRouter's real
        selection against the installed strands-agents version, not just
        an assumption about its shape."""
        from strands import Agent
        from strands.hooks import AfterModelCallEvent

        cheap = self._fake_anthropic_model("cheap-model")
        capable = self._fake_anthropic_model("capable-model")
        router = travel_agent.ModelRouter(
            models=[
                travel_agent.RoutingCandidate(capable, name="capable"),
                travel_agent.RoutingCandidate(cheap, name="cheap"),
            ],
        )

        seen_names = []

        async def _capture(event: AfterModelCallEvent) -> None:
            name = travel_agent._selected_candidate_name(event)
            if name is not None:
                seen_names.append(name)

        agent = Agent(model=router, callback_handler=None)
        agent.hooks.add_callback(AfterModelCallEvent, _capture)

        asyncio_run(agent.invoke_async("hello"))

        # FallbackStrategy (the router's default here, since no
        # ClassifierStrategy is configured) opens on the first-declared
        # candidate — "capable" — so this confirms the read reflects a
        # real selection, not a hardcoded/fallback string.
        self.assertIn("capable", seen_names)

    def test_selected_candidate_name_returns_none_for_unrelated_event(self):
        """A plain object with no matching invocation_state key (e.g. a
        non-routed agent's AfterModelCallEvent) must degrade to None, not
        raise — this is a purely observational feature."""
        fake_event = SimpleNamespace(invocation_state={"unrelated-key": object()})

        self.assertIsNone(travel_agent._selected_candidate_name(fake_event))

    def test_stream_agent_turn_emits_routing_event_for_routed_agent(self):
        """stream_agent_turn() must emit exactly one "routing" event when
        the agent is actually routed through a real ModelRouter. A
        non-routed _FakeAgent (no ModelRouter at all) never emits one,
        per StreamAgentTurnTests' own existing coverage — not because of
        an isinstance(agent.model, ModelRouter) check (which would never
        be true even for a genuinely routed agent — see this function's
        own docstring), but because _selected_candidate_name() finds no
        matching routing state to report."""
        from strands import Agent

        cheap = self._fake_anthropic_model("cheap-model")
        capable = self._fake_anthropic_model("capable-model")
        router = travel_agent.ModelRouter(
            models=[
                travel_agent.RoutingCandidate(capable, name="capable"),
                travel_agent.RoutingCandidate(cheap, name="cheap"),
            ],
        )
        agent = Agent(model=router, callback_handler=None)

        events = asyncio_run(_collect(travel_agent.stream_agent_turn(agent, "hello")))

        routing_events = [event for event in events if event["type"] == "routing"]
        self.assertEqual(len(routing_events), 1)
        self.assertEqual(routing_events[0]["data"]["candidate"], "capable")


async def _collect(async_gen):
    return [event async for event in async_gen]



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


class BuildCodeInterpreterToolTests(unittest.TestCase):
    """Covers build_code_interpreter_tool() — the AgentCore Code Interpreter
    tool wiring (see prompts.py step 5 and DESIGN.md for the "compute, don't
    guess" rationale). Only constructs the tool object and inspects its
    Strands-generated metadata; does not exercise a real code-execution
    call, which would need live AWS credentials and a running sandbox.
    """

    def test_builds_a_code_interpreter_tool_named_code_interpreter(self):
        tool = travel_agent.build_code_interpreter_tool()

        self.assertEqual(tool.tool_spec["name"], "code_interpreter")

    def test_uses_the_aws_managed_sandbox_identifier(self):
        from strands_tools.code_interpreter import AgentCoreCodeInterpreter

        interpreter = AgentCoreCodeInterpreter(region=travel_agent.AWS_REGION)

        # The AWS-managed sandbox identifier — not a CDK-provisioned
        # CodeInterpreterCustom resource. Confirms build_code_interpreter_tool()
        # doesn't need (and doesn't rely on) a custom identifier being passed,
        # matching the IAM policy granted in runtime_stack.py, which covers
        # both this account's code-interpreter/* and the :aws: managed one.
        self.assertEqual(interpreter.identifier, "aws.codeinterpreter.v1")


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
