"""Stdio MCP relay. Encode requests for Hermes, which owns approval and execution."""
import hashlib
import hmac
import json
import os
import secrets
import sys

PREFIX = "HERMES_REQUEST_V1 "
CONTRACT = ("Use the hermesbridge_request MCP tool for every Hermes tool request. "
            "Supply the turn token below, the advertised Hermes tool name and its arguments object. "
            "After requesting tools, finish the turn immediately. Hermes supplies results on the next turn. "
            "Never print tool markup or receipts. Answer normally when no tool is needed.")
SCHEMA = {"type": "object", "properties": {key: {"type": kind} for key, kind in
          (("turn", "string"), ("name", "string"), ("arguments", "object"))},
          "required": ["turn", "name", "arguments"], "additionalProperties": False}


def sign(key, payload):
    return hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()


def receipt(key, args):
    payload = json.dumps(dict(args, id="acp_" + secrets.token_hex(12)), separators=(",", ":"))
    return PREFIX + sign(key, payload) + " " + payload


def verify(key, turn, value):
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return None
    signature, _, payload = value[len(PREFIX):].partition(" ")
    if not signature.isascii() or not hmac.compare_digest(sign(key, payload), signature):
        return None
    data = json.loads(payload)
    if data["turn"] == turn:
        return {"id": data["id"], "type": "function",
                "function": {"name": data["name"], "arguments": data["arguments"]}}


def handle(method, params, key):
    if method == "initialize":
        versions = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
        version = params.get("protocolVersion")
        return {"protocolVersion": version if version in versions else versions[-1],
                "capabilities": {"tools": {}}, "serverInfo": {"name": "hermesbridge", "version": "1"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{"name": "request", "description": "Queue a Hermes tool for approval and execution after this turn.",
                           "inputSchema": SCHEMA}]}
    if method != "tools/call":
        raise KeyError(method)
    args = params.get("arguments")
    valid = (params.get("name") == "request" and isinstance(args, dict) and set(args) == set(SCHEMA["required"])
             and isinstance(args["turn"], str) and isinstance(args["name"], str) and isinstance(args["arguments"], dict))
    message = receipt(key, args) if valid else "Expected request(turn, name, arguments object)."
    return {"isError": not valid, "content": [{"type": "text", "text": message}]}


def main():
    key = os.environ["HERMES_MCP_RECEIPT_KEY"]
    for line in sys.stdin.buffer:
        try:
            request = json.loads(line)
        except (ValueError, UnicodeError):
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32700, "message": "Invalid JSON"}}), flush=True)
            continue
        if not isinstance(request, dict) or "id" not in request:
            continue
        try:
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise ValueError()
            result = handle(request.get("method"), params, key)
            reply = {"result": result}
        except KeyError:
            reply = {"error": {"code": -32601, "message": "Unknown method"}}
        except (ValueError, TypeError):
            reply = {"error": {"code": -32602, "message": "Invalid parameters"}}
        print(json.dumps(dict(reply, jsonrpc="2.0", id=request["id"])), flush=True)


if __name__ == "__main__":
    main()
