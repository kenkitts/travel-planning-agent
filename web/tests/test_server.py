"""Unit tests for web/server.py.

All calls to the AgentCore Runtime are mocked (via agent_client.invoke_agent)
— no live network access, no real AWS credentials required.
"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

_WEB_DIR = Path(__file__).resolve().parents[1]
_CLI_DIR = _WEB_DIR.parent / "cli"
sys.path.insert(0, str(_CLI_DIR))

_SERVER_PATH = _WEB_DIR / "server.py"
_spec = importlib.util.spec_from_file_location("web_server", _SERVER_PATH)
web_server = importlib.util.module_from_spec(_spec)
sys.modules["web_server"] = web_server
_spec.loader.exec_module(web_server)

# A syntactically valid 33+ character runtimeSessionId, matching what the
# frontend generates via build_runtime_session_id()-equivalent JS logic.
VALID_SESSION_ID = "web-user___" + "a" * 32
# The bare component AgentCore Memory's ListSessions/ListEvents actually
# use — confirmed live against a real Memory resource: these APIs are
# already actor-scoped and return/accept only the part after "___", not
# the full runtimeSessionId.
BARE_SESSION_ID = "a" * 32


def _resource_not_found_error(operation_name: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
        operation_name,
    )


class ChatEndpointTests(unittest.TestCase):
    def setUp(self):
        # boto3.client() is created inside create_app(); patch it so no
        # real AWS client/credentials are needed.
        patcher = patch("web_server.boto3.client")
        self.mock_boto_client_factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_client = MagicMock()
        self.mock_boto_client_factory.return_value = self.mock_client

        self.app = web_server.create_app(
            agent_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
            region="us-east-1",
            actor_id="web-user",
        )
        self.client = TestClient(self.app)

    @patch("web_server.invoke_agent")
    def test_chat_returns_agent_response(self, mock_invoke_agent):
        mock_invoke_agent.return_value = "Here's a 2-day Boston itinerary..."

        response = self.client.post(
            "/api/chat",
            json={"prompt": "Plan a trip to Boston", "session_id": VALID_SESSION_ID},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"response": "Here's a 2-day Boston itinerary..."}
        )
        mock_invoke_agent.assert_called_once()
        call_args = mock_invoke_agent.call_args.args
        # invoke_agent(client, agent_runtime_arn, runtime_session_id, prompt, qualifier)
        self.assertEqual(call_args[1], "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test")
        self.assertEqual(call_args[2], VALID_SESSION_ID)
        self.assertEqual(call_args[3], "Plan a trip to Boston")

    @patch("web_server.invoke_agent")
    def test_chat_strips_whitespace_from_prompt(self, mock_invoke_agent):
        mock_invoke_agent.return_value = "ok"

        self.client.post(
            "/api/chat",
            json={"prompt": "  Plan a trip  ", "session_id": VALID_SESSION_ID},
        )

        call_args = mock_invoke_agent.call_args.args
        self.assertEqual(call_args[3], "Plan a trip")

    def test_chat_rejects_empty_prompt(self):
        response = self.client.post(
            "/api/chat",
            json={"prompt": "   ", "session_id": VALID_SESSION_ID},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("prompt must not be empty", response.json()["detail"])

    def test_chat_rejects_short_session_id(self):
        response = self.client.post(
            "/api/chat",
            json={"prompt": "hello", "session_id": "too-short"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("33 characters", response.json()["detail"])

    @patch("web_server.invoke_agent")
    def test_chat_propagates_agent_error_as_502(self, mock_invoke_agent):
        mock_invoke_agent.side_effect = RuntimeError("Agent returned an error: boom")

        response = self.client.post(
            "/api/chat",
            json={"prompt": "hello", "session_id": VALID_SESSION_ID},
        )

        self.assertEqual(response.status_code, 502)
        self.assertIn("boom", response.json()["detail"])

    def test_config_endpoint_returns_actor_id(self):
        response = self.client.get("/api/config")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"actor_id": "web-user", "history_enabled": False}
        )

    def test_index_serves_html(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Travel Planning Agent", response.text)


def _conversational_event(role: str, text: str, timestamp=None) -> dict:
    """Build a fake AgentCore Memory event as returned by list_events.

    Encodes `text` the way AgentCoreMemorySessionManager actually stores
    it — a JSON-encoded Strands SessionMessage dump, confirmed against a
    real event via a live ListEvents call — rather than a bare string, so
    these tests exercise the same parsing path production data takes.
    """
    wrapped_text = json.dumps({"message": {"role": role, "content": [{"text": text}]}})
    event = {
        "payload": [{"conversational": {"role": role, "content": {"text": wrapped_text}}}]
    }
    if timestamp is not None:
        event["eventTimestamp"] = timestamp
    return event


def _title_marker_event(title: str) -> dict:
    """Build a fake title-marker event, as written by set_conversation_title."""
    return {
        "payload": [
            {
                "conversational": {
                    "role": "USER",
                    "content": {"text": f"Conversation renamed to “{title}”"},
                }
            }
        ],
        "metadata": {"conversationTitle": {"stringValue": title}},
    }


class ConversationHistoryEndpointTests(unittest.TestCase):
    """Tests for GET /api/conversations and GET /api/conversations/{session_id}."""

    def setUp(self):
        patcher = patch("web_server.boto3.client")
        self.mock_boto_client_factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_client = MagicMock()
        self.mock_boto_client_factory.return_value = self.mock_client

        self.app = web_server.create_app(
            agent_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
            region="us-east-1",
            actor_id="web-user",
            memory_id="mem-123",
        )
        self.client = TestClient(self.app)

    def test_config_reports_history_enabled_when_memory_id_set(self):
        response = self.client.get("/api/config")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["history_enabled"])

    def test_list_conversations_returns_sessions_with_preview(self):
        self.mock_client.list_sessions.return_value = {
            "sessionSummaries": [
                {"sessionId": BARE_SESSION_ID, "createdAt": None},
            ]
        }
        self.mock_client.list_events.return_value = {
            "events": [
                # Newest-first, as the real API returns; the second turn
                # (oldest) is the one used for the preview.
                _conversational_event("ASSISTANT", "Sure, tell me more."),
                _conversational_event("USER", "Plan a trip to Boston"),
            ]
        }

        response = self.client.get("/api/conversations")

        self.assertEqual(response.status_code, 200)
        conversations = response.json()
        self.assertEqual(len(conversations), 1)
        # The response must be the full runtimeSessionId ("actorId___bare"),
        # not the bare component ListSessions returns — /api/chat and the
        # frontend's InvokeAgentRuntime path both require the full form.
        self.assertEqual(conversations[0]["session_id"], VALID_SESSION_ID)
        self.assertEqual(conversations[0]["preview"], "Plan a trip to Boston")

        self.mock_client.list_sessions.assert_called_once_with(
            memoryId="mem-123", actorId="web-user", maxResults=100
        )
        self.mock_client.list_events.assert_called_once_with(
            memoryId="mem-123",
            actorId="web-user",
            sessionId=BARE_SESSION_ID,
            maxResults=100,
        )

    def test_list_conversations_returns_empty_for_actor_with_no_sessions(self):
        # Confirmed live against a real Memory resource: ListSessions
        # raises ResourceNotFoundException for a brand-new actor rather
        # than returning an empty sessionSummaries list.
        self.mock_client.list_sessions.side_effect = _resource_not_found_error(
            "ListSessions"
        )

        response = self.client.get("/api/conversations")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_list_conversations_skips_sessions_with_no_events(self):
        self.mock_client.list_sessions.return_value = {
            "sessionSummaries": [{"sessionId": BARE_SESSION_ID, "createdAt": None}]
        }
        self.mock_client.list_events.return_value = {"events": []}

        response = self.client.get("/api/conversations")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_list_conversations_truncates_long_preview(self):
        long_text = "Plan an extremely detailed multi-week trip " * 3
        self.mock_client.list_sessions.return_value = {
            "sessionSummaries": [{"sessionId": BARE_SESSION_ID, "createdAt": None}]
        }
        self.mock_client.list_events.return_value = {
            "events": [_conversational_event("USER", long_text)]
        }

        response = self.client.get("/api/conversations")

        preview = response.json()[0]["preview"]
        self.assertLessEqual(len(preview), 81)  # PREVIEW_MAX_CHARS + ellipsis
        self.assertTrue(preview.endswith("…"))

    def test_list_conversations_returns_404_when_memory_id_not_configured(self):
        app = web_server.create_app(
            agent_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
            region="us-east-1",
            actor_id="web-user",
        )
        client = TestClient(app)

        response = client.get("/api/conversations")

        self.assertEqual(response.status_code, 404)

    def test_get_conversation_returns_chronological_transcript(self):
        self.mock_client.list_events.return_value = {
            "events": [
                # Newest-first from the API; response should be chronological.
                _conversational_event("ASSISTANT", "Here's your itinerary."),
                _conversational_event("USER", "Plan a trip to Boston"),
            ]
        }

        response = self.client.get(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["session_id"], VALID_SESSION_ID)
        self.assertEqual(
            data["turns"],
            [
                {"role": "user", "text": "Plan a trip to Boston"},
                {"role": "assistant", "text": "Here's your itinerary."},
            ],
        )
        # The full runtimeSessionId in the URL must be stripped to the bare
        # component before calling ListEvents.
        self.mock_client.list_events.assert_called_once_with(
            memoryId="mem-123",
            actorId="web-user",
            sessionId=BARE_SESSION_ID,
            maxResults=100,
        )

    def test_get_conversation_falls_back_to_raw_text_for_unwrapped_events(self):
        # An event whose content.text isn't the JSON SessionMessage wrapper
        # (e.g. an older or different event format) should still render,
        # using the raw string as-is rather than dropping the turn.
        self.mock_client.list_events.return_value = {
            "events": [
                {
                    "payload": [
                        {
                            "conversational": {
                                "role": "USER",
                                "content": {"text": "plain unwrapped text"},
                            }
                        }
                    ]
                }
            ]
        }

        response = self.client.get(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["turns"],
            [{"role": "user", "text": "plain unwrapped text"}],
        )

    def test_get_conversation_skips_blob_payload_items(self):
        # "blob" items are Strands session/state snapshots, not
        # conversation turns, and must not appear in the transcript.
        self.mock_client.list_events.return_value = {
            "events": [
                {"payload": [{"blob": '{"agent_id": "default", "state": {}}'}]},
                _conversational_event("USER", "Plan a trip to Boston"),
            ]
        }

        response = self.client.get(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["turns"],
            [{"role": "user", "text": "Plan a trip to Boston"}],
        )

    def test_get_conversation_returns_404_for_unknown_session(self):
        self.mock_client.list_events.return_value = {"events": []}

        response = self.client.get(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 404)

    def test_get_conversation_returns_404_for_resource_not_found_error(self):
        self.mock_client.list_events.side_effect = _resource_not_found_error(
            "ListEvents"
        )

        response = self.client.get(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 404)

    def test_get_conversation_returns_404_when_memory_id_not_configured(self):
        app = web_server.create_app(
            agent_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
            region="us-east-1",
            actor_id="web-user",
        )
        client = TestClient(app)

        response = client.get(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 404)

    def test_get_conversation_returns_502_on_client_error(self):
        self.mock_client.list_events.side_effect = RuntimeError("boom")

        response = self.client.get(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 502)
        self.assertIn("boom", response.json()["detail"])

    def test_list_conversations_prefers_title_over_preview(self):
        self.mock_client.list_sessions.return_value = {
            "sessionSummaries": [{"sessionId": BARE_SESSION_ID, "createdAt": None}]
        }
        self.mock_client.list_events.return_value = {
            "events": [
                # Newest-first: the marker (latest) comes before the turns.
                _title_marker_event("Seattle Coffee Trip"),
                _conversational_event("USER", "Plan a trip to Seattle"),
            ]
        }

        response = self.client.get("/api/conversations")

        conversations = response.json()
        self.assertEqual(conversations[0]["title"], "Seattle Coffee Trip")
        self.assertEqual(conversations[0]["preview"], "Plan a trip to Seattle")

    def test_list_conversations_title_is_none_when_never_renamed(self):
        self.mock_client.list_sessions.return_value = {
            "sessionSummaries": [{"sessionId": BARE_SESSION_ID, "createdAt": None}]
        }
        self.mock_client.list_events.return_value = {
            "events": [_conversational_event("USER", "Plan a trip to Seattle")]
        }

        response = self.client.get("/api/conversations")

        self.assertIsNone(response.json()[0]["title"])

    def test_list_conversations_uses_latest_title_when_renamed_twice(self):
        self.mock_client.list_sessions.return_value = {
            "sessionSummaries": [{"sessionId": BARE_SESSION_ID, "createdAt": None}]
        }
        self.mock_client.list_events.return_value = {
            # Newest-first: the most recent rename must win.
            "events": [
                _title_marker_event("Seattle Trip v2"),
                _title_marker_event("Seattle Trip v1"),
                _conversational_event("USER", "Plan a trip to Seattle"),
            ]
        }

        response = self.client.get("/api/conversations")

        self.assertEqual(response.json()[0]["title"], "Seattle Trip v2")

    def test_get_conversation_excludes_title_marker_from_transcript(self):
        self.mock_client.list_events.return_value = {
            "events": [
                _title_marker_event("Seattle Coffee Trip"),
                _conversational_event("ASSISTANT", "Here's your itinerary."),
                _conversational_event("USER", "Plan a trip to Seattle"),
            ]
        }

        response = self.client.get(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(
            response.json()["turns"],
            [
                {"role": "user", "text": "Plan a trip to Seattle"},
                {"role": "assistant", "text": "Here's your itinerary."},
            ],
        )

    def test_set_title_writes_marker_event_with_metadata(self):
        self.mock_client.create_event.return_value = {"event": {"eventId": "e1"}}

        response = self.client.put(
            f"/api/conversations/{VALID_SESSION_ID}/title",
            json={"title": "Seattle Coffee Trip"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"session_id": VALID_SESSION_ID, "title": "Seattle Coffee Trip"}
        )

        self.mock_client.create_event.assert_called_once()
        call_kwargs = self.mock_client.create_event.call_args.kwargs
        self.assertEqual(call_kwargs["memoryId"], "mem-123")
        self.assertEqual(call_kwargs["actorId"], "web-user")
        # The full runtimeSessionId in the URL must be stripped to the bare
        # component before calling CreateEvent, same as the other endpoints.
        self.assertEqual(call_kwargs["sessionId"], BARE_SESSION_ID)
        self.assertEqual(
            call_kwargs["metadata"],
            {"conversationTitle": {"stringValue": "Seattle Coffee Trip"}},
        )
        self.assertEqual(call_kwargs["extractionMode"], "SKIP")

    def test_set_title_normalizes_whitespace(self):
        self.mock_client.create_event.return_value = {"event": {"eventId": "e1"}}

        response = self.client.put(
            f"/api/conversations/{VALID_SESSION_ID}/title",
            json={"title": "  Seattle   Coffee\nTrip  "},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "Seattle Coffee Trip")

    def test_set_title_rejects_empty_title(self):
        response = self.client.put(
            f"/api/conversations/{VALID_SESSION_ID}/title",
            json={"title": "   "},
        )

        self.assertEqual(response.status_code, 400)
        self.mock_client.create_event.assert_not_called()

    def test_set_title_truncates_long_title(self):
        self.mock_client.create_event.return_value = {"event": {"eventId": "e1"}}
        long_title = "A" * 200

        response = self.client.put(
            f"/api/conversations/{VALID_SESSION_ID}/title",
            json={"title": long_title},
        )

        self.assertEqual(response.status_code, 200)
        returned_title = response.json()["title"]
        self.assertLessEqual(len(returned_title), 80)
        self.assertTrue(returned_title.endswith("…"))

    def test_set_title_returns_404_when_memory_id_not_configured(self):
        app = web_server.create_app(
            agent_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
            region="us-east-1",
            actor_id="web-user",
        )
        client = TestClient(app)

        response = client.put(
            f"/api/conversations/{VALID_SESSION_ID}/title",
            json={"title": "New Title"},
        )

        self.assertEqual(response.status_code, 404)

    def test_set_title_returns_502_on_client_error(self):
        self.mock_client.create_event.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "CreateEvent",
        )

        response = self.client.put(
            f"/api/conversations/{VALID_SESSION_ID}/title",
            json={"title": "New Title"},
        )

        self.assertEqual(response.status_code, 502)

    def test_delete_conversation_deletes_all_events(self):
        self.mock_client.list_events.return_value = {
            "events": [{"eventId": "e1"}, {"eventId": "e2"}, {"eventId": "e3"}]
        }
        self.mock_client.delete_event.return_value = {}

        response = self.client.delete(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"session_id": VALID_SESSION_ID, "deleted_events": 3, "failed_events": 0},
        )
        self.assertEqual(self.mock_client.delete_event.call_count, 3)
        for call in self.mock_client.delete_event.call_args_list:
            self.assertEqual(call.kwargs["memoryId"], "mem-123")
            self.assertEqual(call.kwargs["actorId"], "web-user")
            # The full runtimeSessionId in the URL must be stripped to the
            # bare component, same as the other endpoints.
            self.assertEqual(call.kwargs["sessionId"], BARE_SESSION_ID)

        self.mock_client.list_events.assert_called_once_with(
            memoryId="mem-123",
            actorId="web-user",
            sessionId=BARE_SESSION_ID,
            maxResults=100,
            includePayloads=False,
        )

    def test_delete_conversation_paginates_through_all_events(self):
        self.mock_client.list_events.side_effect = [
            {"events": [{"eventId": "e1"}], "nextToken": "page2"},
            {"events": [{"eventId": "e2"}]},
        ]
        self.mock_client.delete_event.return_value = {}

        response = self.client.delete(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted_events"], 2)
        self.assertEqual(self.mock_client.list_events.call_count, 2)
        second_call_kwargs = self.mock_client.list_events.call_args_list[1].kwargs
        self.assertEqual(second_call_kwargs["nextToken"], "page2")

    def test_delete_conversation_reports_partial_failure(self):
        self.mock_client.list_events.return_value = {
            "events": [{"eventId": "e1"}, {"eventId": "e2"}]
        }
        self.mock_client.delete_event.side_effect = [
            {},
            ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                "DeleteEvent",
            ),
        ]

        response = self.client.delete(f"/api/conversations/{VALID_SESSION_ID}")

        # Best-effort: a partial failure is still a 200 with the failure
        # count surfaced, not a 502 — the caller can see what happened and
        # retry, rather than the whole delete aborting on one bad event.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"session_id": VALID_SESSION_ID, "deleted_events": 1, "failed_events": 1},
        )

    def test_delete_conversation_returns_404_for_empty_session(self):
        self.mock_client.list_events.return_value = {"events": []}

        response = self.client.delete(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 404)
        self.mock_client.delete_event.assert_not_called()

    def test_delete_conversation_returns_404_for_resource_not_found_error(self):
        self.mock_client.list_events.side_effect = _resource_not_found_error(
            "ListEvents"
        )

        response = self.client.delete(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 404)

    def test_delete_conversation_returns_502_on_other_client_error(self):
        self.mock_client.list_events.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "ListEvents",
        )

        response = self.client.delete(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 502)

    def test_delete_conversation_returns_404_when_memory_id_not_configured(self):
        app = web_server.create_app(
            agent_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
            region="us-east-1",
            actor_id="web-user",
        )
        client = TestClient(app)

        response = client.delete(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
