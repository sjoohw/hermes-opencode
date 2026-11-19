"""Guards for Hermes-owned tools, agent selection and no-duplicate retries; offline only."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from support import ROOT, client as acp, constants

TOOLS = [{"type": "function", "function": {"name": "check_connection", "parameters": {"type": "object"}}}]


class GuardTests(unittest.TestCase):
    def temp_path(self):
        handle, path = tempfile.mkstemp(prefix="acp-guard-", suffix=".jsonl")
        os.close(handle)
        os.unlink(path)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def use(self, scenario, extra=None, **kwargs):
        args = [str(ROOT / "tests/fake_acp.py"), scenario] + ([extra] if extra else [])
        item = acp.OpenCodeACPClient(command=sys.executable, args=args, **kwargs)
        self.addCleanup(item.close)
        return item

    @staticmethod
    def complete(item, tools=None):
        return item.chat.completions.create(model=constants.DEFAULT_MODEL, tools=tools, timeout=5,
                                           messages=[{"role": "user", "content": "synthetic offline test"}])

    @staticmethod
    def lines(path):
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_native_tool_that_ran_fails_the_turn(self):
        with self.assertRaisesRegex(acp.NativeToolUsed, "execute"):
            self.complete(self.use("native-ran"))

    def test_refused_native_tool_does_not_fail_the_turn(self):
        self.assertEqual(self.complete(self.use("native-refused")).choices[0].message.content, "FAKE_ACP_OK")

    def test_malformed_tool_call_is_repaired_once_in_session(self):
        path = self.temp_path()
        result = self.complete(self.use("bad-tool-once", path), TOOLS)
        self.assertEqual(result.choices[0].message.tool_calls[0].function.name, "check_connection")
        prompts = self.lines(path)
        self.assertEqual(len(prompts), 2)
        self.assertEqual(prompts[0]["sessionId"], prompts[1]["sessionId"])
        self.assertTrue(prompts[1]["prompt"].startswith("Your previous reply could not be dispatched"))

    def test_unrepairable_tool_call_is_not_success(self):
        with self.assertRaisesRegex(ValueError, "Incomplete remote tool-call envelope"):
            self.complete(self.use("bad-tool-always"), TOOLS)

    def test_worker_death_after_output_is_not_resent(self):
        marker = self.temp_path()
        with self.assertRaises(acp.WorkerDied):
            self.complete(self.use("exit-after-output", marker))
        with open(marker, encoding="utf-8") as handle:
            self.assertEqual(handle.read().count("prompt"), 1)

    def test_operator_agent_is_selected_before_the_prompt(self):
        path = self.temp_path()
        self.assertEqual(self.complete(self.use("agent", path, agent="hermes-relay")).choices[0].message.content, "OK")
        self.assertEqual(self.lines(path)[0]["values"]["mode"], "hermes-relay")

    def test_offered_relay_agent_is_selected_without_configuration(self):
        path = self.temp_path()
        with patch.dict(os.environ, {constants.AGENT_ENV: ""}):
            item = self.use("agent", path)
        self.assertEqual(self.complete(item).choices[0].message.content, "OK")
        self.assertEqual(self.lines(path)[0]["values"]["mode"], constants.RELAY_AGENT)

    def test_unadvertised_agent_sends_no_prompt(self):
        path = self.temp_path()
        with self.assertRaisesRegex(RuntimeError, "not advertised"):
            self.complete(self.use("record", path, agent="hermes-relay"))
        self.assertFalse(os.path.exists(path))

    def test_operator_environment_sets_args_and_agent(self):
        with patch.dict(os.environ, {constants.ARGS_ENV: "acp", constants.AGENT_ENV: "hermes-relay"}):
            item = acp.OpenCodeACPClient(command=sys.executable)
        self.addCleanup(item.close)
        self.assertEqual(item.args, ["acp"])
        self.assertEqual(item.agent, "hermes-relay")

    def test_failed_initialize_names_the_args_override(self):
        with self.assertRaisesRegex(RuntimeError, constants.ARGS_ENV):
            self.complete(self.use("invalid-json"))


if __name__ == "__main__":
    unittest.main()
