# AgentGuard for Claude Code

A Claude Code plugin that sends every Claude Code session to AgentGuard as traces. Each
session shows the user, model, input, output and cache tokens, cost, tool calls and timing.
Prompt and response text is sent only if you turn it on.

## Install

You need **Python 3.8 or newer** (see [Supported platforms](#supported-platforms)) and the
AgentGuard **host, public key and secret key** for your project.

### From a terminal (Claude Code CLI and VS Code extension)

Use a normal terminal for this. The `/plugin` chat commands do not work in the VS Code
extension's chat panel.

**macOS, Linux, WSL:**

```sh
claude plugin marketplace add Harish-Developer-AIML/plugin-mode
claude plugin install agentguard@actaclad
sh "$(ls -dt ~/.claude/plugins/cache/actaclad/agentguard/*/ | head -1)scripts/agentguard.sh" --setup
```

**Windows (PowerShell):**

```powershell
claude plugin marketplace add Harish-Developer-AIML/plugin-mode
claude plugin install agentguard@actaclad
$ag = Get-ChildItem "$env:USERPROFILE\.claude\plugins\cache\actaclad\agentguard" -Directory | Sort-Object LastWriteTime | Select-Object -Last 1
powershell -NoProfile -ExecutionPolicy Bypass -File "$($ag.FullName)\scripts\agentguard.ps1" --setup
```

The last command picks the newest installed version folder, so you never type a version
number. To see the folders yourself: `ls ~/.claude/plugins/cache/actaclad/agentguard/` (or
`dir "$env:USERPROFILE\.claude\plugins\cache\actaclad\agentguard"` on Windows). If you set
`CLAUDE_CONFIG_DIR`, the plugins are under that folder instead of `~/.claude`.

Setup asks for the host (press Enter to keep `https://ag-dev.actaclad.com`), the public key
and the secret key. **The secret key prompt is hidden: nothing appears while you paste or
type it.** Press Enter after pasting. Setup checks the keys against AgentGuard before saving
anything.

**Then restart Claude Code.** In VS Code, quit VS Code completely (on macOS: Cmd+Q, not just
closing the window), then start it again. On macOS, opening VS Code from a terminal with
`code .` helps: VS Code then gets the terminal's `PATH` and finds the same Python as the
terminal does. From then on, every session is sent automatically.

### Alternative: chat commands in the Claude Code CLI

In the Claude Code CLI (not the VS Code chat panel):

```
/plugin marketplace add Harish-Developer-AIML/plugin-mode
/plugin install agentguard@actaclad
/agentguard:setup
```

Then restart Claude Code.

The `@actaclad` in the install command is the marketplace's name from
`.claude-plugin/marketplace.json`, not the GitHub owner, so it stays `actaclad` wherever the
repository lives.

## Update

```sh
claude plugin update agentguard@actaclad
```

Then restart Claude Code. If it says the plugin is already up to date but a newer version
exists, refresh the marketplace first: `claude plugin marketplace update actaclad`.

## Supported platforms

**macOS, Linux, and Windows (native, or in WSL).** The plugin needs **Python 3.8 or newer**.
It uses only Python's standard library, so there is nothing to `pip install`. It looks for
`python3`, then `python`, then `py -3`, and uses the first that is 3.8 or newer.

| Platform | Python | Notes |
|---|---|---|
| macOS | Usually there already: `python3` comes with the Xcode command line tools, which git needs too. Otherwise `brew install python3` or [python.org](https://www.python.org/downloads/macos/). | |
| Linux | Usually there already. Otherwise `sudo apt install python3` (Debian, Ubuntu) or `sudo dnf install python3` (Fedora). | |
| Windows, native | Not included in Windows. Install it with `winget install Python.Python.3.12` or from [python.org](https://www.python.org/downloads/windows/), and tick **"Add python.exe to PATH"** in the installer. The `py` launcher that comes with it also works. Python from the Microsoft Store works too. | Works with or without Git for Windows. With it, the hooks run in Git Bash; without it, in PowerShell. The `python` / `python3` shortcuts Windows ships, which only open the Microsoft Store, are detected and never run. |
| Windows, WSL | Install Python inside the WSL distribution, as on Linux. | Run Claude Code inside WSL. Settings are in the Linux home folder there (`~/.claude`), not the Windows one. |

Without a suitable Python the plugin does nothing. Claude Code works normally, and the
status and setup commands say how to install Python.

On Windows, `~/.claude` below means `%USERPROFILE%\.claude`, for example
`C:\Users\you\.claude\settings.json`.

## Set up your keys

Setup (the terminal command above, or `/agentguard:setup`) asks for three values:

| Value | Where to find it |
|---|---|
| Host | `https://ag-dev.actaclad.com` unless you were given another one |
| Public key (`pk-ag-...`) | AgentGuard → your project → Settings → API keys |
| Secret key (`sk-ag-...`) | Shown once when the key is created |

`/agentguard:setup` offers two ways to enter them:

- **In your own terminal (recommended).** It gives you the terminal command above, which asks
  for the secret key with a hidden prompt, so the key never appears in the chat.
- **In the chat.** Quicker, but the secret key is then stored in that session's transcript
  on your machine and sent to the model provider.

Either way, setup checks the keys against AgentGuard before saving anything. It then adds
`AGENTGUARD_HOST`, `AGENTGUARD_PUBLIC_KEY` and `AGENTGUARD_SECRET_KEY` to the `env` block of
`~/.claude/settings.json` and leaves your other settings alone. The previous file is kept as
`~/.claude/settings.json.agentguard-backup`.

## Old OpenTelemetry setup

If you used the earlier setup, with a local Docker OpenTelemetry collector, a
`docker-compose.yml`, and `CLAUDE_CODE_ENABLE_TELEMETRY` / `OTEL_*` settings, remove it.
While it runs, every session appears in AgentGuard twice.

- **Settings:** setup finds the `OTEL_*` and `CLAUDE_CODE_ENABLE_TELEMETRY` entries in
  `~/.claude/settings.json` and offers to remove them. If they are set in your shell profile
  (`~/.zshrc`, `~/.bashrc`) or in your Windows user environment variables, delete them there.
- **The collector:** setup and status check whether something still listens on
  `127.0.0.1:4318`, the collector's port. If Docker is available, they look up the container
  and print the exact command to stop it, for example:

  ```
  WARNING: an OpenTelemetry collector is still running (container claude-telemetry-otel-collector-1). Stop it with:
    cd '/Users/you/claude-telemetry' && docker compose down
  ```

  For a container that was not started with Docker Compose, they print `docker stop <name>`
  and `docker update --restart=no <name>` instead. The plugin never stops or removes
  containers itself.
- **Run `docker compose down` only in the collector's own folder**, the one shown in that
  message, which holds the collector's `docker-compose.yml`. In any other folder it stops
  whatever project lives there instead.

## What is sent

One AgentGuard **session** per Claude Code session (same id). Inside it:

| AgentGuard | What it holds |
|---|---|
| Trace `claude_code.turn` | One per prompt you send. User id, session id, tag `claude-code`, and metadata: working directory, git branch, Claude Code version, plugin version, and what started the turn (your prompt, a slash command, or a background task finishing). |
| Generation `claude_code.llm_call` | One per model response. Model name, start and end time, and token counts: `input`, `output`, `cache_read_input_tokens`, and cache writes (`input_cache_creation_1h` / `input_cache_creation_5m`, or `cache_creation_input_tokens` when Claude Code does not break them down). AgentGuard calculates the cost from the model and these counts. |
| Span, named after the tool (`Bash`, `Read`, `Edit`, an MCP tool, ...) | One per tool call, nested under the response that made it. Start and end time, and an error level if the tool failed. |
| Subagent activity | Responses and tool calls of subagents, nested under the `Agent` tool call that started them and marked `sidechain: true` with the agent's id and type. |

**User id:** `AGENTGUARD_USER` if set, otherwise your `git config user.email`, otherwise your
OS username.

**Not sent by default:** your prompts, Claude's responses, tool inputs (commands, file paths,
edits) and tool outputs. Only names, counts, sizes and timing are sent. Claude's thinking is
never sent. Anything that looks like an AgentGuard API key (`sk-ag-...`, `pk-ag-...`, or the
older `sk-lf-...` / `pk-lf-...`) is replaced with `[REDACTED]` before sending and in the error log.

### Send prompt and response text too

Add these lines to the `env` block of `~/.claude/settings.json` and restart Claude Code:

```json
"AGENTGUARD_LOG_CONTENT": "true",
"AGENTGUARD_MAX_CONTENT_CHARS": "2000"
```

Each text field is cut to `AGENTGUARD_MAX_CONTENT_CHARS` characters (default 2000).

## Settings

All of these go in the `env` block of `~/.claude/settings.json`:

| Variable | Default | Meaning |
|---|---|---|
| `AGENTGUARD_HOST` | `https://ag-dev.actaclad.com` | AgentGuard URL |
| `AGENTGUARD_PUBLIC_KEY` | (none) | Project public key. Without both keys the plugin does nothing. |
| `AGENTGUARD_SECRET_KEY` | (none) | Project secret key |
| `AGENTGUARD_USER` | git email, then OS user | User id shown in AgentGuard |
| `AGENTGUARD_LOG_CONTENT` | `false` | Also send prompt, response and tool text |
| `AGENTGUARD_MAX_CONTENT_CHARS` | `2000` | Maximum length of each text field |
| `AGENTGUARD_TIMEOUT_SECONDS` | `15` | Total time one send may take, for all its requests together (1 to 60; another value falls back to 15 and logs a warning once). Sending happens in the background, so this never slows Claude Code down. |
| `AGENTGUARD_DEBUG` | `false` | Send in the foreground and print what happens (see below). Each answer may then wait up to `AGENTGUARD_TIMEOUT_SECONDS`. |

## How it works

The plugin registers a hook on `Stop` (each time Claude finishes answering) and on
`SessionEnd`. The hook runs a small launcher (`scripts/agentguard.sh` in sh or Git Bash,
`scripts/agentguard.ps1` in PowerShell). The launcher finds Python and starts
`scripts/send_trace.py`, which starts a background process and returns at once, so Claude
Code never waits for the network. The background process:

1. reads only the transcript lines added since its last run (it saves its place per session
   in `~/.claude/agentguard/state.json`);
2. turns them into traces, generations and spans, with ids derived from the transcript's own
   ids, so sending the same data twice updates it instead of duplicating it;
3. sends them in batches, within `AGENTGUARD_TIMEOUT_SECONDS` (15 by default) for the whole
   send.

If AgentGuard cannot be reached, is too slow, or refuses the data, nothing is lost. The saved
place does not move, and the same data is sent again after the next answer. Errors go to
`~/.claude/agentguard/error.log` (1 MB, then rotated). Nothing is ever shown in Claude Code. The
secret key is never written to the state file or the log.

## Troubleshooting

Run `/agentguard:status`, or in a terminal:

```sh
# macOS, Linux, WSL, Git Bash
sh "$(ls -dt ~/.claude/plugins/cache/actaclad/agentguard/*/ | head -1)scripts/agentguard.sh" --status
```

```powershell
# Windows PowerShell
$ag = Get-ChildItem "$env:USERPROFILE\.claude\plugins\cache\actaclad\agentguard" -Directory | Sort-Object LastWriteTime | Select-Object -Last 1
powershell -NoProfile -ExecutionPolicy Bypass -File "$($ag.FullName)\scripts\agentguard.ps1" --status
```

It shows:

- whether the keys are set (masked), and where they come from. In a terminal they come from
  `settings.json`, because Claude Code passes its `env` block only to its own sessions.
- the active timeout,
- when data was last sent and whether it worked,
- the last lines of `error.log`,
- any old OpenTelemetry settings or a still-running collector.

| Status says | Do this |
|---|---|
| `AgentGuard needs Python 3.8 or newer` | Install Python as shown under [Supported platforms](#supported-platforms), then restart Claude Code. |
| Keys are not set | Run setup (see [Install](#install)). |
| `restart Claude Code to use them` | Setup saved the keys after this session started. Restart Claude Code. |
| `FAILED (HTTP 401)` | The keys are wrong or for another host. Run setup again. |
| `FAILED (HTTP 403)` | The keys work but AgentGuard refused the data; its message says why (for example, the organization's plan has ended). Data waits and is sent once that is fixed. |
| `FAILED: TimeoutError` / `timed out` | AgentGuard answered too slowly. Nothing is lost: the data is sent again after the next answer. If it keeps happening, raise `AGENTGUARD_TIMEOUT_SECONDS` (for example to `30`) in the settings `env` block and restart. |
| `FAILED: ... ` connection refused / name not resolved | AgentGuard is unreachable from this machine (VPN, proxy, wrong host). Data waits and is sent later. |
| `time budget ... used up` | A long backlog, for example the first run on a long session. It drains over the next few answers. |
| `Last send: never` | Data goes out when an answer finishes. Ask Claude something, then check again. |
| Sessions appear twice in AgentGuard | The old OpenTelemetry setup is still active. Status shows the settings and the collector, with the exact command to stop it (see [Old OpenTelemetry setup](#old-opentelemetry-setup)). |
| Works in the terminal (`claude`) but not in VS Code | VS Code was not fully restarted, or it does not see your Python. Quit VS Code completely and start it again. On macOS, start it from a terminal with `code .`. Then run `/agentguard:status` in the VS Code chat: its `Python:` line shows which Python it uses. |

For a closer look, set `"AGENTGUARD_DEBUG": "true"` in the settings `env` block and restart.
Each send then runs in the foreground and prints to Claude Code's hook output (`claude --debug`
or verbose mode). Turn it off afterwards: in debug mode each answer waits for the send, up to
`AGENTGUARD_TIMEOUT_SECONDS`.

## Uninstall

```sh
claude plugin uninstall agentguard@actaclad
claude plugin marketplace remove actaclad
```

(or `/plugin uninstall agentguard@actaclad` and `/plugin marketplace remove actaclad` in the
Claude Code CLI). Then remove `AGENTGUARD_HOST`, `AGENTGUARD_PUBLIC_KEY` and
`AGENTGUARD_SECRET_KEY` from the `env` block of `~/.claude/settings.json`, and delete
`~/.claude/agentguard/` (state and error log).

## Development

CI (`.github/workflows/tests.yml`) runs every test on ubuntu-latest, macos-latest and
windows-latest with Python 3.8 and 3.12. That includes the hook command itself under sh, dash,
bash, Git Bash, PowerShell 7 and Windows PowerShell 5.1, wherever the runner has them. Failed
tests also appear as annotations on the run, which can be read without signing in.

```
python3 -m unittest discover -s tests -v       # all tests, standard library only
                                               # (PowerShell tests run where pwsh/powershell exist)
python3 tests/fixtures/build_fixtures.py       # rebuild the anonymized transcript fixtures
python3 tests/fake_server.py 18765             # a local fake AgentGuard that prints what it receives
claude plugin validate .                       # check the marketplace and plugin manifests
```

To try a local checkout:

```
git clone https://github.com/Harish-Developer-AIML/plugin-mode.git
claude plugin marketplace add ./plugin-mode
claude plugin install agentguard@actaclad
```

Alternatively, `claude --plugin-dir ./plugin-mode/plugins/agentguard` loads the plugin for one
session without installing it.

`NOTES.md` records how the transcript format, hook inputs and ingestion API were checked, and
`CHANGELOG.md` lists the changes per version. To change which usage keys are sent (they must
match AgentGuard's model price table), edit `USAGE_KEYS` and `SPLIT_CACHE_CREATION_BY_TTL` at
the top of `send_trace.py`.

## License

Apache License 2.0. See [LICENSE](LICENSE).
