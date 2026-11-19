# Hermes OpenCode ACP

Connect Hermes to your installed OpenCode CLI (1.x or 2.x) over the Agent Client Protocol (ACP). OpenCode chooses the model, agent and reasoning settings when it creates the ACP session. An existing interactive OpenCode session is separate.

## Hermes setup

With this local fork installed and enabled, select provider `hermes-opencode-acp` and model `opencode-default` in Hermes. The model name is a Hermes display label. OpenCode receives no model override. You can run a single chat with:

```sh
hermes chat --provider hermes-opencode-acp --model opencode-default
```

OpenCode uses the working directory passed to the client (otherwise the Hermes process working directory), its normal configuration and the process environment. Set `HERMES_OPENCODE_ACP_COMMAND` to the OpenCode CLI's absolute executable path if several installations are available. Hermes communicates with OpenCode over stdio. OpenCode manages its own model configuration and credentials.

The bridge supports OpenCode 1.x and 2.x (checked with 1.19.6 and 2.0.32) without extra settings. It starts the CLI with `acp --pure --port 4885`. If initialization fails, it retries with `acp --pure` (normal port selection), then with `acp` for 2.x, which rejects `--pure` and `--port`. Only initialization is retried; no session or prompt exists yet. Hermes always communicates over stdio. To force specific arguments, set `HERMES_OPENCODE_ACP_ARGS`, for example `acp --pure`. It replaces the whole argument list and disables the retries.

## Hermes-owned tools: the relay agent

OpenCode's default `build` agent carries its own coding prompt and native tools. To keep every action behind Hermes approval and drop that prompt, add the `hermes-relay` agent from [opencode/hermes-relay.jsonc](opencode/hermes-relay.jsonc) to the global OpenCode config (`opencode debug paths` shows its folder). Use the file's V1 keys (`agent`, `prompt`, `permission`) on both versions: 2.x converts them, and 1.x rejects the whole config if the 2.x-only `agents`/`permissions` keys are present.

When OpenCode offers `hermes-relay`, the bridge selects it on each new ACP session before any prompt is sent. To require the agent, set `HERMES_OPENCODE_ACP_AGENT=hermes-relay`. With the variable set, a request fails and no prompt is sent if OpenCode does not offer the agent. Without it, the bridge uses OpenCode's default agent when `hermes-relay` is missing. The model, credentials and effort stay as OpenCode configures them. Some models refuse custom agents: the OpenCode free model `opencode/tidepool-alpha-free` returned a usage-limit error for every non-built-in agent.

Whatever agent is used, the bridge watches OpenCode's tool notifications. A native tool that OpenCode reports as running or completed fails the turn with `NativeToolUsed`, and that reply is discarded. A native tool that was refused does not fail the turn.

## Environment variables

All three are optional. Set them in the environment that starts Hermes, including a gateway started in the background.

| Variable | Effect |
|---|---|
| `HERMES_OPENCODE_ACP_COMMAND` | Absolute path of the OpenCode executable. Without it, the bridge uses `opencode` on `PATH`, then common install folders. |
| `HERMES_OPENCODE_ACP_ARGS` | Whole argument list, split on spaces. Disables the startup retries. |
| `HERMES_OPENCODE_ACP_AGENT` | Agent to require. A missing agent fails the request before any prompt is sent. |

A Hermes process keeps the environment and plugin code it started with. After changing a variable or the installed plugin, restart the Hermes gateway (`hermes gateway restart --detach`).

## Checking the Hermes profile

`model.provider` must be the bridge, and no other setting in the profile may reach the chat model directly. Run the read-only checker with Hermes's Python:

```sh
python tools/check_hermes_config.py --forbid <direct model host> --forbid <model name>
```

It fails if the main provider is not the bridge or any value contains a forbidden string. It warns about fallback chains and delegation providers outside the bridge, and lists each `auxiliary.*` provider. It prints key paths, not values.

## Images and auxiliary vision

In the Hermes profile used by your TUI or Mattermost gateway, keep the bridge as the chat provider and point `auxiliary.vision` at an image-capable model. Replace the two placeholders with your existing vision backend settings.

```yaml
model:
  provider: hermes-opencode-acp
  default: opencode-default

auxiliary:
  vision:
    provider: "<vision-provider>"
    model: "<vision-model-id>"
```

In Hermes 0.23.2, omitting `agent.image_input_mode` uses its default, `auto`. An explicitly configured auxiliary vision backend takes precedence in this mode. If your profile already uses that backend, keep its configuration.

For a Mattermost image attachment, Hermes sends the image to the vision model and passes its description and cached file path to the main model as text over ACP. If OpenCode reports image input (`promptCapabilities.image: true`) at initialization, the bridge forwards inline base64 images as ACP image blocks, for example when the bridge itself is the vision backend. Otherwise a request whose latest user message carries an image fails before any prompt is sent, so a text-only guess is never returned as an image analysis. Earlier images in the history become text markers. Keep the Hermes `vision_analyze` tool enabled so the main model can ask a more specific question about the original image on a later turn.

When Hermes offers `vision_analyze`, the bridge adds an index of attachment descriptions and paths from Hermes's image notes: the whole history for a new ACP session, new attachments only for a continued one. A Mattermost image follow-up flow was tested in a separate Windows profile before these behaviors were revised. See [docs/MATTERMOST-LAB.md](docs/MATTERMOST-LAB.md).

## Tools and permissions

Hermes receives structured tool requests reconstructed from OpenCode's text replies and applies its normal tool validation and approval flow. The bridge declines ACP permission requests and client-side filesystem and terminal requests. Use the relay agent above so OpenCode has no native tools to run. The child CLI inherits environment variables available to Hermes, including credentials.

If a reply contains a malformed or disallowed tool request, the bridge asks OpenCode once, in the same session, to resend it. If the second reply also fails, the turn fails. If OpenCode exits after a turn has produced output, the bridge does not resend the prompt, so that inference is never billed twice.

## Verification

Run `python -m unittest discover -s tests` for offline transport tests. They need neither Hermes nor OpenCode. Run `python tests/smoke_cli.py --command /path/to/opencode` for a real ACP session handshake with no chat prompt. Run `python tests/e2e_tool_roundtrip.py --command /path/to/opencode` for a live check that sends two prompts through OpenCode: one tool request, then an answer that must use the tool's result. A reported model of `opencode-default` means the server omitted model metadata.

This fork builds on [engida32/hermes-opencode-acp](https://github.com/engida32/hermes-opencode-acp) and [cryptobredda/hermes-opencode-free-acp](https://github.com/cryptobredda/hermes-opencode-free-acp). See [LICENSE](LICENSE) for attribution. [docs/VERIFICATION.md](docs/VERIFICATION.md) records this fork's results and the upstream history.
