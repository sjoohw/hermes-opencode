"""Opt-in live check: one remote tool request and one answer from its result.

Unlike the offline suite, this sends two real prompts through OpenCode and its
configured model. The tool is synthetic and is never executed; the script
supplies its result. Use the agent and arguments Hermes will use.

  python tests/e2e_tool_roundtrip.py --command <opencode> --args acp --agent hermes-relay
"""
import argparse
import json
import secrets

from support import client as acp, constants

TOOLS = [{"type": "function", "function": {
    "name": "get_ticket_code",
    "description": "Return the private code for a ticket. Only this tool knows the code.",
    "parameters": {"type": "object", "properties": {"ticket": {"type": "string"}}, "required": ["ticket"]},
}}]


def session_mode(config):
    """The agent OpenCode confirmed for the session, whether chosen explicitly or automatically."""
    return next((item.get("currentValue") for item in config.get("configOptions") or []
                 if isinstance(item, dict) and item.get("id") == "mode"), None)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--command", required=True)
    parser.add_argument("--args", nargs="+", help="CLI arguments; defaults to the bridge's")
    parser.add_argument("--agent", help="OpenCode agent (ACP mode) to select")
    parser.add_argument("--timeout", type=float, default=180)
    options = parser.parse_args()
    code = "T" + secrets.token_hex(4).upper()
    client = acp.OpenCodeACPClient(command=options.command, args=options.args, agent=options.agent)
    messages = [{"role": "user", "content": "What is the private code for ticket 42? Use the tool."}]
    try:
        first = client.chat.completions.create(model=constants.DEFAULT_MODEL, messages=messages,
                                               tools=TOOLS, timeout=options.timeout).choices[0].message
        calls = first.tool_calls or []
        if [call.function.name for call in calls] != ["get_ticket_code"]:
            raise SystemExit(f"FAIL: expected one get_ticket_code request, got {[c.function.name for c in calls]}")
        messages += [first.model_dump(), {"role": "tool", "tool_call_id": calls[0].id, "content": code}]
        second = client.chat.completions.create(model=constants.DEFAULT_MODEL, messages=messages,
                                                tools=TOOLS, timeout=options.timeout)
        answer = second.choices[0].message.content or ""
        session = client._worker._sessions[-1]
        print(json.dumps({"ok": code in answer, "tool_requested": True, "code_in_answer": code in answer,
                          "session_model": second.model, "agent": session_mode(session.config)}, indent=2))
        if code not in answer:
            raise SystemExit("FAIL: the answer did not use the tool result")
    finally:
        client.close()


if __name__ == "__main__":
    main()
