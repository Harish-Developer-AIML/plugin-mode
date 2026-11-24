---
description: Show the AgentGuard plugin's status. Shows whether the keys are set (masked), when data was last sent and with what result, and the latest errors.
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/send_trace.py" --status)
---

# AgentGuard status

```
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/send_trace.py" --status`
```

Show the status above to the user unchanged, in a code block. Then add a short diagnosis,
only for the problems that actually appear:

- **Keys are not set:** run `/agentguard:setup`.
- **Keys are in settings.json but not in this session:** restart Claude Code.
- **Last send FAILED (HTTP 401 or 403):** the keys are wrong or belong to another host. Run
  `/agentguard:setup` again.
- **Last send FAILED with a timeout or connection error:** AgentGuard could not be reached
  from this machine. Nothing is lost: unsent data stays queued and goes out on the next turn
  once the host is reachable. Check the host URL and the network or VPN.
- **"time budget used up":** a long backlog, for example the first run on a long session. It
  drains over the next few turns.
- **Last send: never** with keys set: data is sent when a turn ends. Send any prompt, wait for
  the answer, then run `/agentguard:status` again.
- **Old OpenTelemetry settings found:** they make every session appear twice. Run
  `/agentguard:setup`, which offers to remove them, or delete them from the `env` block of
  `~/.claude/settings.json`.

For more detail, the user can set `"AGENTGUARD_DEBUG": "true"` in the settings `env` block and
restart. Hooks then run in the foreground and print what they do in Claude Code's verbose hook
output. Debug mode makes each turn wait for the send, so it should be turned off afterwards.
