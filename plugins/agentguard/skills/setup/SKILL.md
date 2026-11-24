---
description: Connect Claude Code to AgentGuard. Tests the host and API keys, saves them to ~/.claude/settings.json, and removes old OpenTelemetry settings that would duplicate traces.
disable-model-invocation: true
allowed-tools: Bash(sh "${CLAUDE_PLUGIN_ROOT}/scripts/agentguard.sh" --status) Bash(sh "${CLAUDE_PLUGIN_ROOT}/scripts/agentguard.sh" --check-otel) PowerShell(powershell -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}/scripts/agentguard.ps1" --status) PowerShell(powershell -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}/scripts/agentguard.ps1" --check-otel)
---

# AgentGuard setup

You are setting up the AgentGuard plugin. It sends each Claude Code session to AgentGuard as
traces. It needs three values: the AgentGuard host, a public key (`pk-ag-...`) and a secret
key (`sk-ag-...`). Both keys come from the AgentGuard project's settings page (API keys).

## How to run the plugin's script

Every command below goes through a small launcher. It finds Python 3.8 or newer and prints
install instructions when there is none. Use the form that matches your shell tool, and
replace `<options>` with the options given in each step:

- **Bash tool** (macOS, Linux, WSL, or Windows with Git Bash):
  `sh "${CLAUDE_PLUGIN_ROOT}/scripts/agentguard.sh" <options>`
- **PowerShell tool** (Windows without Git Bash):
  `powershell -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}/scripts/agentguard.ps1" <options>`

## Steps

1. **Check the current state.** Run the launcher with `--status`, then with `--check-otel`.
   If the output says **"AgentGuard needs Python 3.8 or newer"**, stop here. Show the user that
   message, point out the install line for their system, and tell them to restart Claude Code
   after installing Python and then run `/agentguard:setup` again.

2. **Ask how the user wants to enter the keys.** Use AskUserQuestion with these two options:
   - **In my own terminal (recommended).** The secret key never enters this chat.
   - **Paste them here.** Warn the user first: the secret key will be stored in this
     session's transcript on their machine and sent to the model provider.

3. **Terminal option.** Give the user the one command for their system. They run it in a
   separate terminal window, not with `!` here, because it asks for the secret with a hidden
   prompt:

   - macOS, Linux, WSL (and Git Bash on Windows):
     ```
     sh "${CLAUDE_PLUGIN_ROOT}/scripts/agentguard.sh" --setup
     ```
   - Windows, in PowerShell or Command Prompt:
     ```
     powershell -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}/scripts/agentguard.ps1" --setup
     ```

   It asks for the host (Enter keeps `https://ag-dev.actaclad.com`), the public key and the
   secret key. It tests the keys, saves them, and offers to remove old OpenTelemetry settings.
   Ask the user to tell you when it prints `Saved ...`. Then run the launcher with `--status`.
   It should say the keys are in settings.json but not in this session yet, which is expected
   before a restart.

4. **Paste option.** Ask for the host (default `https://ag-dev.actaclad.com`), the public key
   and the secret key. If `--check-otel` listed anything under `in_settings_env`, ask whether
   to remove those settings. Explain that they make Claude Code send its own telemetry, so
   every session would appear twice. Pass the secret on stdin, never as an argument:

   - Bash tool:
     ```
     sh "${CLAUDE_PLUGIN_ROOT}/scripts/agentguard.sh" --setup --host "<host>" --public-key "<public key>" --secret-stdin [--remove-otel] <<'AGENTGUARD_SECRET'
     <secret key>
     AGENTGUARD_SECRET
     ```
   - PowerShell tool:
     ```
     @'
     <secret key>
     '@ | powershell -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}/scripts/agentguard.ps1" --setup --host "<host>" --public-key "<public key>" --secret-stdin [--remove-otel]
     ```

   Add `--remove-otel` only if the user agreed. Exit codes: 0 saved; 1 no suitable Python
   (see step 1); 2 a value is missing or the host is malformed; 3 the keys were refused or
   the host could not be reached (nothing was written); 4 `~/.claude/settings.json` could
   not be read or written (nothing was written). On 3, show the message and ask the user to
   check the keys and host.

5. **OpenTelemetry outside settings.json.** If anything is listed under
   `elsewhere_in_environment`, tell the user those variables come from their shell profile,
   their Windows user environment variables, or another settings file, and that they should
   remove them there. If they used the old Docker collector setup (`claude-telemetry` with
   `docker-compose.yml`), they can stop and remove it: `docker compose down` in that folder.

6. **Finish.** Tell the user to **restart Claude Code**, because the hooks only see the new
   keys in a new session. After that, every session appears in AgentGuard under their git
   email. Mention that prompt and response text is **not** sent by default. To turn it on,
   they add `"AGENTGUARD_LOG_CONTENT": "true"` to the `env` block of
   `~/.claude/settings.json` (`%USERPROFILE%\.claude\settings.json` on Windows).
   `/agentguard:status` shows the last send and any errors.

Rules: never repeat the secret key back to the user, never write it anywhere yourself (only the
script writes it, to `~/.claude/settings.json`), and never put it in a command argument.
