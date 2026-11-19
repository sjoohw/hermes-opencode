# Mattermost image lab

This records a Windows test of Mattermost image follow-ups. The image forwarding and recall changes tested in the separate Hermes profile are now included in the repository.

## Setup

On 2026-11-19 the profile ran Hermes 0.23.2 with OpenCode 1.19.6 and the OpenCode-selected model `openai/gpt-6-luna`. Both chat and auxiliary vision used the installed OpenCode CLI:

```yaml
model:
  provider: hermes-opencode-acp
  default: opencode-default
auxiliary:
  vision:
    provider: hermes-opencode-acp
    model: opencode-default
```

This routes images through OpenCode, and so through the chat model. The project intent in `intend.md` assigns vision to Gemma 4 instead. A deployment that follows the intent points `auxiliary.vision` at Gemma 4.

## Image support tested in the lab

| Change | Purpose | Limitation |
|---|---|---|
| Inline `data:image/...` parts are sent as ACP image blocks. The frame limit rises to 32 MiB for such prompts. | First analysis of an attachment | Does not check the agent's `promptCapabilities.image`. A fresh session re-sends every image in the history. |
| `chat.completions.create` returns a coroutine when the calling thread has a running event loop | Hermes awaits clients that set `HERMES_SKIP_ASYNC_WRAP`. Before the change, vision failed with `'Record' object can't be awaited`. | A synchronous caller on an event-loop thread receives a coroutine. An explicit async adapter would avoid guessing. |
| An index of earlier attachment descriptions and paths is appended to every prompt when Hermes offers `vision_analyze` | Lets the model choose an original image for a follow-up | Parses an internal Hermes text marker. Repeats the index and the tool contract on every turn. |
| Message signatures hash raw content instead of rendered text | Different images give different signatures | Formatting differences now force a full transcript re-send. |

## Observed result

In the same Mattermost DM, a user asked about the background of an earlier Korean history timeline after a different image had been posted. Without another upload, Hermes called `vision_analyze` on the timeline's original cached PNG. The tool reported success and the final answer described a pale cream or beige paper background. An earlier two-number test only demonstrated recall of the initial description, which already contained both numbers.

A profile instruction to reuse the original was insufficient on its own. The attachment index above was needed.

To repeat the check, send an image, then ask about a detail missing from its initial description. Check the Hermes tool history for a successful second call with the original attachment path. This covers a retained original in the same conversation. Cross-session recall and recall after cache deletion were not tested.

## Running the lab

Mattermost is served at `http://127.0.0.1:18065`. To use the phone, connect to the same tailnet, open the HTTPS address shown by `tailscale serve status` on port 8443 in the Mattermost app, and message the test bot. Check the dedicated gateway with `hermes -p mm-luna-lab gateway status`.

The local lab scripts are under `%LOCALAPPDATA%/mm-luna-lab/`. Run `start.ps1` to start the lab. Running `remove-test.ps1` deletes the test profile and Mattermost test data.
