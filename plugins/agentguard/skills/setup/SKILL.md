---
description: Connect Claude Code to AgentGuard. Tests the host and API keys, saves them to ~/.claude/settings.json, and removes old OpenTelemetry settings that would duplicate traces.
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/send_trace.py" --status) Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/send_trace.py" --check-otel)
---

# AgentGuard setup

You are setting up the AgentGuard plugin. It sends each Claude Code session to AgentGuard as
traces. It needs three values: the AgentGuard host, a public key (`pk-ag-...`) and a secret
key (`sk-ag-...`). Both keys come from the AgentGuard project's settings page (API keys).

Current plugin status:

```
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/send_trace.py" --status`
```

Old OpenTelemetry settings (JSON):

```
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/send_trace.py" --check-otel`
```

## Steps

1. **Ask how the user wants to enter the keys.** Use AskUserQuestion with these two options:
   - **In my own terminal (recommended).** The secret key never enters this chat.
   - **Paste them here.** Warn the user first: the secret key will be stored in this
     session's transcript on their machine and sent to the model provider.

2. **Terminal option.** Give the user this command to run in a separate terminal window, not
   with `!` here, because it asks for the secret with a hidden prompt:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/send_trace.py" --setup
   ```

   It asks for the host (Enter keeps `https://ag-dev.actaclad.com`), the public key and the
   secret key. It tests the keys, saves them, and offers to remove old OpenTelemetry settings.
   Ask the user to tell you when it prints `Saved ...`. Then run
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/send_trace.py" --status`. It should say the keys
   are in settings.json but not in this session yet, which is expected before a restart.

3. **Paste option.** Ask for the host (default `https://ag-dev.actaclad.com`), the public key
   and the secret key. If the OpenTelemetry check above lists anything under
   `in_settings_env`, ask whether to remove those settings, and explain that they make
   Claude Code send its own telemetry, so every session would appear twice. Then run this,
   with the secret on its own line inside the here-document and never as an argument:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/send_trace.py" --setup --host "<host>" --public-key "<public key>" --secret-stdin [--remove-otel] <<'AGENTGUARD_SECRET'
   <secret key>
   AGENTGUARD_SECRET
   ```

   Add `--remove-otel` only if the user agreed. Exit codes: 0 saved; 2 a value is missing or
   the host is malformed; 3 the keys were refused or the host could not be reached (nothing
   was written); 4 `~/.claude/settings.json` could not be read or written (nothing was
   written). On 3, show the message and ask the user to check the keys and host.

4. **OpenTelemetry outside settings.json.** If anything is listed under
   `elsewhere_in_environment`, tell the user those variables come from their shell profile or
   another settings file, and that they should remove them there. If they used the old Docker
   collector setup (`claude-telemetry` with `docker-compose.yml`), they can stop and remove
   it: `docker compose down` in that folder.

5. **Finish.** Tell the user to **restart Claude Code**, because the hooks only see the new
   keys in a new session. After that, every session appears in AgentGuard under their git
   email. Mention that prompt and response text is **not** sent by default. To turn it on,
   they add `"AGENTGUARD_LOG_CONTENT": "true"` to the `env` block of
   `~/.claude/settings.json`. `/agentguard:status` shows the last send and any errors.

Rules: never repeat the secret key back to the user, never write it anywhere yourself (only the
script writes it, to `~/.claude/settings.json`), and never put it in a command argument.
