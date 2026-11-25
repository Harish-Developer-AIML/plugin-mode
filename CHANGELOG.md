# Changelog

All notable changes to the AgentGuard plugin for Claude Code. Update an installed plugin with
`claude plugin update agentguard@actaclad`, then restart Claude Code.

## 0.1.1 (2026-11-25)

### Fixed

- **Slow AgentGuard answers no longer fail.** The fixed send budget
  (`HTTP_BUDGET_SECONDS = 3.0`) was shorter than AgentGuard's ingestion endpoint sometimes
  needs. The plugin then recorded `FAILED: TimeoutError` and sent the same data again on
  every following run. The total time for one send is now
  `AGENTGUARD_TIMEOUT_SECONDS`: 15 by default, 1 to 60 allowed. An invalid value falls back to
  15 and logs a warning once. Sending still happens in the background, so Claude Code does not
  wait for it.
- **Status in a terminal no longer says the keys are missing.** Claude Code passes the `env`
  block of `settings.json` only to its own sessions. When the keys are not in the
  environment, status now reads them from `~/.claude/settings.json` (read only) and says where
  each one came from. Environment values still take precedence.
- **HTTP 403 is no longer reported as a key problem.** A 403 means the keys work but
  AgentGuard refused the data, for example because the organization's plan has ended. Status
  now points to AgentGuard's own message instead of the keys.

### Added

- **Status and setup detect a still-running OpenTelemetry collector** from the old setup on
  `127.0.0.1:4318`, which makes sessions appear twice. With Docker available, they name the
  container and print the exact command to stop it in its own Compose folder (or
  `docker stop` / `docker update --restart=no` without Compose). The plugin never stops
  anything itself.
- Status shows the active timeout.
- `LICENSE` (Apache License 2.0) and this changelog.

### Changed

- The README starts with installing from a terminal (`claude plugin ...`), which also works
  for the VS Code extension, where the `/plugin` chat commands are unavailable. It adds
  Update, Old OpenTelemetry setup and more troubleshooting entries.
- The hook's own timeout in `hooks.json` is now 90 seconds, so a debug-mode (foreground) send
  can use the whole budget. Normal hooks still return within milliseconds.

## 0.1.0 (2026-11-24)

First release.

- Sends each Claude Code session to AgentGuard through the Langfuse ingestion API: one trace
  per prompt, one generation per model response (usage counted once per message), one span
  per tool call, with subagents nested under the call that started them. Ids are derived from
  the transcript, so re-sending never duplicates.
- Incremental: only new transcript lines are read, with a file lock, a 7-day state cleanup,
  and retry on failure (the offset only moves after a successful send).
- Content is off by default. With `AGENTGUARD_LOG_CONTENT=true`, prompt, response and tool
  text is sent, cut to `AGENTGUARD_MAX_CONTENT_CHARS`. AgentGuard keys (`sk-ag-`, `pk-ag-`,
  `sk-lf-`, `pk-lf-`) are always redacted.
- Cache writes are sent split by cache lifetime (`input_cache_creation_1h` / `_5m`), matching
  AgentGuard's price table. Fast-mode pricing comes from `modelParameters.speed`.
- The hook hands the work to a detached background process and always exits 0, silently.
- macOS, Linux and Windows (native with or without Git Bash, or WSL). One hook command runs
  in both sh and PowerShell. Launchers find Python 3.8+ and never run the Microsoft Store
  shortcut.
- `/agentguard:setup` (keys from a hidden terminal prompt, or pasted) and
  `/agentguard:status`.
- CI on Ubuntu, macOS and Windows with Python 3.8 and 3.12.
