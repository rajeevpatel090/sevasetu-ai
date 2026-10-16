# -*- coding: utf-8 -*-
import os
import sys
import json
import unittest
from datetime import datetime
from cryptography.hazmat.primitives.asymmetric import rsa

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(BASE_DIR, "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import app as portal
import oauth
from mcp_auth import verify_mcp_token
from mcp_http import mcp_blueprint


class TestMCPHttpEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Configure test RSA key
        if "priv" not in oauth._KEY_CACHE:
            oauth._KEY_CACHE["kid"] = "mcp-test-kid"
            oauth._KEY_CACHE["priv"] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.flask_app = portal.app
        # Register blueprint if not already registered
        if "mcp" not in cls.flask_app.blueprints:
            cls.flask_app.register_blueprint(mcp_blueprint)
        cls.client = cls.flask_app.test_client()

        # Mint test tokens
        cls.citizen_token, _ = oauth.mint_access_token(
            subject="citizen:9876543210",
            role="citizen",
            scope="citizen",
            audience=oauth.ISSUER + "/mcp/citizen",
            client_id="test-client"
        )
        cls.officer_token, _ = oauth.mint_access_token(
            subject="officer:admin",
            role="officer",
            scope="officer",
            audience=oauth.ISSUER + "/mcp/officer",
            client_id="test-client"
        )

    def test_missing_token_returns_401(self):
        resp = self.client.post("/mcp/citizen", json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list"
        })
        self.assertEqual(resp.status_code, 401)
        data = resp.get_json()
        self.assertIn("error", data)

    def test_cross_role_access_denied(self):
        # Citizen token used on officer endpoint
        resp1 = self.client.post("/mcp/officer",
            headers={"Authorization": "Bearer " + self.citizen_token},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        self.assertEqual(resp1.status_code, 401)

        # Officer token used on citizen endpoint
        resp2 = self.client.post("/mcp/citizen",
            headers={"Authorization": "Bearer " + self.officer_token},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        self.assertEqual(resp2.status_code, 401)

    def test_citizen_initialize(self):
        resp = self.client.post("/mcp/citizen",
            headers={"Authorization": "Bearer " + self.citizen_token},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "clientInfo": {"name": "test-client", "version": "1.0"}
                }
            }
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["jsonrpc"], "2.0")
        self.assertEqual(data["id"], 1)
        self.assertEqual(data["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(data["result"]["serverInfo"]["name"], "sewasetu-citizen-mcp")

    def test_officer_initialize(self):
        resp = self.client.post("/mcp/officer",
            headers={"Authorization": "Bearer " + self.officer_token},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "clientInfo": {"name": "test-client", "version": "1.0"}
                }
            }
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["jsonrpc"], "2.0")
        self.assertEqual(data["result"]["serverInfo"]["name"], "sewasetu-officer-mcp")

    def test_tools_list_separation(self):
        # Citizen tools list
        resp_c = self.client.post("/mcp/citizen",
            headers={"Authorization": "Bearer " + self.citizen_token},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )
        self.assertEqual(resp_c.status_code, 200)
        c_tools = [t["name"] for t in resp_c.get_json()["result"]["tools"]]
        self.assertIn("check_eligibility", c_tools)
        self.assertIn("submit_application", c_tools)
        self.assertNotIn("decide_application", c_tools)

        # Officer tools list
        resp_o = self.client.post("/mcp/officer",
            headers={"Authorization": "Bearer " + self.officer_token},
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"}
        )
        self.assertEqual(resp_o.status_code, 200)
        o_tools = [t["name"] for t in resp_o.get_json()["result"]["tools"]]
        self.assertIn("decide_application", o_tools)
        self.assertIn("list_pending_applications", o_tools)
        self.assertNotIn("submit_application", o_tools)

    def test_tools_call_check_eligibility(self):
        resp = self.client.post("/mcp/citizen",
            headers={"Authorization": "Bearer " + self.citizen_token},
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "check_eligibility",
                    "arguments": {
                        "dob": "15/08/1955",
                        "block": "Sonari"
                    }
                }
            }
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data["result"].get("isError", False))
        content = data["result"]["content"][0]["text"]
        parsed = json.loads(content)
        self.assertTrue(parsed["eligible"])
        self.assertIn("message_hi", parsed)
        self.assertIn("message_en", parsed)

    def test_sse_endpoint_get(self):
        # GET /mcp/citizen initiates SSE session and gives endpoint
        resp = self.client.get("/mcp/citizen?token=" + self.citizen_token,
            headers={"Accept": "text/event-stream"},
            buffered=False
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue("text/event-stream" in resp.headers["Content-Type"])
        first_chunk = next(resp.response)
        self.assertIn(b"event: endpoint", first_chunk)
        self.assertIn(b"/mcp/citizen/messages", first_chunk)


if __name__ == "__main__":
    unittest.main()
