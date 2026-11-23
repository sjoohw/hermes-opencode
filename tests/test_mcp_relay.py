"""Regression checks for the MCP-to-Hermes dispatch boundary."""
import json
import secrets
import unittest

from support import client as acp


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.key = secrets.token_hex(32)
        self.turn = "current"
        self.session = acp.SessionState("s", {"configOptions": [{"id": "mode", "currentValue": "hermes-mcp"}]}, None)
        self.args = {"turn": self.turn, "name": "read", "arguments": {"name": "private fixture"}}
        self.value = acp.mcp_relay.handle("tools/call", {"name": "request", "arguments": self.args},
                                          self.key)["content"][0]["text"]

    def send(self, events, stop="end_turn"):
        def request(method, params, sinks, timeout):
            worker._active = acp._Active(1, "s", sinks)
            for event in events:
                worker._route({"params": {"sessionId": "s", "update": event}})
            return {"stopReason": stop}
        worker = object.__new__(acp.BridgeWorker)
        worker.mcp_key = self.key
        worker._state_lock = acp.threading.Lock()
        worker.request = request
        return acp.OpenCodeACPClient._send_prompt(worker, self.session, "", 1, turn=self.turn)[0]

    def events(self, title="hermesbridge_request"):
        return [{"sessionUpdate": "tool_call", "toolCallId": "x", "title": title, "kind": "other", "status": "pending"},
                {"sessionUpdate": "tool_call_update", "toolCallId": "x", "status": "completed", "content": [
                    {"type": "content", "content": {"type": "text", "text": self.value}}]}]

    def test_mcp_returns_one_validated_request(self):
        answer = self.send(self.events())
        calls, content = acp.parse_tool_calls(answer, [{"function": {"name": "read"}}])
        self.assertFalse(content)
        self.assertEqual(json.loads(calls[0].function.arguments), self.args["arguments"])
        with self.assertRaises(ValueError):
            acp.parse_tool_calls(answer, [])

    def test_reject_mixed_origin_replay_and_incomplete_turn(self):
        extra = {"sessionUpdate": "tool_call", "toolCallId": "y", "kind": "execute", "status": "in_progress"}
        for events in (self.events("private fixture"), self.events() + [extra]):
            with self.assertRaises(acp.NativeToolUsed) as ctx:
                self.send(events)
            self.assertNotIn("private fixture", str(ctx.exception))
            self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaisesRegex(acp.ToolProtocolError, "ACP-MCP-STOP"):
            self.send(self.events(), "max_tokens")
        self.turn = "next"
        with self.assertRaises(acp.NativeToolUsed):
            self.send(self.events())

    def test_invalid_signature_and_duplicate_receipt(self):
        with self.assertRaises(acp.NativeToolUsed):
            self.send(self.events() + [dict(e, toolCallId="y") for e in self.events()])
        self.value = self.value.replace("private fixture", "altered")
        with self.assertRaises(acp.NativeToolUsed):
            self.send(self.events())
