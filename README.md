# Hermes OpenCode ACP

Connect Hermes to your installed OpenCode CLI with `acp --pure`. OpenCode chooses the model, agent and reasoning settings when it creates the ACP session. An existing interactive OpenCode session is separate.

## Hermes setup

With this local fork installed and enabled, select provider `hermes-opencode-acp` and model `opencode-default` in Hermes. The model name is a Hermes display label. OpenCode receives no model override. You can run a single chat with:

```sh
hermes chat --provider hermes-opencode-acp --model opencode-default
```

OpenCode uses the working directory passed to the client (otherwise the Hermes process working directory), its normal configuration and the process environment. Set `HERMES_OPENCODE_ACP_COMMAND` to the OpenCode CLI's absolute executable path if several installations are available. Hermes communicates with OpenCode over stdio. OpenCode manages its own model configuration and credentials.

## Tools and permissions

Hermes receives structured tool requests reconstructed from OpenCode's text replies and applies its normal tool validation and approval flow. The bridge declines ACP permission requests and client-side filesystem and terminal requests. OpenCode's own configuration governs any native tools the CLI is already authorized to execute. The child CLI inherits environment variables available to Hermes, including credentials.

## Verification

Run `python -m unittest discover -s tests` for offline transport tests. Run `python tests/smoke_cli.py --command /path/to/opencode` for a real ACP session handshake with no chat prompt. A reported model of `opencode-default` means the server omitted model metadata.

This fork builds on [engida32/hermes-opencode-acp](https://github.com/engida32/hermes-opencode-acp) and [cryptobredda/hermes-opencode-free-acp](https://github.com/cryptobredda/hermes-opencode-free-acp). See [LICENSE](LICENSE) for attribution. The upstream verification history in [docs/VERIFICATION.md](docs/VERIFICATION.md) describes the original release.
