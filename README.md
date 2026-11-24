# AgentGuard for Claude Code

A Claude Code plugin that sends every Claude Code session to AgentGuard as traces. Each
session shows the user, model, input, output and cache tokens, cost, tool calls and timing.
Prompt and response text is sent only if you turn it on.

## Install

In Claude Code:

```
/plugin marketplace add actaclad/agentguard-claude-code
/plugin install agentguard@actaclad
/agentguard:setup
```

Then **restart Claude Code**. From then on, every session is sent automatically.

You need Python 3.8 or newer as `python3` on your `PATH`; macOS and most Linux systems have it.
The plugin uses only the standard library, so there is nothing to `pip install`.

## Set up your keys

`/agentguard:setup` asks for three values:

| Value | Where to find it |
|---|---|
| Host | `https://ag-dev.actaclad.com` unless you were given another one |
| Public key (`pk-lf-...`) | AgentGuard → your project → Settings → API keys |
| Secret key (`sk-lf-...`) | Shown once when the key is created |

You can enter them in two ways:

- **In your own terminal (recommended).** Setup gives you a one-line command that asks for
  the secret key with a hidden prompt, so the key never appears in the chat.
- **In the chat.** Quicker, but the secret key is then stored in that session's transcript
  on your machine and sent to the model provider.

Either way, setup checks the keys against AgentGuard before saving anything. It then adds
`AGENTGUARD_HOST`, `AGENTGUARD_PUBLIC_KEY` and `AGENTGUARD_SECRET_KEY` to the `env` block of
`~/.claude/settings.json` and leaves your other settings alone. The previous file is kept as
`~/.claude/settings.json.agentguard-backup`.

### Remove the old Docker collector setup

If you used the earlier OpenTelemetry setup (a local Docker collector, `docker-compose.yml`,
plus `CLAUDE_CODE_ENABLE_TELEMETRY` and `OTEL_*` settings), remove it. Otherwise every session
appears in AgentGuard twice.

- Setup finds the `OTEL_*` and `CLAUDE_CODE_ENABLE_TELEMETRY` entries in
  `~/.claude/settings.json` and offers to remove them.
- If they are set in your shell profile (`~/.zshrc`, `~/.bashrc`), delete them there.
- Stop and remove the collector: run `docker compose down` in its folder.

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
never sent. Anything that looks like an AgentGuard secret key (`sk-lf-...`) is replaced with
`[REDACTED]` before sending.

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
| `AGENTGUARD_DEBUG` | `false` | Send in the foreground and print what happens (see below) |

## How it works

The plugin registers a hook on `Stop` (each time Claude finishes answering) and on
`SessionEnd`. The hook starts a background process and returns at once, so Claude Code never
waits for the network. The background process:

1. reads only the transcript lines added since its last run (it saves its place per session
   in `~/.claude/agentguard/state.json`);
2. turns them into traces, generations and spans, with ids derived from the transcript's own
   ids, so sending the same data twice updates it instead of duplicating it;
3. sends them in batches, spending at most 3 seconds on the network in total.

If AgentGuard cannot be reached or refuses the keys, nothing is lost. The saved place does not
move, and the same data is sent again after the next answer. Errors go to
`~/.claude/agentguard/error.log` (1 MB, then rotated). Nothing is ever shown in Claude Code. The
secret key is never written to the state file or the log.

## Troubleshooting

Run `/agentguard:status`. It shows:

- whether the keys are set (masked),
- when data was last sent and whether it worked,
- the last lines of `error.log`,
- any old OpenTelemetry settings.

| Status says | Do this |
|---|---|
| Keys are not set | Run `/agentguard:setup` |
| Keys are in settings.json but not in this session | Restart Claude Code |
| `FAILED (HTTP 401)` or `403` | The keys are wrong or for another host. Run `/agentguard:setup` again. |
| `FAILED: ... timed out` / connection refused | AgentGuard is unreachable from this machine (VPN, proxy, wrong host). Data waits and is sent later. |
| `time budget used up` | A long backlog, for example the first run on a long session. It drains over the next few answers. |
| `Last send: never` | Data goes out when an answer finishes. Ask Claude something, then check again. |

For a closer look, set `"AGENTGUARD_DEBUG": "true"` in the settings `env` block and restart.
Each send then runs in the foreground and prints to Claude Code's hook output (`claude --debug`
or verbose mode). Turn it off afterwards, because in debug mode each answer waits for the send.

You can also run the script yourself:

```
python3 ~/.claude/plugins/cache/actaclad/agentguard/<version>/scripts/send_trace.py --status
```

## Uninstall

```
/plugin uninstall agentguard@actaclad
/plugin marketplace remove actaclad
```

Then remove `AGENTGUARD_HOST`, `AGENTGUARD_PUBLIC_KEY` and `AGENTGUARD_SECRET_KEY` from the
`env` block of `~/.claude/settings.json`, and delete `~/.claude/agentguard/` (state and error
log).

## Development

```
python3 -m unittest discover -s tests -v       # all tests, standard library only
python3 tests/fixtures/build_fixtures.py       # rebuild the anonymized transcript fixtures
python3 tests/fake_server.py 18765             # a local fake AgentGuard that prints what it receives
claude plugin validate .                       # check the marketplace and plugin manifests
```

To try a local checkout: `/plugin marketplace add ./agentguard-claude-code`, then
`/plugin install agentguard@actaclad`. Alternatively, start Claude Code with
`claude --plugin-dir ./agentguard-claude-code/plugins/agentguard` to load it for one session
without installing.

`NOTES.md` records how the transcript format, hook inputs and ingestion API were checked. To
change which usage keys are sent (they must match AgentGuard's model price table), edit
`USAGE_KEYS` and `SPLIT_CACHE_CREATION_BY_TTL` at the top of `send_trace.py`.
