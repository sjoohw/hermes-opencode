# Mattermost image lab

This records a Windows test of Mattermost image follow-ups in a separate Hermes profile, `mm-luna-lab`. The image forwarding and recall changes from that test are in the repository. They were revised after a review agreed between Claude and Codex, and the revised versions have not been rerun in Mattermost.

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

## Image support and its revisions

| Lab change | Purpose | Revision in the repository |
|---|---|---|
| Inline `data:image/...` parts are sent as ACP image blocks. The frame limit rises to 32 MiB for such prompts. | First analysis of an attachment | Image blocks are sent only when that OpenCode process reports `promptCapabilities.image: true` at initialization. Otherwise a new image in the current request fails it before any prompt is sent, and earlier images become text markers. A fresh session still re-sends every image in the history. |
| `chat.completions.create` returns a coroutine when the calling thread has a running event loop | Hermes awaits clients that set `HERMES_SKIP_ASYNC_WRAP`. Before the change, vision failed with `'Record' object can't be awaited`. | Kept, because Hermes offers no hook for a provider's async client. Synchronous calls without a running loop still return results; this is covered by tests. No synchronous call on an event-loop thread was found in Hermes 0.23.2. |
| An index of attachment descriptions and paths is added to prompts when Hermes offers `vision_analyze` | Lets the model choose an original image for a follow-up | Parses each gateway or TUI image note into one description and path, so two images in one message no longer mix. A new session lists the whole history and a delta only new attachments. The index no longer repeats the tool contract. It still depends on Hermes's note text. |
| Message signatures hash raw content instead of rendered text | Different images give different signatures | Signatures use the rendered text plus the ordered SHA-256 of each inline image, so formatting-only differences keep the session. |

## Observed result

In the same Mattermost DM, a user asked about the background of an earlier Korean history timeline after a different image had been posted. Without another upload, Hermes called `vision_analyze` on the timeline's original cached PNG. The tool reported success and the final answer described a pale cream or beige paper background. An earlier two-number test only demonstrated recall of the initial description, which already contained both numbers.

A profile instruction to reuse the original was insufficient on its own. The attachment index was needed. The index is now sent only with new attachments or a new session, so this result must be rechecked: send images A and B, then, without a new upload, ask about a detail missing from A's first description. `vision_analyze` should succeed on A's original path.

To repeat the check, send an image, then ask about a detail missing from its initial description. Check the Hermes tool history for a successful second call with the original attachment path. This covers a retained original in the same conversation. Cross-session recall and recall after cache deletion were not tested.

## Running the lab

Mattermost is served at `http://127.0.0.1:18065`. To use the phone, connect to the same tailnet, open the HTTPS address shown by `tailscale serve status` on port 8443 in the Mattermost app, and message the test bot. Check the dedicated gateway with `hermes -p mm-luna-lab gateway status`.
