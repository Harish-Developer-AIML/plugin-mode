"""Builds the anonymized Codex fixtures under tests/fixtures/codex_home/.

Line types, field names and their order are copied from real Codex 0.171 rollout files
(VS Code extension, 2026-11-25): session_meta, event_msg (task_started, user_message,
item_completed, token_count, task_complete, turn_aborted), turn_context, world_state,
response_item (message, reasoning, custom_tool_call, function_call and their outputs) and
token_usage_record. Ids, text, paths and numbers are made up.

    python3 tests/fixtures/build_codex_fixtures.py
"""

import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.join(HERE, "codex_home")
DAY = os.path.join(HOME, "sessions", "2026", "11", "20")
MAIN = "019a0000-0000-7000-8000-00000000aaaa"
GUARD = "019a0000-0000-7000-8000-00000000bbbb"
OTHER = "019a0000-0000-7000-8000-00000000cccc"
HELPER = "019a0000-0000-7000-8000-00000000dddd"  # notified, but never saved (no rollout file)
LEGACY = "019a0000-0000-7000-8000-00000000eeee"  # written by an older Codex: no token_usage_record at first
LEGACY_GUARD = "019a0000-0000-7000-8000-00000000ffff"  # its guardian review, in the next day's folder
DAY19 = os.path.join(HOME, "sessions", "2026", "11", "19")
T1, T2 = "019a0000-0000-7000-8000-0000000000t1", "019a0000-0000-7000-8000-0000000000t2"
FAKE_KEY = "sk-ag-3f2a9c1e-7b4d-4e8a-9c2f-1a2b3c4d5e6f"


def usage(inp, cached, write, out, reasoning):
    return {"input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": write,
            "output_tokens": out, "reasoning_output_tokens": reasoning, "total_tokens": inp + out}


class Writer(object):
    def __init__(self, prefix="2026-11-20T10:"):
        self.lines = []
        self.prefix = prefix

    def add(self, ts, type_, payload):
        self.lines.append({"timestamp": self.prefix + ts, "type": type_, "payload": payload, "ordinal": len(self.lines), "metadata": {}})

    def msg(self, ts, role, text, kind=None):
        kind = kind or ("output_text" if role == "assistant" else "input_text")
        self.add(ts, "response_item", {"type": "message", "id": "msg_%d" % len(self.lines), "role": role, "content": [{"type": kind, "text": text}],
                                       "internal_chat_message_metadata_passthrough": None})

    def record(self, ts, response_id, turn_id, thread_id, u):
        self.add(ts, "token_usage_record", {"response_id": response_id, "root_turn_id": turn_id, "session_id": MAIN, "thread_id": thread_id,
                                            "turn_id": turn_id, "usage": u, "turn_token_usage": u, "thread_token_usage": u})
        self.add(ts, "event_msg", {"type": "token_count", "info": {"total_token_usage": u, "last_token_usage": u, "model_context_window": 272000}, "rate_limits": None})

    def token_count(self, ts, total, last):
        self.add(ts, "event_msg", {"type": "token_count", "info": None if total is None else {"total_token_usage": total, "last_token_usage": last, "model_context_window": 272000}, "rate_limits": {"primary": None}})

    def save(self, name, folder=None):
        with open(os.path.join(folder or DAY, name), "w", encoding="utf-8", newline="\n") as fh:
            for line in self.lines:
                fh.write(json.dumps(line, separators=(",", ":")) + "\n")


def meta(thread_id, session_id, source, parent=None):
    p = {"id": thread_id, "session_id": session_id, "timestamp": "2026-11-20T10:00:00.000Z", "cwd": "/home/dev/example-project",
         "originator": "codex_vscode", "cli_version": "0.171.0-alpha.13.2", "source": source, "model_provider": "openai",
         "git": {"commit_hash": "abc123", "branch": "feature/demo", "repository_url": "https://example.com/repo.git"},
         "base_instructions": {"text": "BASE-INSTRUCTIONS"}, "context_window": 272000, "history_mode": "paginated", "thread_source": "user",
         "creator_account_id": "acct-0000", "creator_user_id": "user-0000", "account_scoped": True, "runtime_workspace_roots": ["/home/dev/example-project"]}
    if parent:
        p["parent_thread_id"] = parent
    return p


def main_thread():
    w = Writer()
    w.add("00:00.000Z", "session_meta", meta(MAIN, MAIN, "vscode"))
    w.add("00:00.010Z", "event_msg", {"type": "task_started", "turn_id": T1, "root_turn_id": T1, "started_at": "2026-11-20T10:00:00.010Z",
                                      "model_context_window": 272000, "collaboration_mode_kind": "default", "turn_attribution": "user"})
    w.add("00:00.020Z", "response_item", {"type": "additional_tools", "id": "tools_1", "role": "developer", "tools": []})
    w.msg("00:00.030Z", "developer", "DEVELOPER-INSTRUCTIONS")
    w.msg("00:00.040Z", "user", "# Context from my IDE setup:\nOPEN-FILES")
    w.msg("00:00.050Z", "user", "<environment_context>ENV</environment_context>")
    w.add("00:00.060Z", "world_state", {"full": True, "state": {}})
    w.add("00:00.070Z", "turn_context", {"turn_id": T1, "root_turn_id": T1, "model": "gpt-6.1-sol", "effort": "high", "cwd": "/home/dev/example-project",
                                         "approval_policy": "on-request", "summary": "auto"})
    w.add("00:00.080Z", "event_msg", {"type": "thread_settings_applied", "thread_id": MAIN, "thread_settings": {}})
    w.msg("00:00.090Z", "user", "PROMPT-ONE: fix the failing test")
    w.add("00:00.100Z", "event_msg", {"type": "user_message", "message": "PROMPT-ONE: fix the failing test", "images": [], "local_images": [],
                                      "text_elements": [], "audio": None, "local_audio": None, "client_id": "c1"})
    w.add("00:01.500Z", "response_item", {"type": "reasoning", "id": "rs_1", "summary": [], "content": None, "encrypted_content": "ENCRYPTED-REASONING",
                                          "internal_chat_message_metadata_passthrough": None})
    w.msg("00:01.600Z", "assistant", "RESPONSE-ONE-A: let me look")
    w.add("00:01.700Z", "event_msg", {"type": "agent_message", "message": "RESPONSE-ONE-A: let me look", "phase": None, "memory_citation": None})
    w.add("00:02.000Z", "response_item", {"type": "custom_tool_call", "id": "ctc_1", "call_id": "call_C1", "name": "exec", "input": "SCRIPT-INPUT: run tests",
                                          "status": "completed", "internal_chat_message_metadata_passthrough": None})
    w.record("00:02.100Z", "resp_R1", T1, MAIN, usage(7783, 4864, 0, 138, 88))
    w.add("00:04.000Z", "event_msg", {"type": "item_completed", "thread_id": MAIN, "turn_id": T1, "started_at_ms": 1, "completed_at_ms": 2,
                                      "item": {"type": "CommandExecution", "id": "cmd_1", "command": "pytest", "status": "failed", "exit_code": 1,
                                               "duration": {"secs": 1, "nanos": 0}, "aggregated_output": "TEST-OUTPUT", "cwd": "/x", "parsed_cmd": [], "process_id": 1, "source": "agent"}})
    w.add("00:04.100Z", "response_item", {"type": "custom_tool_call_output", "id": "ctco_1", "call_id": "call_C1",
                                          "output": [{"type": "input_text", "text": "TOOL-OUTPUT-C1: 1 failed"}], "internal_chat_message_metadata_passthrough": None})
    w.add("00:06.000Z", "response_item", {"type": "function_call", "id": "fc_1", "call_id": "call_C2", "name": "apply_patch", "namespace": None,
                                          "arguments": "{\"patch\":\"PATCH-TEXT\"}", "status": "completed", "internal_chat_message_metadata_passthrough": None})
    w.record("00:06.100Z", "resp_R2", T1, MAIN, usage(9000, 8000, 500, 300, 100))
    w.add("00:07.000Z", "event_msg", {"type": "item_completed", "thread_id": MAIN, "turn_id": T1, "started_at_ms": 3, "completed_at_ms": 4,
                                      "item": {"type": "FileChange", "id": "fc_item_1", "status": "completed"}})
    w.add("00:07.100Z", "response_item", {"type": "function_call_output", "id": "fco_1", "call_id": "call_C2", "output": "Script exited with code 0",
                                          "internal_chat_message_metadata_passthrough": None})
    w.msg("00:11.000Z", "assistant", "FINAL-ONE: fixed the test")
    w.record("00:11.100Z", "resp_R3", T1, MAIN, usage(9500, 9000, 0, 50, 0))
    w.add("00:12.000Z", "event_msg", {"type": "task_complete", "turn_id": T1, "root_turn_id": T1, "started_at": "2026-11-20T10:00:00.010Z",
                                      "completed_at": "2026-11-20T10:00:12.000Z", "duration_ms": 11990, "time_to_first_token_ms": 800,
                                      "last_agent_message": "FINAL-ONE: fixed the test"})
    w.add("01:00.000Z", "event_msg", {"type": "task_started", "turn_id": T2, "root_turn_id": T2, "started_at": "2026-11-20T10:01:00.000Z",
                                      "model_context_window": 272000, "collaboration_mode_kind": "default", "turn_attribution": "user"})
    w.add("01:00.010Z", "turn_context", {"turn_id": T2, "root_turn_id": T2, "model": "gpt-6.1-sol", "effort": "medium", "cwd": "/home/dev/example-project"})
    w.msg("01:00.020Z", "user", "PROMPT-TWO: the key is %s" % FAKE_KEY)
    w.msg("01:02.000Z", "assistant", "PARTIAL-TWO")
    w.record("01:02.100Z", "resp_R4", T2, MAIN, usage(9600, 9500, 0, 10, 0))
    w.add("01:03.000Z", "event_msg", {"type": "turn_aborted", "turn_id": T2, "root_turn_id": T2, "reason": "interrupted", "started_at": "2026-11-20T10:01:00.000Z",
                                      "completed_at": "2026-11-20T10:01:03.000Z", "duration_ms": 3000})
    return w


def guardian_thread():
    w = Writer()
    w.add("00:05.000Z", "session_meta", meta(GUARD, MAIN, {"subagent": {"other": "guardian"}}, parent=MAIN))
    w.add("00:05.010Z", "event_msg", {"type": "task_started", "turn_id": "turn_G1", "root_turn_id": "turn_G1", "started_at": "2026-11-20T10:00:05.010Z"})
    w.add("00:05.020Z", "turn_context", {"turn_id": "turn_G1", "model": "codex-auto-review", "effort": "low", "cwd": "/home/dev/example-project"})
    w.msg("00:05.030Z", "user", "REVIEW-REQUEST: is this command safe")
    w.msg("00:05.900Z", "assistant", "REVIEW-ANSWER: approve")
    w.record("00:06.000Z", "resp_RG1", "turn_G1", GUARD, usage(3000, 0, 0, 20, 10))
    w.add("00:06.050Z", "event_msg", {"type": "task_complete", "turn_id": "turn_G1", "completed_at": "2026-11-20T10:00:06.050Z", "duration_ms": 1040,
                                      "last_agent_message": "REVIEW-ANSWER: approve"})
    return w


def other_thread():
    w = Writer()
    w.add("30:00.000Z", "session_meta", meta(OTHER, OTHER, "vscode"))
    w.add("30:00.010Z", "event_msg", {"type": "task_started", "turn_id": "turn_O1", "started_at": "2026-11-20T10:30:00.010Z"})
    w.add("30:00.020Z", "turn_context", {"turn_id": "turn_O1", "model": "gpt-6.1-sol"})
    w.msg("30:00.030Z", "user", "PROMPT-OTHER-SESSION")
    w.record("30:01.000Z", "resp_RO1", "turn_O1", OTHER, usage(100, 0, 0, 10, 0))
    w.add("30:01.100Z", "event_msg", {"type": "task_complete", "turn_id": "turn_O1", "duration_ms": 1090, "last_agent_message": "OTHER"})
    return w


def legacy_thread():
    """Old format: each response only as a token_count whose running total changed. The file
    switches to token_usage_record in its last response, as real files of that age do."""
    w = Writer("2026-11-19T23:")
    m = meta(LEGACY, LEGACY, "vscode")
    m["cli_version"] = "0.150.0-alpha.12.2"
    w.add("59:50.000Z", "session_meta", m)
    w.add("59:50.010Z", "event_msg", {"type": "task_started", "turn_id": "turn_L1", "started_at": "2026-11-19T23:59:50.010Z"})
    w.add("59:50.020Z", "turn_context", {"turn_id": "turn_L1", "model": "gpt-5.6-sol", "effort": "high"})
    w.msg("59:50.030Z", "user", "PROMPT-LEGACY")
    w.token_count("59:50.040Z", None, None)  # info is null at first: no response yet
    w.add("59:55.000Z", "response_item", {"type": "custom_tool_call", "id": "ctc_L", "call_id": "call_L1", "name": "exec", "input": "LEGACY-SCRIPT"})
    a = usage(5000, 4000, 0, 100, 40)
    w.token_count("59:55.100Z", a, a)
    w.token_count("59:55.200Z", a, a)  # same total again (a rate-limit update): not a new response
    w2 = Writer("2026-11-20T00:")
    w2.lines = w.lines
    w2.add("00:05.000Z", "response_item", {"type": "custom_tool_call_output", "id": "ctco_L", "call_id": "call_L1", "output": [{"type": "input_text", "text": "LEGACY-OUT"}]})
    b_last = usage(6000, 5000, 0, 60, 0)
    b_total = usage(11000, 9000, 0, 160, 40)
    w2.token_count("00:20.000Z", b_total, b_last)
    w2.record("00:25.000Z", "resp_L3", "turn_L1", LEGACY, usage(7000, 6000, 0, 30, 10))  # record era starts: its token_count is not counted again
    w2.add("00:30.000Z", "event_msg", {"type": "task_complete", "turn_id": "turn_L1", "duration_ms": 39990, "last_agent_message": "LEGACY-DONE"})
    return w2


def legacy_guardian():
    w = Writer("2026-11-20T00:")
    w.add("00:10.000Z", "session_meta", meta(LEGACY_GUARD, LEGACY, {"subagent": {"other": "guardian"}}, parent=LEGACY))
    w.add("00:10.010Z", "event_msg", {"type": "task_started", "turn_id": "turn_LG", "started_at": "2026-11-20T00:00:10.010Z"})
    w.add("00:10.020Z", "turn_context", {"turn_id": "turn_LG", "model": "codex-auto-review"})
    w.record("00:11.000Z", "resp_LG", "turn_LG", LEGACY_GUARD, usage(1000, 0, 0, 5, 0))
    w.add("00:11.100Z", "event_msg", {"type": "task_complete", "turn_id": "turn_LG", "duration_ms": 1090})
    return w


if __name__ == "__main__":
    shutil.rmtree(HOME, ignore_errors=True)
    os.makedirs(DAY)
    main_thread().save("rollout-2026-11-20T10-00-00-%s.jsonl" % MAIN)
    guardian_thread().save("rollout-2026-11-20T10-00-05-%s.jsonl" % GUARD)
    other_thread().save("rollout-2026-11-20T10-30-00-%s.jsonl" % OTHER)
    os.makedirs(DAY19)
    legacy_thread().save("rollout-2026-11-19T23-59-50-%s.jsonl" % LEGACY, DAY19)
    legacy_guardian().save("rollout-2026-11-20T00-00-10-%s.jsonl" % LEGACY_GUARD)
    with open(os.path.join(HOME, "config.toml"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write('model = "gpt-6.1-sol"\n\n[projects."/home/dev/example-project"]\ntrust_level = "trusted"\n')
    print("Codex fixtures written to", HOME)
