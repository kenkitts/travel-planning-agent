"""Unit tests for web/server.py.

All calls to the AgentCore Runtime are mocked (via agent_client.invoke_agent)
— no live network access, no real AWS credentials required.
"""
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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


class ChatEndpointTests(unittest.TestCase):
    def setUp(self):
        # boto3.client() is created inside create_app(); patch it so no
        # real AWS client/credentials are needed.
        patcher = patch("web_server.boto3.client")
        self.mock_boto_client_factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_boto_client_factory.return_value = MagicMock()

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
        self.assertEqual(response.json(), {"actor_id": "web-user"})

    def test_index_serves_html(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Travel Planning Agent", response.text)


if __name__ == "__main__":
    unittest.main()
