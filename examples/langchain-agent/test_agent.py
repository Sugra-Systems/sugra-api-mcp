from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from agent import SUGRA_MCP_URL, build_connections, completed_discovery_flow, tool_call_sequence


class ConnectionTests(TestCase):
    @patch("agent.sys.executable", "/path/to/python")
    def test_stdio_connection_runs_installed_package(self) -> None:
        self.assertEqual(
            build_connections("stdio", "test-key"),
            {
                "sugra": {
                    "transport": "stdio",
                    "command": "/path/to/python",
                    "args": ["-m", "sugra_api_mcp"],
                    "env": {"SUGRA_API_KEY": "test-key"},
                }
            },
        )

    def test_http_connection_sends_bearer_token(self) -> None:
        self.assertEqual(
            build_connections("http", "test-key"),
            {
                "sugra": {
                    "transport": "http",
                    "url": SUGRA_MCP_URL,
                    "headers": {"Authorization": "Bearer test-key"},
                }
            },
        )

    def test_unknown_transport_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "stdio.*http"):
            build_connections("websocket", "test-key")


class FlowTests(TestCase):
    def test_extracts_tool_calls_and_accepts_required_order(self) -> None:
        messages = [
            SimpleNamespace(tool_calls=[{"name": "search_endpoints"}]),
            SimpleNamespace(tool_calls=[{"name": "describe_endpoint"}]),
            SimpleNamespace(tool_calls=[{"name": "call_endpoint"}]),
        ]
        sequence = tool_call_sequence(messages)

        self.assertEqual(sequence, list(("search_endpoints", "describe_endpoint", "call_endpoint")))
        self.assertTrue(completed_discovery_flow(sequence))

    def test_rejects_shortcut_or_out_of_order_flow(self) -> None:
        self.assertFalse(completed_discovery_flow(["fetch_data"]))
        self.assertFalse(
            completed_discovery_flow(["describe_endpoint", "search_endpoints", "call_endpoint"])
        )
