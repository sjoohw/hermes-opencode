# Verification

Checked for 0.5.0 on 2026-11-19: native Windows 11, Python 3.13.5, Hermes 0.23.2, OpenCode 1.19.6 and 2.0.32, and the OpenCode-selected model `openai/gpt-6-luna`. Chat and image follow-ups through a Mattermost gateway were confirmed by the user.

## Offline

- `python -m unittest discover -s tests`: 117 tests passed. The suite needs neither Hermes nor OpenCode.
- `hermes plugins doctor`: manifest 0.5.0, import, runtime discovery and provider registration passed.

## Real OpenCode

| Check | 1.19.6 | 2.0.32 |
|---|---|---|
| Startup with no bridge variables | `acp --pure` (port 4885 was in use) | `acp`, after `--port` and `--pure` were rejected |
| `hermes-relay` from the V1 config | Offered and selected automatically | Converted to the 2.x form, offered and selected automatically |
| Relay permissions | Final rule `* * deny` | Final rule `* * deny` |
| `tests/smoke_cli.py --list-models` | Passed | Passed |
| `tests/e2e_tool_roundtrip.py` | Passed | Passed |

The live round trip sends two prompts. The model requested the synthetic remote tool, then answered with the code from the supplied tool result. No tool ran on the host.

## Findings

- OpenCode 1.19.6 rejects the whole config when the 2.x-only `agents`/`permissions` keys are present. OpenCode 2.0.32 accepts the V1 `agent`/`prompt`/`permission` keys and converts them, so `opencode/hermes-relay.jsonc` uses V1 keys.
- On 2.0.32, a one-line prompt cost 14,559 input tokens with the built-in `build` agent and 234 with `hermes-relay`.
- The OpenCode free model `opencode/tidepool-alpha-free` returned `provider.quota` for every custom or modified agent. Use a model that accepts custom agents.
- For an unknown session, both CLIs returned JSON-RPC `-32602` with the session ID in `data.sessionId`. Only this response, before any output, makes the bridge replay a turn on a fresh session.
- Usage: one 2.0.32 response reported 5,014 input + 2,048 cached input + 6 output = 7,068 total. Hermes normalized it to 7,062 prompt tokens, and its compressor triggered at the expected threshold.

## Compression check

`tools/eval_compaction.py` runs a fixed synthetic handoff through the real Hermes compressor. It uses the installed Hermes Python with this checkout on `PYTHONPATH`. With `openai/gpt-6-luna` as both compressor and examiner:

| Arm | Retained context (estimated tokens) | Constraint, reason, failed trial, next ticket | Correct follow-up ticket |
|---|---:|---|---|
| Uncompressed control | 38,039 | 4/4 | Yes |
| Compressed, no history search | 5,819 | 4/4 | Yes |
| Compressed, with history search | 5,819 before search excerpts | 4/4 | Yes |

This single case measures recall of exact identifiers only.

## Not covered

- A company OpenCode deployment. This PC used a personal OpenAI login.
- Incremental streaming. Replies are returned after the bridge has validated the complete tool envelope and native tool events.
