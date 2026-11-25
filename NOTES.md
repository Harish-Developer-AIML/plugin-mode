# Implementation notes: verified findings

Checked on 2026-11-24 against Claude Code 2.1.308, the docs at code.claude.com, 25 real
transcripts (plus 10 subagent transcripts) under `~/.claude/projects/`, and the AgentGuard
source (`packages/shared/src/server/ingestion/types.ts`,
`worker/src/constants/default-model-prices.json`). Nothing below is from memory alone.

## 1. Claude Code plugin format

Sources: `plugins/create-marketplace.md`, `plugins/marketplace-reference.md`,
`plugins/manifest-reference.md`, `plugins/components.md`, `hooks.md`, `skills.md`,
`sub-agents.md`, `settings-reference.md` (all under code.claude.com/docs/en/).

- **marketplace.json** (`.claude-plugin/marketplace.json` at the repo root) requires `name`,
  `owner` (with `owner.name`) and `plugins[]`. Each plugin entry requires `name` and `source`.
  A relative source must start with `./` and resolve inside the repo.
- **`/plugin install agentguard@actaclad`**: the part after `@` is the marketplace's `name`
  field, not the GitHub owner or repo. So marketplace.json must say `"name": "actaclad"`.
  `actaclad` and `agentguard` are not reserved names.
- **plugin.json**: `name` is the only required key. `hooks/hooks.json`, `commands/` and
  `skills/` at the plugin root are found automatically, with no manifest key. A `version`
  in plugin.json overrides the one in the marketplace entry. Keep both plugin names the same.
- **hooks.json**: `{"hooks": {"Stop": [{"hooks": [{"type": "command", ...}]}]}}`. Stop
  and SessionEnd take no matcher. `timeout` is in seconds (default 600). `${CLAUDE_PLUGIN_ROOT}`
  is expanded in `command` and `args`. The exec form (`"command": "python3", "args": [...]`)
  is preferred, because an unquoted variable in the shell form makes `claude plugin validate`
  warn. ~~The plugin uses the exec form~~ Superseded in section 8: exec form cannot be portable
  to Windows, so the hooks now use one shell-form command that runs in sh and in PowerShell.
- **Hook stdin.** Common fields: `session_id`, `prompt_id`, `transcript_path`, `cwd`,
  `permission_mode`, `effort`, `hook_event_name`. Stop adds `stop_hook_active`,
  `last_assistant_message`, `background_tasks`. SessionEnd adds `reason` (`clear`,
  `resume`, `logout`, `prompt_input_exit`, `other`).
- **Exit codes.** On Stop, exit 2 *forces Claude to keep working*, and any other non-zero
  code shows a "hook error" notice. The script must always exit 0.
- **Blocking.** Hooks block Claude until they finish. SessionEnd hooks share a
  **1.5 second budget**, and a timeout set by a plugin does not raise it. A hook that hits
  its timeout is cancelled.
  **Consequence:** the hook process only reads stdin, starts a detached worker process and
  exits 0, typically in tens of milliseconds. The worker does the parsing and the network
  call with nobody waiting on it. This meets "no visible delay" even when AgentGuard is down,
  and it fits the SessionEnd budget. (`"async": true` exists for command hooks, but async
  hooks are killed at teardown in `-p` mode, so the plugin detaches by itself instead.)
- **Environment.** Settings `env` applies "for every session and for the subprocesses Claude
  Code starts from it", and hook processes inherit that environment. Keys written to
  `~/.claude/settings.json` `env` therefore reach the hook after a restart.
- **Commands vs skills.** The docs call `commands/` "the older format" and recommend `skills/`.
  They also conflict on whether `${CLAUDE_PLUGIN_ROOT}` is substituted in `commands/*.md`.
  **Decision (confirmed with the user):** `skills/setup/SKILL.md` and `skills/status/SKILL.md`,
  which give the same `/agentguard:setup` and `/agentguard:status` names.
- **Subagents.** Transcripts are at
  `~/.claude/projects/<project>/<session_id>/subagents/agent-<agentId>.jsonl`, with
  `agent-<agentId>.meta.json` beside them. A `SubagentStop` hook exists, but it is not needed:
  the worker scans the subagents folder on every Stop and SessionEnd.

## 2. Transcript structure (observed)

One JSON object per line, append-only. The main file is `<session_id>.jsonl`.

- **Entry types seen:** `user`, `assistant`, `attachment`, `system`, `queue-operation`,
  `file-history-snapshot`, `file-history-delta`, `last-prompt`, `ai-title`, `mode`,
  `session-owner`, `cost-state`, `tools-used`, `api-request`, `api-request-blob`,
  `api-request-shape`, `atis-latch`, `isolation-latch`. Only `user` and `assistant` matter
  here. Everything else is ignored, and so must any unknown future type be.
- **Common fields** on `user`/`assistant`/`attachment`/`system`: `uuid`, `parentUuid`
  (null at a root), `timestamp` (ISO 8601 UTC with `Z`, ms), `sessionId`, `cwd`, `gitBranch`,
  `version` (Claude Code version), `entrypoint`, `isSidechain`, `userType`.
- **Assistant entries:** `message.{id, model, role, content[], stop_reason, usage}`, plus
  `requestId`. **Each content block is written as its own line** (`thinking`, `text` and
  `tool_use` lines in sequence, chained by `parentUuid`). All lines of one response share
  `message.id` and `requestId`, and **repeat the same `usage` on every line**: 4,300 of
  4,300 split messages had identical usage across their lines (up to 10 lines each).
  **Usage is counted once per `message.id`**, as one generation per `message.id`.
- **`message.usage` keys:** `input_tokens` (excludes cached tokens), `output_tokens`,
  `cache_read_input_tokens`, `cache_creation_input_tokens`,
  `cache_creation.{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}`, `service_tier`,
  `speed` (`standard`/`fast`), `server_tool_use`, `output_tokens_details`, `inference_geo`,
  `iterations`.
- **Models seen:** `claude-opus-5-5`, `claude-haiku-5-5` and `<synthetic>`. `<synthetic>`
  marks locally generated messages such as API-error notices, with zero usage and no real
  request. These are not sent as generations.
- **`tool_use` block:** `{type, id: "toolu_...", name, input}` inside an assistant line.
- **`tool_result`:** a `user` entry whose `message.content` is a list with
  `{type: "tool_result", tool_use_id, content, is_error?}`. The same entry carries
  `toolUseResult` (tool-specific dict or string) and `sourceToolAssistantUUID`.
  `is_error` is missing on about a quarter of results, so missing means "not an error".
- **Human prompts:** `user` entries whose content is a string or a list of `text`/`image`
  blocks with no `tool_result`, and which have no `isMeta`, `isCompactSummary` or
  `isVisibleInTranscriptOnly`. They carry `promptId` and `turnOrigin: "human"`. Other
  non-tool user entries, recognised by their leading text or `turnOrigin`:
  - `[Request interrupted by user...]`: an interruption marker, not a new turn.
  - `<task-notification>` (`turnOrigin: "task_notification"`): a background task finished
    and the model is woken. It starts a turn, tagged by origin.
  - `<command-name>`: a slash command. It starts a turn, tagged as a command. Commands that
    run locally make no model call.
  - `<local-command-stdout>`, `<local-command-caveat>`, `<bash-input>`, `<bash-stdout>`:
    local output, not a turn.
  - `isCompactSummary` ("This session is being continued..."): a compaction artefact, not a
    turn.
- **Subagents.** Subagent transcripts are separate files (above). All their lines have
  `isSidechain: true` and `agentId`. The first line is a `user` entry with
  `parentUuid: null` (the agent's prompt). Their `sessionId` is the parent session's.
  `agent-<id>.meta.json` has `agentType`, `description`, `model`, `spawnDepth` and
  **`toolUseId`, the `Agent` tool call in the parent transcript that started it**. That
  is how subagent activity is nested. The parent's tool result also has
  `toolUseResult.agentId`. A subagent can start its own subagents (`spawnDepth > 1`), and
  then `toolUseId` points into another subagent's file. The main transcript in current
  versions contains no `isSidechain: true` lines. Older versions wrote sidechains inline,
  so the parser handles that too.
- **Timing.** Assistant lines are written as each block finishes streaming. There is no
  request start time on the line, so a generation's `startTime` is the timestamp of the
  entry that triggered it: the prompt or tool result it answers, which is the previous
  user entry in file order. `endTime` is the last line of the message, and
  `completionStartTime` is its first line. A tool span runs from its `tool_use` line to its
  `tool_result` line.

## 3. Ingestion API (verified in the AgentGuard source, standard Langfuse)

- `POST {host}/api/public/ingestion`, HTTP Basic auth `public_key:secret_key`, body
  `{"batch": [{"id", "timestamp", "type", "body"}], "metadata"?}`. The response is 207 with
  `{"successes": [...], "errors": [{id, status, message}]}`. The size limit is 4.5 MB per
  request (`sizeLimit: "4.5mb"`). The plugin splits batches at 3 MB and 500 events.
- `trace-create` body: `id, timestamp, name, userId, sessionId, input, output, metadata,
  release, version, tags, environment, public`. Sending it again with the same `id` updates
  the trace.
- `span-create`/`span-update`: `id, traceId, parentObservationId, name, startTime, endTime,
  input, output, metadata, level (DEBUG|DEFAULT|WARNING|ERROR), statusMessage, version`.
- `generation-create` adds `model, modelParameters, completionStartTime, usageDetails`
  (a record of string to int) and `costDetails`.
- AgentGuard extends the event types (`agent-create`, `tool-create`, ...). The plugin sticks
  to `trace-create`, `generation-create` and `span-create`, as the spec asks.
- Environment names starting with `agentguard`/`langfuse` are stripped on the server. The
  plugin sends no environment, so traces land in `default`.
- **The envelope `id`** is a hash of the event type and body. Resending unchanged data
  sends the same envelope, and changed data (for example a span that now has its result)
  gets a new one. The **body `id`s** are deterministic (section 4), so the server upserts
  them.
- **Results:** 2xx with no 5xx entries in `errors` is a success. 4xx entries are logged and
  never retried, because the same data would fail again. Any 5xx, network error, timeout,
  401 or 403 is a failure, and the offset does not move.

## 4. Mapping and pricing

| Claude Code | AgentGuard | Deterministic id |
|---|---|---|
| `session_id` | `sessionId` | (same value) |
| human prompt (or task notification / slash command that led to a model call) | trace `claude_code.turn` | `sha256(session_id + ":trace:" + prompt uuid)` as a 32-hex id |
| assistant `message.id` | generation `claude_code.llm_call` | `sha256(session_id + ":gen:" + message.id)` |
| `tool_use` / `tool_result` | span named after the tool, child of its generation | `sha256(session_id + ":tool:" + tool_use_id)` |
| subagent file | its generations and spans nest under the `Agent` span that started it, in that turn's trace, with metadata `sidechain: true`, `agent_id`, `agent_type` | same rules |

- **Price table check.** `default-model-prices.json` has `claude-opus-5-5`,
  `claude-sonnet-5-5` and `claude-haiku-5-5` with the keys `input`, `output`,
  `cache_read_input_tokens`, `cache_creation_input_tokens`, `input_cache_creation_5m` and
  `input_cache_creation_1h`.
- **Finding that changes the spec's key list.** Claude Code writes its prompt cache with the
  **1-hour** TTL: 16.7M of 16.7M cache-write tokens in the sampled transcripts were
  `ephemeral_1h`. AgentGuard prices `cache_creation_input_tokens` at the 5-minute rate
  ($5/M on opus-5-5) and `input_cache_creation_1h` at $8/M. With the spec's key alone,
  cache-write cost would be **37.5% too low**. By default the plugin therefore sends cache
  writes split as `input_cache_creation_5m` and `input_cache_creation_1h` when the breakdown
  is present, and falls back to `cache_creation_input_tokens` when it is not. It never sends
  both, so nothing is counted twice. This is switched by one setting (`USAGE_KEYS` /
  `SPLIT_CACHE_CREATION_BY_TTL` at the top of `send_trace.py`).
- **Fast mode.** The price table has a "Fast mode" tier selected by
  `model_parameters.speed == "fast"`. The plugin sends `modelParameters.speed` from
  `usage.speed` so the tier applies.

## 5. Decisions made with the user

- Slash commands are `skills/` (see section 1).
- Setup offers two ways to give the keys: the default is a command the user runs in their own
  terminal, which asks for the secret with a hidden prompt, so the secret never enters the
  chat. Pasting the keys into chat also works, but the secret is then in the session
  transcript. The worker always redacts the configured secret key, and anything shaped like an
  AgentGuard key (see section 7), from any content it sends.

## 6. Known limits

- Stop does not fire when the user interrupts a turn. Its data goes out on the next Stop or
  SessionEnd.
- The first run on a long existing session sends its whole history. Progress is committed per
  batch inside the HTTP time budget (`AGENTGUARD_TIMEOUT_SECONDS`, 15 by default since 0.1.1), so a large backlog takes a few hook runs to drain.
- A subagent whose `Agent` call has not been seen yet is held back (its offset does not move)
  until it has, except on SessionEnd. There, the subagent gets its own trace instead of being
  dropped.

## 7. Changes in this round (still 0.1.0, not released)

- **Large hook input broke the background worker (bug).** The hook handed its whole stdin JSON
  to the worker in the `AGENTGUARD_HOOK_PAYLOAD` environment variable. Stop's
  `last_assistant_message` can be very large, and environment size is limited (128 KB per
  variable on Linux, about 1 MB in total on macOS, about 32 KB on Windows). `Popen` then failed
  with "Argument list too long" and that Stop sent nothing. This was reproduced here with a
  2 MB message. Now only the four fields the worker reads (`session_id`, `transcript_path`,
  `hook_event_name`, `cwd`) are passed, in both background and sync/debug mode. An `OSError`
  from starting the worker is written to `error.log`, and the hook still exits 0. Tests:
  500 KB and 2 MB messages through the real spawn path, the exact payload in the worker's
  environment, sync mode, and the `OSError` path.
- **Repository path.** README and `plugin.json` now point to
  `https://github.com/Harish-Developer-AIML/plugin-mode`. The marketplace name stays
  `actaclad`, so the install command is unchanged: in `agentguard@actaclad`, the part after
  `@` is the marketplace name, not the GitHub owner. The local-checkout example uses the
  clone's folder name, `plugin-mode`.
- **Key redaction.** AgentGuard generates keys as `pk-ag-<uuid>` and `sk-ag-<uuid>`
  (`generateKeySet` in `packages/shared/src/server/auth/apiKeys.ts`). `sk-lf-` and `pk-lf-`
  appear only in its tests. The pattern is now `\b(?:sk|pk)-(?:ag|lf)-[A-Za-z0-9_-]{8,}`, so it
  covers secret and public keys in both formats and is applied to `error.log`, debug output and
  any content sent. It now requires a word boundary before the key, so text such as
  `disk-lf-...` is no longer cut. The configured secret is still also redacted by exact match.
  Setup prompts and docs now show the `-ag-` format.
- **Windows.** The README says "Supported: macOS and Linux. Windows is not supported yet."
  (the hooks call `python3`). The Windows code paths are left in place but untested.
- **Test leaks.** The concurrent-runs test never closed its subprocess pipes (8
  `ResourceWarning`s). It now reads the hook input from a file and collects output with
  `communicate()`, which closes every pipe and also works on Python 3.12. A warning raised during garbage collection
  never fails a test, even with `-W error`, so the test module now collects them with
  `sys.unraisablehook` and fails the run if there are any.

## 8. Native Windows (checked 2026-11-24 against code.claude.com/docs/en/{hooks,skills,setup,permissions,tools-reference,plugins/components}.md)

### What the docs say

- **Git Bash is optional on native Windows.** (setup.md) "Installing Git for Windows is optional
  ... Without Git for Windows, Claude Code runs shell commands via the PowerShell tool." WSL
  setups behave like Linux.
- **Shell-form hooks** (no `args`): "The `command` string is passed to a shell: `sh -c` on
  macOS and Linux, Git Bash on Windows, or PowerShell when Git Bash isn't installed." The
  per-hook `shell` field ("bash" or "powershell") is not tied to the OS, so it can't choose
  per platform. For PowerShell, "Claude Code auto-detects `pwsh.exe` ... and falls back to
  `powershell.exe` for Windows PowerShell 5.1."
- **Exec-form hooks** (`args` set): `command` "is resolved as an executable on PATH and spawned
  directly", and "on Windows, exec form requires `command` to resolve to a real executable such
  as a `.exe`". No executable exists on all three systems (`python3` is not on Windows,
  `powershell` is not on macOS/Linux). A hook that cannot start is a non-blocking error, which
  shows a "hook error" notice. **So exec form cannot be portable, and the previous
  `python3` exec-form hook either failed on Windows or ran the Microsoft Store stub.**
- **`${CLAUDE_PLUGIN_ROOT}`** is substituted into `command` (and `args`) before any shell sees
  it, and "every hook process receives `CLAUDE_PLUGIN_ROOT` ... in its environment"
  (plugins/components.md).
- **Skill `!` commands** run through the Bash tool when bash is available (Git Bash on Windows),
  otherwise through the PowerShell tool (skills.md, "How injected commands run"). "A failed
  command aborts the entire skill invocation." Injected commands "never prompt": outside auto
  mode, one that `allowed-tools` doesn't allow aborts the skill. `PowerShell(...)` permission
  rules have the same shape as `Bash(...)` rules (permissions.md).
- **Not documented:** the exact flags Claude Code passes to PowerShell for a hook (`-Command`
  text versus a temporary file). The design below assumes the command text is parsed as
  PowerShell source, which is what `-Command`, `-EncodedCommand` and the docs' "PowerShell
  started no program" troubleshooting all imply. The Windows test on a real machine confirms it.

### Design

- **Hook command:** one shell-form command, the same for Stop and SessionEnd, valid in both
  sh and PowerShell:

  ```
  echo `# <#` >/dev/null
  sh "${CLAUDE_PLUGIN_ROOT}/scripts/agentguard.sh"; exit 0
  #> > $null; try { & ([scriptblock]::Create([IO.File]::ReadAllText((Join-Path (Join-Path $env:CLAUDE_PLUGIN_ROOT 'scripts') 'agentguard.ps1')))) } catch { }; exit 0
  ```

  In sh, the backticks hold only a comment, so line 1 is an empty `echo`; line 2 runs the sh
  launcher; line 3 is a comment. In PowerShell, `` `# `` is an escaped `#`, `<#` opens a block
  comment that hides line 2, `#>` closes it, `> $null` silences the `echo`, and the rest loads
  the PowerShell launcher. Loading the file as a script block means PowerShell's execution
  policy does not apply. A failure (for example Constrained Language Mode) is caught, and
  the hook exits 0. Tested under sh, dash, bash and zsh locally; in CI also under Git Bash,
  pwsh and Windows PowerShell 5.1, including with no Python on PATH.
- **Launchers:** `scripts/agentguard.sh` (POSIX sh: macOS, Linux, WSL, Git Bash) and
  `scripts/agentguard.ps1` (PowerShell 5.1 and 7). They follow the same rules, so with two
  possible shells on Windows there is one launcher per shell, not one per OS.
  - They try `python3`, `python`, then `py -3`, and take the first that passes
    `sys.version_info >= (3, 8)`. The version check runs with stdin closed, so it never
    consumes the hook input.
  - **Microsoft Store stubs:** a candidate resolved under `...\WindowsApps\` is used only if a
    `PythonSoftwareFoundation.*` folder sits next to it, which is how Store-installed Python
    appears. Otherwise it is skipped **without being run**, because running the stub opens
    the Store.
  - **Hook mode** (no arguments): stdin is passed through to Python. The PowerShell launcher
    reads it as bytes and pipes it as UTF-8, because 5.1 would otherwise re-encode it as
    ASCII. Nothing is printed, and the exit code is 0 even when no Python is found.
  - **With arguments** (status, setup): when no Python is found they print how to install it
    for each system. `--status` then exits 0 and everything else exits 1.
  - The sh launcher uses only shell builtins (`command -v`, `case`, `printf`), so it works
    with a bare PATH.
- **Skills:** the `!` injections are gone, because a `sh` command fails in the PowerShell
  tool and that aborts the skill. Instead, the skill tells Claude to run the matching
  launcher with the shell tool it has. Both forms are pre-approved in `allowed-tools`:
  `Bash(sh ".../agentguard.sh" --status)` and
  `PowerShell(powershell -NoProfile -ExecutionPolicy Bypass -File ".../agentguard.ps1" --status)`.
  Checked on macOS: `/agentguard:status` in `claude -p` ran the launcher through the Bash rule
  with no prompt.
- **Line endings:** `.gitattributes` forces LF. Git on Windows usually checks out CRLF, and a
  CRLF `agentguard.sh` fails in Git Bash. This also covers each user's plugin clone.

### Windows code paths in send_trace.py (reviewed and fixed)

- **Worker spawn:** `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`, plus
  `CREATE_BREAKAWAY_FROM_JOB` so the worker isn't killed with a job object Claude Code may run
  hooks in. A job that forbids breakaway makes `CreateProcess` fail, and then the spawn is
  retried without it. The std handles are DEVNULL and `close_fds=True`, so Claude Code never
  waits on inherited pipes.
- **`git config user.email`** in the console-less worker would open a console window: it now
  uses `CREATE_NO_WINDOW` and stdin DEVNULL.
- **Hook stdin** was read in text mode, which on Windows decodes with the ANSI code page and
  garbles non-ASCII paths. It is now read as bytes and decoded as UTF-8, accepting a BOM.
- **`os.replace`** fails on Windows while another process has the target open without
  delete sharing (an editor, a virus scanner, a concurrent status read). `replace_file()`
  retries for about a second. For `settings.json` there is also a last resort: overwrite in
  place, with the backup already written.
- **Status and setup output** reconfigure stdout/stderr with `errors="replace"`, so a
  character the console code page can't encode doesn't crash them.
- **Checked, no change needed:**
  - `msvcrt.locking` locks byte 0 after `seek(0)`; locking past the end of an empty file is
    allowed, and a held lock raises `OSError` at once with `LK_NBLCK`.
  - `os.chmod(0o600/0o700)` only toggles the read-only flag on Windows and keeps files
    writable.
  - `~` resolves to `%USERPROFILE%` (Python 3.8+ ignores `HOME` on Windows), which is where
    Claude Code keeps `.claude`.
  - Transcript lines ending in CRLF are handled by `strip()`.

### Still to verify on a real Windows machine

- That Claude Code runs the hook command text the way the CI emulation does
  (`-NoProfile -NonInteractive -Command`), with and without Git Bash.
- A real session: install, setup (terminal and paste), status, and data in AgentGuard.

### First CI run (run 45454945602) and fixes

- Ubuntu passed on 3.8 and 3.12. The macOS jobs were cancelled by hand before running.
  Windows failed one test on both Pythons:
  `PsLauncherFileTests.test_status_and_setup_with_real_python` under **Windows PowerShell 5.1
  only** (pwsh passed). `--setup --secret-stdin` got HTTP 401: the secret arrived with a UTF-8
  BOM, which 5.1 puts in front of text it pipes to a program. `str.strip()` does not remove
  it. Every hook test, including PowerShell 5.1 hook mode, passed, because hook stdin was
  already decoded as `utf-8-sig`.
- Fix: `--secret-stdin` now reads bytes and decodes them as `utf-8-sig`, dropping any BOM.
  A new test sends a BOM and CRLF on every platform; it fails with the old code (same 401)
  and passes now. This would have hit real Windows users who chose the paste option.
- Both launchers now try **every** match on PATH, not only the first. Otherwise a Store
  shortcut earlier on PATH hid a real `python3` later on PATH. New tests cover this for both
  launchers.
- CI: job logs need a signed-in user, so failures are now also written as annotations, which
  are public, by `.github/scripts/annotate_failures.py`. The shells and Pythons found on
  PATH are written as a notice. The fake server no longer prints a traceback when a timed-out
  client hangs up (Windows `ConnectionAbortedError`). `actions/checkout@v5`; uv cache off.

## 9. Changes in 0.1.1

- **Timeout.** `HTTP_BUDGET_SECONDS = 3.0` was too short: AgentGuard's ingestion endpoint
  sometimes takes longer to answer. The data then arrived, but the plugin recorded
  `TimeoutError`, kept the offset, and re-sent the same data on every run (seen in a real
  error log: two `TimeoutError` lines). It is now `AGENTGUARD_TIMEOUT_SECONDS` (default 15,
  range 1 to 60), parsed by `parse_timeout`. An invalid value gives the default plus a warning
  that is logged once per distinct value (`state["timeout_warning"]`). Each request still gets
  only the time left in the budget. The lock wait grows with the budget (budget + 20 s), so a
  second run does not give up while the first is still within its budget. The hook's own
  `timeout` in `hooks.json` went from 10 to 90 s: otherwise Claude Code would cancel a
  debug-mode (foreground) send before the budget ends. SessionEnd's 1.5 s limit is unaffected,
  because in normal mode the hook returns at once.
- **Status reads settings.json.** Claude Code passes the `env` block of `settings.json` only
  to its own sessions, so `--status` in a terminal always said "not set". `status_config`
  takes each `AGENTGUARD_*` setting from the environment first, else from settings.json
  (read only), and status prints the source per value. `CLAUDECODE=1` (documented: set in
  every subprocess Claude Code starts) tells a Claude Code session apart from a terminal. Only
  inside a session do keys found only in settings.json mean "restart Claude Code". When
  settings.json holds a different key than the environment, status says new sessions will
  use that one. A broken settings.json gives a warning, never a crash or a write.
- **Collector detection.** `collector_advice()` first tries a 0.3 s connect to
  127.0.0.1:4318. Only if that succeeds does it run `docker ps --format` with the name, the
  ports and the `com.docker.compose.project.working_dir` label. It matches containers that
  publish 4318 on the host, port ranges included, and prints one command per Compose project:
  `cd '<dir>' && docker compose down` on POSIX, `cd '<dir>'; docker compose down` in
  PowerShell on Windows, with the path quoted for that shell. Without a label it prints
  `docker stop` plus `docker update --restart=no`. It only prints: docker is asked for `ps`
  and nothing else (a test checks the fake docker's argument log). A missing docker, docker
  errors, timeouts, or nothing on the port produce no error output. The skills tell Claude
  never to run the docker commands itself.
- **HTTP 403 hint.** A 403 used to add "(check AGENTGUARD_PUBLIC_KEY / AGENTGUARD_SECRET_KEY)".
  A real 403 said "Your organization's plan has ended": the keys were fine. 401 keeps the key
  hint; 403 now says AgentGuard refused the data and to see its message.
- **Docs.** The README install section leads with `claude plugin marketplace add` and
  `claude plugin install` from a terminal, because `/plugin` does not work in the VS Code chat
  panel. Setup runs through the launcher in the newest version folder: `ls -dt ... | head -1`
  on POSIX, `Sort-Object LastWriteTime` in PowerShell, so there is no hardcoded version. The
  POSIX form was checked against a real install. The README also gains Update, Old
  OpenTelemetry setup, new troubleshooting rows, and the timeout setting.
- **Release.** Version 0.1.1 in `plugin.json`, `marketplace.json` and `PLUGIN_VERSION`, so
  `claude plugin update` sees it. Added `LICENSE` (Apache 2.0, the canonical text, sha256
  `cfc7749b...3d30`), `"license": "Apache-2.0"` in plugin.json, and `CHANGELOG.md`.

