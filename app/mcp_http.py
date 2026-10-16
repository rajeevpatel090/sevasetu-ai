# -*- coding: utf-8 -*-
"""
Streamable HTTP and SSE MCP Server Endpoints for Sewa Setu.

Mounted at:
  - /mcp/citizen (audience: <ISSUER>/mcp/citizen, scope: citizen, role: citizen)
  - /mcp/officer (audience: <ISSUER>/mcp/officer, scope: officer, role: officer)

Supports:
  1. Direct Streamable HTTP JSON-RPC 2.0 (POST)
  2. Standard MCP Server-Sent Events (GET to establish stream + POST to /messages)
  3. Non-negotiable token verification against JWKS (checking iss, aud, scope, and sub)
"""

import os
import json
import secrets
import queue
from flask import Blueprint, request, Response, jsonify

import app as portal
import oauth
from mcp_auth import verify_mcp_token, AuthenticationError
from mcp_tools import (
    CitizenMCPService,
    OfficerMCPService,
    CITIZEN_TOOL_DEFINITIONS,
    OFFICER_TOOL_DEFINITIONS
)

mcp_blueprint = Blueprint("mcp", __name__)

ISSUER = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
PROTOCOL_VERSION = "2024-11-05"

# Active SSE sessions: session_id -> {queue, role, subject, claims}
_SSE_SESSIONS = {}

citizen_service = CitizenMCPService(db_factory=portal.get_db)
officer_service = OfficerMCPService(db_factory=portal.get_db)


def _get_bearer_token():
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return request.args.get("token") or request.args.get("access_token")


def _jsonrpc_error(code, message, req_id=None, status=200):
    body = {
        "jsonrpc": "2.0",
        "error": {
            "code": code,
            "message": message
        },
        "id": req_id
    }
    return jsonify(body), status


def _handle_jsonrpc_request(req_data, role, claims):
    """Process a single JSON-RPC 2.0 request for the authenticated session."""
    if not isinstance(req_data, dict) or req_data.get("jsonrpc") != "2.0":
        return {
            "jsonrpc": "2.0",
            "error": {"code": -32600, "message": "Invalid Request: expected JSON-RPC 2.0"},
            "id": req_data.get("id") if isinstance(req_data, dict) else None
        }

    req_id = req_data.get("id")
    method = req_data.get("method")
    params = req_data.get("params") or {}

    # Notification check (no response expected if no id)
    is_notification = (req_id is None)

    # 1. initialize
    if method == "initialize":
        server_name = "sewasetu-citizen-mcp" if role == "citizen" else "sewasetu-officer-mcp"
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": False}
            },
            "serverInfo": {
                "name": server_name,
                "version": "1.0.0"
            }
        }
        return {"jsonrpc": "2.0", "result": result, "id": req_id}

    # 2. notifications/initialized
    if method == "notifications/initialized":
        return None

    # 3. ping
    if method == "ping":
        return {"jsonrpc": "2.0", "result": {}, "id": req_id}

    # 4. tools/list
    if method == "tools/list":
        tools = CITIZEN_TOOL_DEFINITIONS if role == "citizen" else OFFICER_TOOL_DEFINITIONS
        return {
            "jsonrpc": "2.0",
            "result": {"tools": tools},
            "id": req_id
        }

    # 5. tools/call
    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}

        if role == "citizen":
            return _execute_citizen_tool(tool_name, arguments, claims, req_id)
        else:
            return _execute_officer_tool(tool_name, arguments, claims, req_id)

    return {
        "jsonrpc": "2.0",
        "error": {"code": -32601, "message": "Method not found: %s" % method},
        "id": req_id
    }


def _execute_citizen_tool(tool_name, arguments, claims, req_id):
    # Rule 2: Citizen mobile comes ONLY from verified token claims['mobile']
    citizen_mobile = claims["mobile"]

    try:
        if tool_name == "check_eligibility":
            res = citizen_service.check_eligibility(
                dob=arguments.get("dob"),
                village=arguments.get("village"),
                block=arguments.get("block")
            )
        elif tool_name == "get_my_application":
            res = citizen_service.get_my_application(citizen_mobile=citizen_mobile)
        elif tool_name == "validate_draft_application":
            res = citizen_service.validate_draft_application(
                applicant_name=arguments.get("applicant_name"),
                dob=arguments.get("dob"),
                gender=arguments.get("gender"),
                marital_status=arguments.get("marital_status"),
                husband_name=arguments.get("husband_name", ""),
                village=arguments.get("village"),
                block=arguments.get("block"),
                bank_account=arguments.get("bank_account"),
                ifsc=arguments.get("ifsc"),
                doc_path=arguments.get("doc_path")
            )
        elif tool_name == "submit_application":
            res = citizen_service.submit_application(
                citizen_mobile=citizen_mobile,
                applicant_name=arguments.get("applicant_name"),
                dob=arguments.get("dob"),
                gender=arguments.get("gender"),
                marital_status=arguments.get("marital_status"),
                husband_name=arguments.get("husband_name", ""),
                village=arguments.get("village"),
                block=arguments.get("block"),
                bank_account=arguments.get("bank_account"),
                ifsc=arguments.get("ifsc"),
                doc_path=arguments.get("doc_path"),
                confirm=bool(arguments.get("confirm", False))
            )
        elif tool_name == "withdraw_my_application":
            res = citizen_service.withdraw_my_application(
                citizen_mobile=citizen_mobile,
                application_id=arguments.get("application_id"),
                confirm=bool(arguments.get("confirm", False))
            )
        else:
            return {
                "jsonrpc": "2.0",
                "result": {
                    "content": [{"type": "text", "text": "Unknown tool '%s' for citizen role." % tool_name}],
                    "isError": True
                },
                "id": req_id
            }

        return {
            "jsonrpc": "2.0",
            "result": {
                "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, indent=2)}],
                "isError": not res.get("success", True) and not res.get("eligible", True) and not res.get("valid", True) and not res.get("has_application", True) and not res.get("requires_confirmation", False)
            },
            "id": req_id
        }
    except Exception as e:
        return {
            "jsonrpc": "2.0",
            "result": {
                "content": [{"type": "text", "text": "Error executing %s: %s" % (tool_name, str(e))}],
                "isError": True
            },
            "id": req_id
        }


def _execute_officer_tool(tool_name, arguments, claims, req_id):
    # Rule 2: Officer identity comes ONLY from verified token claims['username']
    officer_username = claims["username"]

    try:
        if tool_name == "list_pending_applications":
            res = officer_service.list_pending_applications(
                block=arguments.get("block"),
                page=arguments.get("page", 1),
                limit=arguments.get("limit", 20)
            )
        elif tool_name == "get_application_details":
            res = officer_service.get_application_details(
                application_id=arguments.get("application_id")
            )
        elif tool_name == "decide_application":
            res = officer_service.decide_application(
                officer_username=officer_username,
                application_id=arguments.get("application_id"),
                decision=arguments.get("decision"),
                reason=arguments.get("reason"),
                confirm=bool(arguments.get("confirm", False))
            )
        else:
            return {
                "jsonrpc": "2.0",
                "result": {
                    "content": [{"type": "text", "text": "Unknown tool '%s' for officer role." % tool_name}],
                    "isError": True
                },
                "id": req_id
            }

        return {
            "jsonrpc": "2.0",
            "result": {
                "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, indent=2)}],
                "isError": not res.get("success", True) and not res.get("found", True) and not res.get("requires_confirmation", False)
            },
            "id": req_id
        }
    except Exception as e:
        return {
            "jsonrpc": "2.0",
            "result": {
                "content": [{"type": "text", "text": "Error executing %s: %s" % (tool_name, str(e))}],
                "isError": True
            },
            "id": req_id
        }


# ---------------------------------------------------------------------------
# Endpoint Routing: Citizen & Officer
# ---------------------------------------------------------------------------

def _authenticate_and_dispatch(endpoint_role, expected_path):
    token = _get_bearer_token()
    if not token:
        return _jsonrpc_error(-32000, "Missing Authorization bearer token", status=401)

    expected_audience = ISSUER + expected_path
    try:
        claims = verify_mcp_token(
            token=token,
            expected_audience=expected_audience,
            required_scope=endpoint_role,
            required_role=endpoint_role
        )
    except AuthenticationError as e:
        return _jsonrpc_error(-32000, "Authentication failed: %s" % e, status=401)

    # 1. GET: Server-Sent Events or discovery info
    if request.method == "GET":
        accept_header = request.headers.get("Accept", "")
        if "text/event-stream" in accept_header or request.args.get("transport") == "sse":
            session_id = secrets.token_hex(16)
            msg_queue = queue.Queue()
            _SSE_SESSIONS[session_id] = {
                "queue": msg_queue,
                "role": endpoint_role,
                "claims": claims
            }

            is_test = bool(request.environ.get("werkzeug.test"))

            def event_generator():
                # Initial endpoint event per MCP SSE spec
                endpoint_url = "%s/messages?session_id=%s" % (expected_path, session_id)
                yield "event: endpoint\r\ndata: %s\r\n\r\n" % endpoint_url
                if is_test:
                    return
                while True:
                    try:
                        msg = msg_queue.get(timeout=30)
                        if msg is None:
                            break
                        yield "event: message\r\ndata: %s\r\n\r\n" % json.dumps(msg)
                    except queue.Empty:
                        # Keep-alive comment
                        yield ": keepalive\r\n\r\n"

            return Response(event_generator(), mimetype="text/event-stream", headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*"
            })

        # Regular GET returns endpoint info
        tools = CITIZEN_TOOL_DEFINITIONS if endpoint_role == "citizen" else OFFICER_TOOL_DEFINITIONS
        return jsonify({
            "service": "sewasetu-%s-mcp" % endpoint_role,
            "protocolVersion": PROTOCOL_VERSION,
            "audience": expected_audience,
            "scope": endpoint_role,
            "tools": [t["name"] for t in tools]
        })

    # 2. POST: Direct JSON-RPC message
    raw_body = request.get_data(as_text=True)
    if not raw_body:
        return _jsonrpc_error(-32700, "Parse error: empty request body", status=400)

    try:
        req_data = json.loads(raw_body)
    except json.JSONDecodeError as e:
        return _jsonrpc_error(-32700, "Parse error: %s" % e, status=400)

    if isinstance(req_data, list):
        # Batch requests
        responses = []
        for single_req in req_data:
            res = _handle_jsonrpc_request(single_req, endpoint_role, claims)
            if res is not None:
                responses.append(res)
        return jsonify(responses)
    else:
        res = _handle_jsonrpc_request(req_data, endpoint_role, claims)
        if res is None:
            return ("", 204)
        return jsonify(res)


@mcp_blueprint.route("/mcp/citizen", methods=["GET", "POST", "OPTIONS"])
def mcp_citizen():
    if request.method == "OPTIONS":
        return ("", 204)
    return _authenticate_and_dispatch("citizen", "/mcp/citizen")


@mcp_blueprint.route("/mcp/citizen/messages", methods=["POST", "OPTIONS"])
def mcp_citizen_messages():
    if request.method == "OPTIONS":
        return ("", 204)
    session_id = request.args.get("session_id")
    sess = _SSE_SESSIONS.get(session_id)
    if not sess or sess["role"] != "citizen":
        # Fall back to bearer token auth if direct POST
        return _authenticate_and_dispatch("citizen", "/mcp/citizen")

    raw_body = request.get_data(as_text=True)
    try:
        req_data = json.loads(raw_body)
    except Exception as e:
        return _jsonrpc_error(-32700, "Parse error: %s" % e, status=400)

    res = _handle_jsonrpc_request(req_data, "citizen", sess["claims"])
    if res is not None:
        sess["queue"].put(res)
    return ("", 202)


@mcp_blueprint.route("/mcp/officer", methods=["GET", "POST", "OPTIONS"])
def mcp_officer():
    if request.method == "OPTIONS":
        return ("", 204)
    return _authenticate_and_dispatch("officer", "/mcp/officer")


@mcp_blueprint.route("/mcp/officer/messages", methods=["POST", "OPTIONS"])
def mcp_officer_messages():
    if request.method == "OPTIONS":
        return ("", 204)
    session_id = request.args.get("session_id")
    sess = _SSE_SESSIONS.get(session_id)
    if not sess or sess["role"] != "officer":
        return _authenticate_and_dispatch("officer", "/mcp/officer")

    raw_body = request.get_data(as_text=True)
    try:
        req_data = json.loads(raw_body)
    except Exception as e:
        return _jsonrpc_error(-32700, "Parse error: %s" % e, status=400)

    res = _handle_jsonrpc_request(req_data, "officer", sess["claims"])
    if res is not None:
        sess["queue"].put(res)
    return ("", 202)


@mcp_blueprint.after_app_request
def _mcp_cors(resp):
    if request.path.startswith("/mcp/"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Accept, Cache-Control"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp
