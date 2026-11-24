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
  warn. The plugin uses the exec form, which runs the same command as the spec's
  `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/send_trace.py`.
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
  transcript. The worker always redacts the configured secret key (and anything shaped like
  `sk-lf-...`) from any content it sends.

## 6. Known limits

- Stop does not fire when the user interrupts a turn. Its data goes out on the next Stop or
  SessionEnd.
- The first run on a long existing session sends its whole history. Progress is committed per
  batch inside the 3 s HTTP budget, so a large backlog takes a few hook runs to drain.
- A subagent whose `Agent` call has not been seen yet is held back (its offset does not move)
  until it has, except on SessionEnd. There, the subagent gets its own trace instead of being
  dropped.
