# Release verification

## 0.5.0 working tree, 2026-11-19

The user reported that Mattermost worked after commit `b28d035`. Profile used for that report: unconfirmed. Installed plugin revision: unconfirmed. The earlier Windows image-follow-up experiment used `mm-luna-lab`. The working tree was tested separately without replacing that profile or installing a sign-in task.

- All 117 offline tests passed. Hermes plugin doctor confirmed manifest 0.5.0, import, runtime discovery and provider registration.
- Real ACP handshakes and `tests/smoke_cli.py --list-models` succeeded on 1.19.6 and 2.0.32. Both reported bridge version 0.5.0.
- Both CLIs returned JSON-RPC code `-32602`, the exact missing session ID in `data.sessionId`, and the same missing-session message for an unknown ID. Only this observed pre-output response permits replay on a fresh session.
- Two consecutive prompts in one session showed per-prompt output counts dropping from 16 to 5 (1.19.6) and from 17 to 5 (2.0.32). Both reported cached reads separately from input on the second call. For one 2.0.32 response, 5,014 input + 2,048 cached input + 6 output = 7,068 total. Hermes normalized this to 7,062 prompt tokens. Its compressor stayed below a test threshold of 7,063 and triggered at that threshold. The reasoning breakdown and correction path were checked using synthetic usage.
- With the installed Hermes Python and the checkout on `PYTHONPATH`, `python tools/eval_compaction.py` runs a deterministic synthetic handoff through the real Hermes compressor and tests it with the OpenCode-selected examiner. To compare another candidate, use `--profile-compression` with an isolated profile configured for that candidate and keep the examiner's OpenCode configuration fixed. Exact-ID grading and questions stay fixed. A failed compaction or facts outside the archived region invalidate the run.

| Luna compression / Luna examiner | Retained context (estimated tokens) | Constraint, reason, failed trial, next ticket | Correct follow-up ticket |
|---|---:|---|---|
| Uncompressed control | 38,039 | 4/4 | Yes |
| Compressed, no history search | 5,819 | 4/4 | Yes |
| Compressed, with history search | 5,819 before adding search excerpts | 4/4 | Yes |

This single synthetic case measures recall of operational identifiers. Incremental output remains deferred while the bridge validates complete tool envelopes and native tool events.

## Hermes OpenCode bridge

Checked on 2026-11-19 on native Windows 11 with Python 3.13.5 and Hermes 0.23.2. OpenCode 1.19.6 was the global install. OpenCode 2.0.32 was installed separately with `npm install --prefix`. Both used the OpenCode-selected model `openai/gpt-6-luna`.

### Offline

- `python -m unittest discover -s tests`: 113 tests passed.
- `hermes plugins doctor`: manifest, import and registration of provider `hermes-opencode-acp` passed.

### Real OpenCode, both versions

| Check | 1.19.6 | 2.0.32 |
|---|---|---|
| Startup with no bridge variables | Started with `acp --pure`. Port 4885 was already in use, so the first attempt failed. | Started with `acp` after `--port` and `--pure` were rejected |
| `hermes-relay` from the V1 config | Offered and selected automatically | Converted to the 2.x form, offered and selected automatically |
| Relay permissions | Final rule `* * deny` | Final rule `* * deny` |
| `tests/e2e_tool_roundtrip.py` | Passed | Passed |

The live check sends two prompts. The model requested the synthetic remote tool, then answered with the code from the supplied tool result. No tool ran on the host.

### Configuration findings

- OpenCode 1.19.6 rejects the whole config when the 2.x-only `agents`/`permissions` keys are present. OpenCode 2.0.32 accepts the V1 `agent`/`prompt`/`permission` keys and converts them. `opencode/hermes-relay.jsonc` therefore uses V1 keys.
- On 2.0.32, a one-line prompt cost 14,559 input tokens with the built-in `build` agent and 234 with `hermes-relay`.
- The OpenCode free model `opencode/tidepool-alpha-free` returned `provider.quota` for every custom or modified agent, while unmodified built-in agents succeeded in the same minute. Use a model that accepts custom agents.

### Not verified

- A full Hermes chat in the default profile. Its gateway was started before the plugin and environment changes and had live sessions, so it was not restarted.
- The company OpenCode deployment. This PC used a personal OpenAI login.
- Image forwarding and attachment recall with a real model after their revision. The offline suite covers the capability check, note parsing, signatures and the sync/async contract. The Mattermost recheck is described in [MATTERMOST-LAB.md](MATTERMOST-LAB.md).

## Historical results from upstream

The following sections refer to earlier implementations and model-selection behavior.

## v0.4.0 — cross-platform release

Verified on **2026-10-31**, including native GitHub Actions results for runtime commit `e0e1e03d99c8e8ebec17d7a468463bed04ec6fc5`. Only documentation was changed after that runtime verification.

### Actual local results

- **Linux / Python 3.14.7:** `python -m unittest discover -s tests -q` collected **75 tests**, returned **OK**, and explicitly skipped the two Windows-only runtime tests.
- **Windows CPython 3.11.9 x64 under Wine 11.20:** the same suite ran **75 tests**, returned **OK**, and skipped none. This executes the Windows code paths, real `cmd.exe` `.cmd`/`.bat` wrappers, Unicode/metacharacter paths, process creation suspended until job assignment, cancellation/timeout cleanup and a real child-process cleanup check. The unrelated control process remained alive. Wine is not a native Windows host; do not describe this as native-host verification.
- **Real Linux OpenCode 2.1.2:** `python tests/smoke_cli.py` initialized ACP, created a session, discovered the seven curated free IDs, and confirmed Build plus a free-model selection. It returned `ok: true`, `bridge_version: "0.4.0"`, `inference_requested: false`.
- **Real Windows OpenCode 2.1.2 x64 under Wine:** the identical smoke script negotiated the same selections using the official Windows executable. Its npm package's SHA-512 integrity was checked against registry metadata before execution. The probe used an isolated Windows vendor home/config/cache. It returned `ok: true`, `platform: "win32"`, `bridge_version: "0.4.0"`, `inference_requested: false`.
- **Hermes validation and doctor:** validation returned `ok: true`, `warnings: []`, a safe scan and no core overrides; doctor passed runtime discovery/import/registration for version 0.4.0. Isolation remains `in_process`, `host_ready: false`, as documented.
- **Diff/syntax checks:** `git diff --check` and `python -m compileall -q .` passed.

An intermediate README sentence caused a negation-related `context_exfil` false positive. It was clarified with the exact protocol boundary (`session/prompt` is never called by the smoke script); no scanner or runtime permission rule was disabled.

No model inference was requested for these new platform checks. Offline responses are explicitly synthetic. The real CLI checks test actual executable launch, stdio, ACP and session configuration, not a fabricated model answer or live Hermes file-tool execution.

### macOS and native-host CI

Completed [GitHub Actions run 41155884768](https://github.com/cryptobredda/hermes-opencode-free-acp/actions/runs/41155884768) passed **all nine jobs** on the published runtime commit above:

| Native runner | Python 3.11 suite | Python 3.14 suite | Real OpenCode 2.1.2 ACP/config check |
| --- | --- | --- | --- |
| Linux (`ubuntu-latest`) | OK; two Windows-only skips | OK; two Windows-only skips | OK; `platform: linux` |
| macOS (`macos-latest`) | OK; two Windows-only skips | OK; two Windows-only skips | OK; `platform: darwin` |
| Windows (`windows-latest`) | OK; no skips | OK; no skips | OK; `platform: win32` |

Every offline job collected 75 tests. The Windows jobs exercised actual batch launchers and verified descendant cleanup while leaving an unrelated process alive. The real CLI jobs installed the pinned vendor package in job-local directories, confirmed Build/free-model selection and returned `inference_requested: false`. The Windows CLI job used npm's actual `.bin/opencode.cmd`; the POSIX npm symlinks resolved to the installed native executable. No native macOS or Windows live model inference or end-to-end Hermes file-tool execution is claimed here.

For historical context only, the published v0.3.0 [CI run 40243746587](https://github.com/cryptobredda/hermes-opencode-free-acp/actions/runs/40243746587) passed its offline matrix on all three OSes. That result must not be attributed to the new v0.4.0 code.

### Catalog/publication status

The original [catalog PR #144713](https://github.com/NousResearch/hermes-agent/pull/144713) merged on 2026-10-29 with v0.3.0 scoped to Linux. Source publication does not change the external platform gate: an upstream SHA-bump PR must pin this release's exact commit, set version `0.4.0` and `platforms: [linux, macos, windows]`, and preserve accurate verification/known-issue disclosures. Catalog approval is separate from source publication and is not vendor access authorization.

---

## v0.3.0 — historical published-release verification

Performed on 2026-10-25 on Linux with Hermes `v0.22.2+1376.g18c525b`, OpenCode `v2.1.2`, Python `3.14.7`, and OpenAI SDK `2.24.0` in Hermes (the plugin itself does not import the SDK).

## Offline suite

`python -m unittest discover -s tests -v`

Actual result: **47 tests passed**. These tests use an explicitly synthetic ACP stdio server; they neither perform inference nor require credentials, Hermes or OpenCode. CI is configured for Python 3.11/3.14 across Linux/macOS/Windows. Check the repository's Actions page for actual CI status; this local report does not claim those remote jobs passed before they ran.

## Hermes admission/runtime checks

Commands actually exercised:

- `hermes plugins validate /path/to/plugin --install-deps --json`
- `hermes plugins doctor /path/to/plugin --ci`

The validator returned `ok: true`, every check passed, `warnings: []`, a safe security scan, and no runtime core overrides. Runtime discovery/import/provider registration passed. Isolation classification was `in_process`, `host_ready: false`; this limitation is disclosed, not treated as isolated-host support.

## Live model transport

A direct instance of the release transport discovered the intersection of the curated IDs with OpenCode's real ACP model catalog. A live DeepSeek V4.1 Flash Free call returned the requested marker `PUBLIC_ACP_RELEASE_OK`. This was an actual OpenCode CLI request, not a mocked response.

## Live Hermes GUI-source tool loop, including resumed chats

A second isolated gateway was refused because the installed Hermes permits one gateway per host. The existing gateway was **not** stopped or restarted. Instead, a temporary, separately named model-provider alias loaded this release's transport on the existing gateway without replacing the user's working provider. Synthetic sessions used GUI source, `--ignore-rules`, and the file toolset; no real user memory or project instructions were sent for testing.

Both `opencode/deepseek-v4.1-flash-free` and `opencode/glm-5.3-flash-free`:

1. Emitted a real Hermes `read_file` request for a harmless scratch fixture.
2. Received a successful Hermes `tool_result` with that fixture's actual content.
3. Returned exactly `PUBLIC_PLUGIN_REAL_TOOL_PROOF_8C73A921`.
4. Were resumed on the same running gateway after the final transport revision was loaded under a fresh test-module namespace.
5. Issued a new real `read_file` call for a different fixture and returned exactly `PUBLIC_PLUGIN_FINAL_TOOL_PROOF_C49B28E1`.

The final proof was based on `tool_use`/`tool_result` events and exact file contents, not a model saying it had read a file. The tests exercise replayed tool history and an existing GUI session, not only a fresh command-line worker.

Only these two models and Linux live integration are claimed as verified. Other curated models are discoverable, not individually inference-tested. Token counters were displayed as zero by the frontend because usage is unknown; this is not evidence of zero tokens or a billing guarantee.

## Disclosure review

README.md documents the real CLI/network/data path, normal vendor configuration and local records, environment allowlisting, subprocess cleanup, no credential-store access by plugin code, tool/approval boundaries, non-sandbox status, limitations, terms, and distinction from the catalog's direct-HTTP OpenCode provider. No live inputs, vendor credentials, private account state or diagnostic logs are bundled in this repository.
