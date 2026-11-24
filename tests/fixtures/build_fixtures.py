"""Builds the anonymized transcript fixtures.

The entry shapes (keys, nesting, one line per content block with repeated usage, tool
results as user entries, subagents in <session>/subagents/) are copied from real Claude
Code 2.1.x transcripts; all ids, text and paths are made up.

    python3 tests/fixtures/build_fixtures.py
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION = "11111111-2222-3333-4444-555555555555"
CWD = "/home/dev/example-project"
VERSION = "2.1.330"
FAKE_SECRET_IN_PROMPT = "sk-lf-0123456789abcdef"  # looks like a key: must be redacted

common = {"userType": "external", "entrypoint": "cli", "cwd": CWD, "sessionId": SESSION, "version": VERSION, "gitBranch": "feature/demo"}


def usage(inp, out, read, create, ttl="1h", split=True):
    u = {
        "input_tokens": inp,
        "cache_creation_input_tokens": create,
        "cache_read_input_tokens": read,
        "output_tokens": out,
        "output_tokens_details": {"reasoning_tokens": 0},
        "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
        "service_tier": "standard",
        "inference_geo": "global",
        "iterations": [{"input_tokens": inp, "output_tokens": out}],
        "speed": "standard",
        "fallback_credit": None,
    }
    if split:
        u["cache_creation"] = {"ephemeral_5m_input_tokens": create if ttl == "5m" else 0, "ephemeral_1h_input_tokens": create if ttl == "1h" else 0}
    return u


def assistant(uuid, parent, ts, mid, req, block, u, model="claude-opus-5-5", stop="tool_use", sidechain=False, extra=None):
    e = {
        "parentUuid": parent,
        "isSidechain": sidechain,
        "message": {"model": model, "id": mid, "type": "message", "role": "assistant", "content": [block], "container": None, "stop_reason": stop, "stop_sequence": None, "stop_details": None, "usage": u, "input_transformations": [], "diagnostics": None, "context_management": None},
        "requestId": req,
        "requestedModel": model,
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts,
        "effort": "high",
    }
    e.update(common)
    if extra:
        e.update(extra)
    return e


def user(uuid, parent, ts, content, extra=None, sidechain=False):
    e = {"parentUuid": parent, "isSidechain": sidechain, "type": "user", "message": {"role": "user", "content": content}, "uuid": uuid, "timestamp": ts}
    e.update(common)
    if extra:
        e.update(extra)
    return e


def tool_result(uuid, parent, ts, tool_id, text, is_error=None, tur=None, sidechain=False):
    block = {"tool_use_id": tool_id, "type": "tool_result", "content": text}
    if is_error is not None:
        block["is_error"] = is_error
    return user(uuid, parent, ts, [block], {"toolUseResult": tur if tur is not None else {"stdout": text, "stderr": "", "interrupted": False}, "sourceToolAssistantUUID": parent, "promptId": "p-1"}, sidechain=sidechain)


def main_lines():
    uA = usage(3, 120, 1000, 500)
    uB = usage(1, 80, 1500, 200)
    uC = usage(1, 40, 1700, 0)
    uD = usage(5, 60, 0, 900, split=False)  # older format: no cache_creation breakdown
    uE = usage(2, 30, 2000, 300, ttl="5m")
    return [
        {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-11-20T10:00:00.000Z", "sessionId": SESSION, "content": "x"},
        dict({"parentUuid": None, "isSidechain": False, "type": "attachment", "attachment": {"type": "environment"}, "uuid": "a-0", "timestamp": "2026-11-20T10:00:00.010Z"}, **common),
        user("u-1", "a-0", "2026-11-20T10:00:00.100Z", [{"type": "text", "text": "PROMPT-ONE: list the files in this project"}], {"promptId": "p-1", "origin": {"kind": "human"}, "promptSource": "sdk", "turnOrigin": "human", "permissionMode": "default"}),
        # one response written as three lines (thinking, text, tool_use) sharing message.id and usage
        assistant("as-1", "u-1", "2026-11-20T10:00:02.000Z", "msg_A", "req_A", {"type": "thinking", "thinking": "THINKING-SECRET", "signature": "sig"}, uA),
        assistant("as-2", "as-1", "2026-11-20T10:00:02.300Z", "msg_A", "req_A", {"type": "text", "text": "RESPONSE-A: I will look."}, uA),
        assistant("as-3", "as-2", "2026-11-20T10:00:03.000Z", "msg_A", "req_A", {"type": "tool_use", "id": "toolu_A1", "name": "Bash", "input": {"command": "ls -la /home/dev/example-project", "description": "List files"}}, uA),
        tool_result("tr-1", "as-3", "2026-11-20T10:00:04.000Z", "toolu_A1", "TOOL-OUTPUT-A1: README.md src tests", is_error=False),
        {"type": "api-request", "id": "r-1", "timestamp": "2026-11-20T10:00:04.010Z", "sessionId": SESSION},
        assistant("as-4", "tr-1", "2026-11-20T10:00:05.000Z", "msg_B", "req_B", {"type": "tool_use", "id": "toolu_B1", "name": "Agent", "input": {"description": "Explore src", "prompt": "SUBAGENT-PROMPT", "subagent_type": "Explore"}}, uB),
        assistant("as-5", "as-4", "2026-11-20T10:00:05.200Z", "msg_B", "req_B", {"type": "tool_use", "id": "toolu_B2", "name": "Read", "input": {"file_path": "/home/dev/example-project/missing.txt"}}, uB),
        tool_result("tr-2", "as-5", "2026-11-20T10:00:05.400Z", "toolu_B2", "File does not exist.", is_error=True, tur="Error: File does not exist."),
        tool_result("tr-3", "as-4", "2026-11-20T10:00:09.000Z", "toolu_B1", "SUBAGENT-REPORT", tur={"status": "completed", "agentId": "afixture01", "prompt": "SUBAGENT-PROMPT", "isAsync": False}),
        assistant("as-6", "tr-3", "2026-11-20T10:00:10.000Z", "msg_C", "req_C", {"type": "text", "text": "FINAL-ANSWER-ONE: there are three entries."}, uC, stop="end_turn"),
        {"type": "last-prompt", "lastPrompt": "x", "leafUuid": "as-6", "sessionId": SESSION},
        {"type": "ai-title", "aiTitle": "Listing files", "sessionId": SESSION},
        user("u-int", "as-6", "2026-11-20T10:01:00.000Z", [{"type": "text", "text": "[Request interrupted by user]"}]),
        assistant("as-syn", "u-int", "2026-11-20T10:01:00.100Z", "msg_SYN", None, {"type": "text", "text": "API Error"}, usage(0, 0, 0, 0), model="<synthetic>", stop="stop_sequence", extra={"isApiErrorMessage": True}),
        user("u-2", "as-syn", "2026-11-20T10:02:00.000Z", "PROMPT-TWO: my key is %s, keep it safe" % FAKE_SECRET_IN_PROMPT, {"promptId": "p-2", "turnOrigin": "human", "permissionMode": "default"}),
        assistant("as-7", "u-2", "2026-11-20T10:02:03.000Z", "msg_D", "req_D", {"type": "text", "text": "FINAL-ANSWER-TWO: noted."}, uD, stop="end_turn"),
        user("u-3", "as-7", "2026-11-20T10:03:00.000Z", "<task-notification>\n<task-id>t1</task-id>\n<status>completed</status>\n</task-notification>", {"promptId": "p-3", "turnOrigin": "task_notification", "promptSource": "system"}),
        assistant("as-8", "u-3", "2026-11-20T10:03:01.000Z", "msg_E", "req_E", {"type": "text", "text": "FINAL-ANSWER-THREE: the task finished."}, uE, stop="end_turn"),
        user("u-4", "as-8", "2026-11-20T10:04:00.000Z", "<command-name>/clear</command-name>\n<command-message>clear</command-message>\n<command-args></command-args>", {"promptId": "p-4", "turnOrigin": "human"}),
        user("u-5", "u-4", "2026-11-20T10:04:00.050Z", "<local-command-stdout></local-command-stdout>"),
    ]


def sub_lines():
    sc = {"agentId": "afixture01"}
    u1 = usage(10, 50, 0, 3000, ttl="5m")
    u2 = usage(4, 70, 3000, 100, ttl="5m")
    return [
        user("s-u1", None, "2026-11-20T10:00:05.500Z", "SUBAGENT-PROMPT", sc, sidechain=True),
        assistant("s-a1", "s-u1", "2026-11-20T10:00:06.000Z", "msg_S1", "req_S1", {"type": "tool_use", "id": "toolu_S1", "name": "Grep", "input": {"pattern": "TODO"}}, u1, model="claude-haiku-5-5", sidechain=True, extra=sc),
        tool_result("s-tr1", "s-a1", "2026-11-20T10:00:07.000Z", "toolu_S1", "src/a.py:1: TODO", sidechain=True, tur={"mode": "content"}),
        assistant("s-a2", "s-tr1", "2026-11-20T10:00:08.500Z", "msg_S2", "req_S2", {"type": "text", "text": "SUBAGENT-REPORT"}, u2, model="claude-haiku-5-5", stop="end_turn", sidechain=True, extra=sc),
    ]


def write(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        for e in lines:
            fh.write(json.dumps(e, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    write(os.path.join(HERE, SESSION + ".jsonl"), main_lines())
    sub_dir = os.path.join(HERE, SESSION, "subagents")
    os.makedirs(sub_dir, exist_ok=True)
    write(os.path.join(sub_dir, "agent-afixture01.jsonl"), sub_lines())
    with open(os.path.join(sub_dir, "agent-afixture01.meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"agentType": "Explore", "description": "Explore src", "isBuiltIn": True, "model": "claude-haiku-5-5", "spawnDepth": 1, "toolUseId": "toolu_B1", "finished": True}, fh, indent=2)
        fh.write("\n")
    print("fixtures written to", HERE)
