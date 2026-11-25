---
description: Show the AgentGuard plugin's status. Shows whether the keys are set (masked), when data was last sent and with what result, and the latest errors.
disable-model-invocation: true
allowed-tools: Bash(sh "${CLAUDE_PLUGIN_ROOT}/scripts/agentguard.sh" --status) PowerShell(powershell -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}/scripts/agentguard.ps1" --status)
---

# AgentGuard status

Run the status command once, with the shell tool you have:

- **Bash tool** (macOS, Linux, WSL, or Windows with Git Bash):
  `sh "${CLAUDE_PLUGIN_ROOT}/scripts/agentguard.sh" --status`
- **PowerShell tool** (Windows without Git Bash):
  `powershell -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}/scripts/agentguard.ps1" --status`

Show its output to the user unchanged, in a code block. Then add a short diagnosis, only for
the problems that actually appear:

- **"AgentGuard needs Python 3.8 or newer"**: the output already says how to install Python
  for each system. Tell the user which line applies to their system (you know their
  platform), and to restart Claude Code afterwards.
- **Keys are not set:** run `/agentguard:setup`.
- **"restart Claude Code to use them":** setup saved the keys after this session started.
  Restart Claude Code (in VS Code: quit it completely, then start it again).
- **Last send FAILED (HTTP 401):** the keys are wrong or belong to another host. Run
  `/agentguard:setup` again.
- **Last send FAILED (HTTP 403):** the keys work, but AgentGuard refused the data. Quote its
  message, for example that the organization's plan has ended. The user or their AgentGuard
  admin has to fix that. Unsent data stays queued until then.
- **Last send FAILED with TimeoutError / "timed out":** AgentGuard answered too slowly.
  Nothing is lost: the data is sent again after the next answer. If it keeps happening, the
  user can raise `AGENTGUARD_TIMEOUT_SECONDS` (shown on the `Timeout:` line, default 15, at
  most 60) in the settings `env` block, then restart.
- **Last send FAILED with a connection error:** AgentGuard could not be reached from this
  machine. Unsent data stays queued. Check the host URL and the network or VPN.
- **"time budget ... used up":** a long backlog, for example the first run on a long session.
  It drains over the next few turns.
- **"an OpenTelemetry collector is still running" or "something is listening on
  127.0.0.1:4318":** the old collector makes every session appear twice. Show the user the
  printed command unchanged. Tell them to run `docker compose down` only in the folder shown,
  never in another one. **Do not run any docker command yourself.**
- **Last send: never** with keys set: data is sent when a turn ends. Send any prompt, wait for
  the answer, then run `/agentguard:status` again.
- **Old OpenTelemetry settings found:** they make every session appear twice. Run
  `/agentguard:setup`, which offers to remove them, or delete them from the `env` block of
  `~/.claude/settings.json`.

For more detail, the user can set `"AGENTGUARD_DEBUG": "true"` in the settings `env` block and
restart. Hooks then run in the foreground and print what they do in Claude Code's verbose hook
output. Debug mode makes each turn wait for the send, so it should be turned off afterwards.
