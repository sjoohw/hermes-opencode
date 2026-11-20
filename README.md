# Hermes OpenCode ACP

Connect Hermes to your installed OpenCode CLI (1.x or 2.x) over the Agent Client Protocol (ACP). OpenCode chooses the model, agent and reasoning settings when it creates the ACP session. An existing interactive OpenCode session is separate.

Chat and auxiliary clients in the same Hermes process share one OpenCode worker when their profile, working directory and launch settings match. Each client keeps its own ACP session history. A title request can queue behind chat. Closing one idle client leaves the worker available to the others; the last client closes it. Cancellation of an active request or a dead worker can require a fresh process.

When the selected chat provider is this bridge, `hermes -p <profile> gateway run` starts a background Python thread during provider discovery. It initializes the shared OpenCode process and prepares one ACP session without sending a prompt. Leave the serving gateway running before opening chat. The first matching client adopts that session. A request that arrives during initialization waits for the same worker. Set `HERMES_OPENCODE_ACP_PREWARM=0` to disable gateway prewarming. The gateway startup client keeps the worker alive until gateway shutdown. This only warms the matching gateway process, profile and workspace.

## Hermes setup

With this local fork installed and enabled, select provider `hermes-opencode-acp` and model `opencode-default` in Hermes. The model name is a Hermes display label. OpenCode receives no model override. You can run a single chat with:

```sh
hermes chat --provider hermes-opencode-acp --model opencode-default
```

OpenCode uses the working directory passed to the client (otherwise the Hermes process working directory), its normal configuration and the process environment. Set `HERMES_OPENCODE_ACP_COMMAND` to the OpenCode CLI's absolute executable path if several installations are available. Hermes communicates with OpenCode over stdio. OpenCode manages its own model configuration and credentials.

The bridge supports OpenCode 1.x and 2.x (checked with 1.19.6 and 2.0.32) without extra settings. It starts the CLI with `acp --pure --port 4885`. If initialization fails, it retries with `acp --pure` (normal port selection), then with `acp` for 2.x, which rejects `--pure` and `--port`. Within the Hermes process, later workers try the last successful arguments for that executable first. A changed executable or failed handshake triggers discovery again. Hermes always communicates over stdio. To force specific arguments, set `HERMES_OPENCODE_ACP_ARGS`, for example `acp --pure`. It replaces the whole argument list and disables the retries.

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

## Gateway at Windows sign-in

Use the Mattermost profile for every command. Run `Get-Command opencode` under the Windows account that runs Hermes. If necessary, set `HERMES_OPENCODE_ACP_COMMAND` to the executable's absolute path in that profile. With the correct profile selected, stop its manually started gateway before installing sign-in startup.

```powershell
hermes -p <profile> gateway stop
hermes -p <profile> gateway install
hermes -p <profile> gateway status
```

After the next sign-in, check status for the same profile and confirm that a Mattermost DM receives exactly one reply. Hermes uses a Windows scheduled task or a Startup-folder fallback. To remove automatic startup, run `hermes -p <profile> gateway stop` followed by `hermes -p <profile> gateway uninstall`.

## Diagnostics and token counts

Set `HERMES_OPENCODE_ACP_DIAGNOSTICS=1` in the gateway environment to log recognized provider error names (`provider.quota` and `provider.auth`). At INFO level this option also logs worker startup or reuse, time waiting for the worker and prompt locks, and time from each ACP prompt RPC to its first update and completion. Timing records contain no transcript or filenames. Vendor messages, stderr and unknown error names are discarded. A request shares one timeout budget across queueing, worker initialization, session setup, prompting and repair. Waiting requests can expire without interrupting an active conversation. Session history is recorded before the next request can reuse the worker. A reused session is recreated once only on the known missing-session response before any update. Quota and authentication errors return immediately. Cancellation sends an ACP notification to the active prompt and allows up to 250 ms for it to finish before stopping the worker.

When the CLI supplies consistent usage, the bridge includes cached input in the prompt count and reasoning in the completion count. Missing or ambiguous counts stay unknown. If a tool response needs correction, Hermes receives the final call's counts for compaction. This omits the first call from the Hermes billing total. Diagnostic logs include the combined total when both calls report usage.

## Checking the Hermes profile

`model.provider` must be the bridge, and no other setting in the profile may reach the chat model directly. Run the read-only checker with Hermes's Python:

```sh
python tools/check_hermes_config.py --forbid <direct model host> --forbid <model name>
```

It fails if the main provider is not the bridge or any value contains a forbidden string. It warns about fallback chains and delegation providers outside the bridge, and lists each `auxiliary.*` provider. It prints key paths, not values.

## Images and auxiliary vision

Select an image-capable model in the OpenCode configuration used by the gateway or TUI. In the corresponding Hermes profile, route both chat and auxiliary vision through the installed OpenCode CLI. The current bridge uses the OpenCode session default for both.

```yaml
model:
  provider: hermes-opencode-acp
  default: opencode-default

auxiliary:
  vision:
    provider: hermes-opencode-acp
    model: opencode-default
```

In Hermes 0.23.2, omitting `agent.image_input_mode` uses its default, `auto`. An explicitly configured auxiliary vision backend takes precedence in this mode.

For a Mattermost image attachment, Hermes sends the image to the vision model and passes its description and cached file path to the main model as text over ACP. If OpenCode reports image input (`promptCapabilities.image: true`) at initialization, the bridge forwards inline base64 images as ACP image blocks, for example when the bridge itself is the vision backend. Otherwise a request whose latest user message carries an image fails before any prompt is sent, so a text-only guess is never returned as an image analysis. Earlier images in the history become text markers. Keep the Hermes `vision_analyze` tool enabled so the main model can ask a more specific question about the original image on a later turn.

When Hermes offers `vision_analyze`, the bridge adds an index of attachment descriptions and paths from Hermes's image notes: the whole history for a new ACP session, new attachments only for a continued one.

## Tools and permissions

Hermes receives structured tool requests reconstructed from OpenCode's text replies and applies its normal tool validation and approval flow. The bridge declines ACP permission requests and client-side filesystem and terminal requests. Use the relay agent above so OpenCode has no native tools to run. The child CLI inherits environment variables available to Hermes, including credentials.

If a reply contains a malformed or disallowed tool request, the bridge asks OpenCode once, in the same session, to resend it. If the second reply also fails, the turn fails. If OpenCode exits after a turn has produced output, the bridge does not resend that prompt.

## Verification

Run `python -m unittest discover -s tests` for offline transport tests. They need neither Hermes nor OpenCode. Run `python tests/smoke_cli.py --command /path/to/opencode` for a real ACP session handshake with no chat prompt. Run `python tests/e2e_tool_roundtrip.py --command /path/to/opencode` for a live check that sends two prompts through OpenCode: one tool request, then an answer that must use the tool's result. A reported model of `opencode-default` means the server omitted model metadata.

This fork builds on [engida32/hermes-opencode-acp](https://github.com/engida32/hermes-opencode-acp) and [cryptobredda/hermes-opencode-free-acp](https://github.com/cryptobredda/hermes-opencode-free-acp). See [LICENSE](LICENSE) for attribution. [docs/VERIFICATION.md](docs/VERIFICATION.md) records this fork's verification results.
