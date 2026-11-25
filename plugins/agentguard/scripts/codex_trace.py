#!/usr/bin/env python3
"""Send OpenAI Codex sessions (CLI, VS Code extension, desktop app) to AgentGuard.

Codex runs this as its `notify` program after every turn and appends one JSON argument
(`type: agent-turn-complete`, `thread-id`, `turn-id`, `cwd`, `client`, ...). See NOTES.md
section 12 for why notify and not Codex hooks.

Modes
  notify <json>   What Codex runs. Calls the developer's previous notify program first (if
                  setup chained one), hands the work to a detached worker, exits 0 at once
                  and prints nothing.
  --worker        The background process: reads the new lines of the thread's rollout file
                  (~/.codex/sessions/.../rollout-*<thread-id>.jsonl) and of its guardian
                  review threads, maps them to traces, generations and spans, and sends them.
  --status        Prints this adapter's state.
  --setup         Asks for and tests the keys (or reuses saved ones), saves them in
                  ~/.agentguard/config.json, copies the scripts to ~/.agentguard/scripts and
                  points Codex's notify there (chaining a notify program that was set before).
                  Options: --host URL --public-key KEY --secret-stdin
  --uninstall     Puts Codex's notify back as it was. Keys and scripts stay (other tools may
                  use them).

Keys come from the environment, else from ~/.agentguard/config.json: Codex passes no
AGENTGUARD_* variables to notify programs. State: ~/.agentguard/state/codex.json; errors:
~/.agentguard/logs/codex.log. Python 3.8+ standard library only.
"""

import copy
import getpass
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agentguard_core as core  # noqa: E402

PLUGIN_VERSION = "0.1.1"
AGENT = "codex"
TRACE_NAME = "codex.turn"
GENERATION_NAME = "codex.llm_call"
SUBAGENT_SPAN_NAME = "codex.subagent"
TRACE_TAGS = ["codex"]
SESSION_PREFIX = "codex:"
USER_AGENT = "agentguard-codex/%s" % PLUGIN_VERSION

# ---------------------------------------------------------------------------
# Usage keys sent as usageDetails. They must match AgentGuard's model price table for
# OpenAI models (checked against gpt-6.1-sol: input, input_cached_tokens,
# input_cache_write_tokens, output, output_reasoning_tokens). Codex counts cached and
# cache-write tokens INSIDE input_tokens and reasoning tokens INSIDE output_tokens, so the
# plain "input" and "output" keys get the remainder: nothing is priced twice.
# Set a value to None to stop sending that key.
# ---------------------------------------------------------------------------
CODEX_USAGE_KEYS = {
    "input": "input",                              # input_tokens - cached - cache writes
    "cached_input": "input_cached_tokens",         # cached_input_tokens
    "cache_write": "input_cache_write_tokens",     # cache_write_input_tokens
    "output": "output",                            # output_tokens - reasoning
    "reasoning": "output_reasoning_tokens",        # reasoning_output_tokens
}

# User messages that Codex injects as context rather than typed prompts (start of the text).
CONTEXT_PREFIXES = ("<", "# Context", "# AGENTS", "# Instructions")
WORKER_PAYLOAD_KEYS = ("thread-id", "turn-id", "cwd", "client", "type")
MAX_BATCH_EVENTS = core.MAX_BATCH_EVENTS
IS_WINDOWS = core.IS_WINDOWS

_CFG = None


# ---------------------------------------------------------------------------
# Paths and logging
# ---------------------------------------------------------------------------

def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def state_path():
    return os.path.join(core.agentguard_home(), "state", "codex.json")


def lock_path():
    return os.path.join(core.agentguard_home(), "state", "codex.lock")


def error_log_path():
    return os.path.join(core.agentguard_home(), "logs", "codex.log")


def log_error(msg):
    core.log_error(msg, error_log_path(), core.ERROR_LOG_MAX_BYTES, cfg=_CFG)


def debug(msg):
    core.debug(msg, _CFG)


def http_request(cfg, method, path, body=None, timeout=core.DEFAULT_TIMEOUT_SECONDS):
    return core.http_request(cfg, method, path, body, timeout, user_agent=USER_AGENT)


def make_sender(cfg):
    return core.Sender(cfg, log=log_error, debug_log=debug, request=http_request)


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def codex_usage_details(usage):
    """Codex usage -> usageDetails with CODEX_USAGE_KEYS (see the comment there)."""
    if not isinstance(usage, dict):
        return {}
    cached = _int(usage.get("cached_input_tokens"))
    write = _int(usage.get("cache_write_input_tokens"))
    reasoning = _int(usage.get("reasoning_output_tokens"))
    values = {
        "input": max(_int(usage.get("input_tokens")) - cached - write, 0),
        "cached_input": cached,
        "cache_write": write,
        "output": max(_int(usage.get("output_tokens")) - reasoning, 0),
        "reasoning": reasoning,
    }
    out = {}
    for name, value in values.items():
        key = CODEX_USAGE_KEYS.get(name)
        if key and (value or name in ("input", "output")):
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Rollout files
# ---------------------------------------------------------------------------

def find_rollout(thread_id):
    """Path of the thread's rollout .jsonl, or None. A compressed .jsonl.zst (older than 7
    days) cannot be read with the standard library and is skipped."""
    if not thread_id or not re.match(r"^[A-Za-z0-9-]+$", thread_id):
        return None
    pattern = os.path.join(codex_home(), "sessions", "*", "*", "*", "rollout-*%s.jsonl" % thread_id)
    found = sorted(glob.glob(pattern))
    return found[-1] if found else None


def read_first_entry(path):
    try:
        with open(path, "rb") as fh:
            return json.loads(fh.readline().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def new_lines(path, cursor):
    """(entries, end_offset, last_ordinal) for complete lines after the cursor.

    The cursor is the byte offset and the `ordinal` of the last line processed. Codex
    rewrites rollout files when it migrates them, so a line whose ordinal is not newer is
    skipped, and if the offset no longer lands on a line boundary the file is read again from
    the start (ordinals keep it from being sent twice)."""
    offset = cursor.get("offset", 0)
    last_ord = cursor.get("ordinal", -1)
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset, last_ord
    if size < offset:
        offset = 0
    for attempt in (0, 1):
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
        entries, pos, end, ordinal, broken = [], 0, offset, last_ord, False
        while True:
            nl = data.find(b"\n", pos)
            if nl < 0:
                break
            raw = data[pos:nl].strip()
            pos = nl + 1
            end = offset + pos
            if not raw:
                continue
            try:
                entry = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                if not entries and attempt == 0 and offset:
                    broken = True  # landed mid-line in a rewritten file
                    break
                log_error("skipped a malformed line in %s" % os.path.basename(path))
                continue
            if not isinstance(entry, dict):
                continue
            ord_ = entry.get("ordinal")
            if isinstance(ord_, int):
                if ord_ <= last_ord:
                    continue
                ordinal = ord_
            entries.append(entry)
        if broken:
            offset = 0
            continue
        return entries, end, ordinal
    return [], offset, last_ord


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------

def text_of(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict):
            if isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif item.get("type") in ("input_image", "image"):
                parts.append("[image]")
    return "\n".join(p for p in parts if p)


def is_typed_prompt(text):
    stripped = (text or "").lstrip()
    return bool(stripped) and not stripped.startswith(CONTEXT_PREFIXES)


class Mapper(object):
    """Turns the lines of one rollout file into ingestion events. All progress lives in
    `fstate["ctx"]` and `sess`, so a failed send can be rolled back by restoring a copy."""

    def __init__(self, cfg, sess, fstate, link=None):
        self.cfg = cfg
        self.sess = sess
        self.fstate = fstate
        self.ctx = fstate.setdefault("ctx", {})
        self.link = link  # guardian threads: {"session_id", "agent_type", "thread_id"}
        self.events = {}
        self.order = []

    # ---- helpers ----
    @property
    def session_id(self):
        return self.sess["session_id"]

    def det(self, *parts):
        return core.det_id(AGENT, self.session_id, *parts)

    def content(self, value):
        if not self.cfg.log_content or value is None:
            return None
        limit = self.cfg.max_content_chars
        if isinstance(value, str):
            return core.redact(core.truncate(value, limit), self.cfg)
        try:
            dumped = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            dumped = str(value)
        if len(dumped) <= limit:
            return json.loads(core.redact(dumped, self.cfg))
        return core.redact(core.truncate(dumped, limit), self.cfg)

    def emit(self, event_type, body):
        key = (event_type, body["id"])
        if key not in self.events:
            self.order.append(key)
        self.events[key] = body

    def take_events(self):
        out = [core.envelope(t, self.events[(t, i)]) for (t, i) in self.order]
        self.events, self.order = {}, []
        return out

    def pending_count(self):
        return len(self.order)

    # ---- turns ----
    def meta_base(self):
        meta = self.ctx.get("meta") or {}
        md = {"agent": AGENT, "plugin_version": PLUGIN_VERSION}
        for src, dst in (("cwd", "cwd"), ("git_branch", "git_branch"), ("cli_version", "codex_version"), ("originator", "originator"), ("thread_id", "thread_id")):
            if meta.get(src):
                md[dst] = meta[src]
        return md

    def on_session_meta(self, p):
        git = p.get("git") if isinstance(p.get("git"), dict) else {}
        self.ctx["meta"] = {
            "thread_id": p.get("id"),
            "cwd": p.get("cwd"),
            "git_branch": git.get("branch"),
            "cli_version": p.get("cli_version"),
            "originator": p.get("originator"),
            "source": p.get("source") if isinstance(p.get("source"), str) else json.dumps(p.get("source"), sort_keys=True),
        }

    def start_turn(self, turn_id, ts):
        self.finish_turn(None, status="incomplete")
        if self.link is not None:
            self.ctx["turn"] = {"turn_id": turn_id, "start": ts, "model": None, "subagent": True}
            self.ctx["boundary"] = ts
            return
        trace_id = self.det("trace", turn_id)
        md = self.meta_base()
        md.update({"turn_id": turn_id, "status": "running", "commands": 0, "commands_failed": 0, "file_changes": 0, "web_searches": 0})
        self.ctx["turn"] = {"turn_id": turn_id, "trace_id": trace_id, "start": ts, "model": None, "metadata": md, "input": None, "output": None}
        self.ctx["boundary"] = ts
        self.sess.setdefault("turns", {})[turn_id] = [trace_id, core.parse_ts(ts), None]
        self.trim_turn_windows()
        self.emit_turn()

    def trim_turn_windows(self, keep=200):
        turns = self.sess.get("turns") or {}
        if len(turns) > keep:
            for tid in sorted(turns, key=lambda t: turns[t][1] or 0)[: len(turns) - keep]:
                del turns[tid]

    def emit_turn(self):
        turn = self.ctx.get("turn")
        if not turn or self.link is not None:
            return
        body = {
            "id": turn["trace_id"],
            "timestamp": turn["start"],
            "name": TRACE_NAME,
            "userId": self.sess.get("user_id"),
            "sessionId": SESSION_PREFIX + self.session_id,
            "tags": list(TRACE_TAGS),
            "metadata": turn["metadata"],
        }
        if turn.get("input") is not None:
            body["input"] = turn["input"]
        if turn.get("output") is not None:
            body["output"] = turn["output"]
        self.emit("trace-create", body)

    def finish_turn(self, p, status, ts=None):
        turn = self.ctx.get("turn")
        if not turn:
            return
        if turn.get("subagent"):
            self.close_subagent_span(ts)
            self.ctx.pop("turn", None)
            return
        md = turn["metadata"]
        md["status"] = status
        if isinstance(p, dict):
            for src, dst in (("duration_ms", "duration_ms"), ("time_to_first_token_ms", "time_to_first_token_ms")):
                if isinstance(p.get(src), (int, float)):
                    md[dst] = p[src]
            if status == "completed" and isinstance(p.get("last_agent_message"), str):
                turn["output"] = self.content(p["last_agent_message"])
            if p.get("reason"):
                md["abort_reason"] = str(p["reason"])[:80]
        window = (self.sess.get("turns") or {}).get(turn["turn_id"])
        if window is not None:
            window[2] = core.parse_ts(ts) if ts else window[1]
        self.emit_turn()
        self.ctx.pop("turn", None)

    def current_turn(self, ts):
        turn = self.ctx.get("turn")
        if turn is None:  # lines before any task_started (resumed state lost): open one
            self.start_turn("untracked-%s" % core.det_id(self.fstate.get("path"), ts)[:12], ts)
            turn = self.ctx["turn"]
        return turn

    # ---- guardian (subagent) threads ----
    def subagent_parent(self, ts):
        """(trace_id, span_id) in the parent session for a guardian line at ts."""
        turn = self.ctx["turn"]
        if turn.get("span_id"):
            return turn["trace_id"], turn["span_id"]
        when = core.parse_ts(ts) or 0
        best = None
        for tid, (trace_id, start, end) in (self.sess.get("turns") or {}).items():
            if start is not None and start <= when and (end is None or when <= end + 1):
                if best is None or start > best[1]:
                    best = (trace_id, start)
        if best is None:  # no parent turn known: the guardian run gets a trace of its own
            trace_id = self.det("trace", "subagent", self.link["thread_id"], turn["turn_id"])
            md = self.meta_base()
            md.update({"turn_id": turn["turn_id"], "status": "orphan_subagent", "sidechain": True, "agent_type": self.link.get("agent_type")})
            self.emit("trace-create", {"id": trace_id, "timestamp": turn["start"], "name": TRACE_NAME, "userId": self.sess.get("user_id"),
                                       "sessionId": SESSION_PREFIX + self.session_id, "tags": list(TRACE_TAGS), "metadata": md})
        else:
            trace_id = best[0]
        span_id = self.det("subagent", self.link["thread_id"], turn["turn_id"])
        turn["trace_id"], turn["span_id"] = trace_id, span_id
        self.emit_subagent_span(None)
        return trace_id, span_id

    def emit_subagent_span(self, end_ts):
        turn = self.ctx["turn"]
        body = {
            "id": turn["span_id"],
            "traceId": turn["trace_id"],
            "name": SUBAGENT_SPAN_NAME,
            "startTime": turn["start"],
            "metadata": {"sidechain": True, "agent_type": self.link.get("agent_type"), "thread_id": self.link["thread_id"], "turn_id": turn["turn_id"]},
        }
        if end_ts:
            body["endTime"] = end_ts
        self.emit("span-create", body)

    def close_subagent_span(self, ts):
        turn = self.ctx.get("turn")
        if turn and turn.get("span_id"):
            self.emit_subagent_span(ts)

    # ---- model responses and tools ----
    def resp(self):
        return self.ctx.setdefault("resp", {"text": "", "text_len": 0, "blocks": {}, "tools": []})

    def on_message(self, p, ts):
        role = p.get("role")
        text = text_of(p.get("content"))
        if role == "user":
            turn = self.ctx.get("turn")
            if turn and not turn.get("subagent") and is_typed_prompt(text) and not turn["metadata"].get("prompt_chars"):
                turn["metadata"]["prompt_chars"] = len(text)
                turn["input"] = self.content(text)
                self.ctx["trigger_input"] = self.content(text)
                self.emit_turn()
        elif role == "assistant":
            r = self.resp()
            r["blocks"]["text"] = r["blocks"].get("text", 0) + 1
            if self.cfg.log_content and text:
                r["text_len"] += (1 if r["text_len"] else 0) + len(text)
                r["text"] = ((r["text"] + "\n" + text) if r["text"] else text)[: self.cfg.max_content_chars]

    def on_tool_call(self, p, ts):
        call_id = p.get("call_id")
        if not isinstance(call_id, str):
            return
        turn = self.current_turn(ts)
        if turn.get("subagent"):
            trace_id, _ = self.subagent_parent(ts)
        else:
            trace_id = turn["trace_id"]
        name = p.get("name") or ("custom_tool" if p.get("type") == "custom_tool_call" else "tool")
        r = self.resp()
        r["tools"].append(name)
        args = p.get("arguments") if p.get("type") == "function_call" else p.get("input")
        self.ctx.setdefault("calls", {})[call_id] = {
            "span_id": self.det("tool", call_id),
            "trace_id": trace_id,
            "parent": None,
            "name": name,
            "start": ts,
            "kind": p.get("type"),
            "input": self.content(args),
            "input_chars": core.text_size(args),
        }

    def on_tool_output(self, p, ts):
        call = (self.ctx.get("calls") or {}).pop(p.get("call_id"), None)
        self.ctx["boundary"] = ts
        if call is None:
            return  # output of a call from a part of the file we never saw
        out = p.get("output")
        md = {"call_id": p.get("call_id"), "tool_kind": call["kind"], "input_chars": call["input_chars"], "output_chars": core.text_size(out)}
        if self.link is not None:
            md.update({"sidechain": True, "agent_type": self.link.get("agent_type")})
        body = {
            "id": call["span_id"],
            "traceId": call["trace_id"],
            "name": call["name"],
            "startTime": call["start"],
            "endTime": ts,
            "metadata": md,
        }
        if call.get("parent"):
            body["parentObservationId"] = call["parent"]
        if call.get("input") is not None:
            body["input"] = call["input"]
        if self.cfg.log_content:
            body["output"] = self.content(text_of(out) if isinstance(out, list) else out)
        self.emit("span-create", body)

    def on_token_count(self, p, ts, ordinal):
        """Older Codex versions (before token_usage_record existed) report each response only
        as a token_count event whose running total changed; an unchanged total is a repeat.
        Once a file has a token_usage_record, those are used and token_count is ignored."""
        info = p.get("info") if isinstance(p.get("info"), dict) else None
        if info is None:
            return
        key = json.dumps(info.get("total_token_usage"), sort_keys=True)
        if key == self.ctx.get("last_total"):
            return
        self.ctx["last_total"] = key
        if self.ctx.get("has_records"):
            return
        thread = (self.ctx.get("meta") or {}).get("thread_id") or self.fstate.get("path")
        self.make_generation("tc-%s-%s" % (thread, ordinal if ordinal is not None else ts), info.get("last_token_usage"), None, ts)

    def on_usage_record(self, p, ts):
        response_id = p.get("response_id")
        if not isinstance(response_id, str):
            return
        self.ctx["has_records"] = True
        self.make_generation(response_id, p.get("usage"), p.get("turn_id"), ts)

    def make_generation(self, response_id, usage, turn_id, ts):
        turn = self.current_turn(ts)
        if turn.get("subagent"):
            trace_id, parent = self.subagent_parent(ts)
        else:
            trace_id, parent = turn["trace_id"], None
        r = self.ctx.pop("resp", None) or {"text": "", "text_len": 0, "blocks": {}, "tools": []}
        gen_id = self.det("gen", response_id)
        start = self.ctx.get("boundary") or ts
        st, en = core.parse_ts(start), core.parse_ts(ts)
        if st is None or en is None or st > en:
            start = ts
        md = {"response_id": response_id, "turn_id": turn_id or turn.get("turn_id"), "content_blocks": r["blocks"]}
        if r["tools"]:
            md["tool_calls"] = r["tools"]
        if self.link is not None:
            md.update({"sidechain": True, "agent_type": self.link.get("agent_type"), "thread_id": self.link["thread_id"]})
        body = {
            "id": gen_id,
            "traceId": trace_id,
            "name": GENERATION_NAME,
            "model": turn.get("model") or self.ctx.get("model"),
            "startTime": start,
            "endTime": ts,
            "usageDetails": codex_usage_details(usage),
            "metadata": md,
        }
        if parent:
            body["parentObservationId"] = parent
        if self.cfg.log_content:
            if self.ctx.get("trigger_input") is not None:
                body["input"] = self.ctx["trigger_input"]
            if r["text"]:
                text = r["text"] + ("... [truncated %d chars]" % (r["text_len"] - len(r["text"])) if r["text_len"] > len(r["text"]) else "")
                body["output"] = core.redact(text, self.cfg)
        self.emit("generation-create", body)
        self.ctx["trigger_input"] = None
        for call in (self.ctx.get("calls") or {}).values():
            if call["parent"] is None:
                call["parent"] = gen_id
        self.ctx["boundary"] = ts

    def on_item_completed(self, p):
        turn = self.ctx.get("turn")
        item = p.get("item") if isinstance(p.get("item"), dict) else {}
        if not turn or turn.get("subagent"):
            return
        md = turn["metadata"]
        kind = item.get("type")
        if kind == "CommandExecution":
            md["commands"] = md.get("commands", 0) + 1
            if item.get("status") == "failed" or (isinstance(item.get("exit_code"), int) and item["exit_code"] != 0):
                md["commands_failed"] = md.get("commands_failed", 0) + 1
        elif kind == "FileChange":
            md["file_changes"] = md.get("file_changes", 0) + 1
        elif kind == "WebSearch":
            md["web_searches"] = md.get("web_searches", 0) + 1

    # ---- dispatch ----
    def consume(self, e):
        t = e.get("type")
        p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
        pt = p.get("type")
        ts = e.get("timestamp") or core.now_iso()
        if t == "session_meta":
            self.on_session_meta(p)
        elif t == "turn_context":
            turn = self.current_turn(ts)
            if isinstance(p.get("model"), str):
                turn["model"] = self.ctx["model"] = p["model"]
                if not turn.get("subagent"):
                    turn["metadata"]["model"] = p["model"]
                    if p.get("effort"):
                        turn["metadata"]["effort"] = str(p["effort"])
                    self.emit_turn()
        elif t == "event_msg" and pt == "task_started":
            self.start_turn(p.get("turn_id") or "turn-%s" % ts, p.get("started_at") if isinstance(p.get("started_at"), str) else ts)
        elif t == "event_msg" and pt == "task_complete":
            self.finish_turn(p, "completed", ts)
        elif t == "event_msg" and pt == "turn_aborted":
            self.finish_turn(p, "aborted", ts)
        elif t == "event_msg" and pt == "item_completed":
            self.on_item_completed(p)
        elif t == "response_item" and pt == "message":
            self.on_message(p, ts)
        elif t == "response_item" and pt == "reasoning":
            r = self.resp()
            r["blocks"]["reasoning"] = r["blocks"].get("reasoning", 0) + 1  # never sent: encrypted
        elif t == "response_item" and pt in ("function_call", "custom_tool_call"):
            self.on_tool_call(p, ts)
        elif t == "response_item" and pt in ("function_call_output", "custom_tool_call_output"):
            self.on_tool_output(p, ts)
        elif t == "token_usage_record":
            self.on_usage_record(p, ts)
        elif t == "event_msg" and pt == "token_count":
            self.on_token_count(p, ts, e.get("ordinal"))
        # everything else (world_state, compaction, thread settings, ...) is ignored


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

def process_file(cfg, sender, st, session_id, path, link=None):
    """Send the new lines of one rollout file in batches. False when a send failed."""
    sess = st["sessions"][session_id]
    key = os.path.basename(path)
    fstate = sess["files"].setdefault(key, {"path": path, "offset": 0, "ordinal": -1, "ctx": {}})
    fstate["path"] = path
    entries, end, ordinal = new_lines(path, fstate)
    if end == fstate.get("offset") and not entries:
        return True
    work = copy.deepcopy(sess)
    wfile = work["files"][key]
    mapper = Mapper(cfg, work, wfile, link)

    def commit():
        wfile["offset"], wfile["ordinal"] = end, ordinal
        work["updated"] = time.time()
        st["sessions"][session_id] = copy.deepcopy(work)

    for entry in entries:
        try:
            mapper.consume(entry)
        except Exception as exc:
            log_error("could not map a line of %s: %s: %s" % (key, type(exc).__name__, exc))
    events = mapper.take_events()
    if events and not sender.send(events):
        return False
    commit()
    return True


MAX_CHILD_DAYS = 31


def day_folders(parent_path, today=None):
    """The parent's day folder (sessions/YYYY/MM/DD) and every later one up to today, at most
    MAX_CHILD_DAYS: guardian threads of a long session land in the folder of the day they ran."""
    import datetime as _dt
    folder = os.path.dirname(parent_path)
    parts = folder.replace("\\", "/").rstrip("/").split("/")[-3:]
    try:
        day = _dt.date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        return [folder]
    sessions = os.path.dirname(os.path.dirname(os.path.dirname(folder)))
    today = today or _dt.date.today()
    out = []
    while day <= today and len(out) < MAX_CHILD_DAYS:
        out.append(os.path.join(sessions, "%04d" % day.year, "%02d" % day.month, "%02d" % day.day))
        day += _dt.timedelta(days=1)
    return out or [folder]


def guardian_children(sess, parent_path, session_id):
    """Rollout files that belong to this session as subagent threads (guardian reviews), in
    the parent's day folder or a later one. Each file's first line is read once and remembered."""
    known = sess.setdefault("classified", {})
    children = []
    candidates = []
    for folder in day_folders(parent_path):
        try:
            names = sorted(n for n in os.listdir(folder) if n.startswith("rollout-") and n.endswith(".jsonl"))
        except OSError:
            continue
        candidates.extend(os.path.join(folder, n) for n in names)
    for path in candidates:
        name = os.path.basename(path)
        if path == parent_path:
            continue
        info = known.get(name)
        if info is None:
            first = read_first_entry(path)
            p = (first or {}).get("payload") or {}
            if not first:
                continue  # still being written: look again next time
            info = {"session": p.get("session_id"), "parent": p.get("parent_thread_id"), "thread": p.get("id"),
                    "agent_type": _agent_type(p.get("source"))}
            known[name] = info
        if info.get("parent") == session_id and info.get("session") == session_id:
            children.append((path, info))
    if len(known) > 2000:
        for name in sorted(known)[: len(known) - 2000]:
            del known[name]
    return children


def _agent_type(source):
    if isinstance(source, dict):
        sub = source.get("subagent")
        if isinstance(sub, dict):
            return str(sub.get("other") or next(iter(sub.values()), "subagent"))
        if sub:
            return str(sub)
    return "subagent"


def process_thread(cfg, payload, st, sender):
    thread_id = payload.get("thread-id")
    path = find_rollout(thread_id)
    if not path:
        debug("no rollout file for thread %s (helper thread or not saved)" % thread_id)
        return True
    first = read_first_entry(path) or {}
    meta = first.get("payload") or {}
    session_id = meta.get("session_id") or thread_id
    if meta.get("parent_thread_id"):  # notify for a subagent thread: process its parent instead
        parent_path = find_rollout(meta["parent_thread_id"])
        if parent_path:
            path, session_id = parent_path, meta["parent_thread_id"]
    sess = st["sessions"].setdefault(session_id, {"session_id": session_id, "files": {}, "turns": {}, "updated": time.time()})
    sess.setdefault("files", {})
    sess.setdefault("turns", {})
    if not sess.get("user_id"):
        sess["user_id"] = core.resolve_user(cfg, payload.get("cwd") or meta.get("cwd"), IS_WINDOWS)
    if not process_file(cfg, sender, st, session_id, path):
        return False
    for child_path, info in guardian_children(st["sessions"][session_id], path, session_id):
        link = {"session_id": session_id, "thread_id": info.get("thread"), "agent_type": info.get("agent_type")}
        if not process_file(cfg, sender, st, session_id, child_path, link):
            return False
    return True


def run_worker(payload, cfg):
    wait = cfg.timeout + core.LOCK_WAIT_EXTRA_SECONDS
    with core.file_lock(lock_path(), wait=wait) as acquired:
        if not acquired:
            log_error("another run held the lock for %.0fs; this run is skipped (its data goes next time)" % wait)
            return
        st = core.load_state(state_path(), log_error)
        core.cleanup_state(st)
        if cfg.timeout_warning and st.get("timeout_warning") != cfg.timeout_warning:
            log_error(cfg.timeout_warning)
        st["timeout_warning"] = cfg.timeout_warning
        sender = make_sender(cfg)
        ok = True
        try:
            ok = process_thread(cfg, payload, st, sender)
        finally:
            if sender.last is not None:
                st["last_send"] = dict(sender.last, thread_id=payload.get("thread-id"), client=payload.get("client"))
            core.save_state(st, state_path())
        debug("done: %s, %d event(s) sent" % ("ok" if ok else "will retry", sender.sent_events))


# ---------------------------------------------------------------------------
# notify entry: chain, detach, return
# ---------------------------------------------------------------------------

def chained_notify():
    """The developer's previous notify argv, saved by setup in ~/.agentguard/config.json."""
    try:
        with open(core.config_file_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        chain = ((data.get("codex") or {}).get("chain_notify")) if isinstance(data, dict) else None
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(chain, list) and chain and all(isinstance(a, str) for a in chain):
        return chain
    return None


def notify_main(argv):
    """Codex's notify: argv[-1] is the JSON payload. Never prints, never raises."""
    global _CFG
    raw = argv[-1] if argv else ""
    chain = chained_notify()
    if chain:  # keep the developer's own notify program working: same argument, same environment
        try:
            core.spawn_detached(chain + [raw], dict(os.environ), IS_WINDOWS)
        except OSError as exc:
            log_error("could not run the previous notify program %s: %s" % (os.path.basename(chain[0]), exc))
    cfg, _sources = core.load_config()
    _CFG = cfg
    if not cfg.has_keys:
        return
    try:
        payload = json.loads(raw)
    except ValueError:
        log_error("notify payload was not JSON")
        return
    if not isinstance(payload, dict) or payload.get("type") not in (None, "agent-turn-complete"):
        return
    payload = {k: payload[k] for k in WORKER_PAYLOAD_KEYS if k in payload}
    if cfg.debug or (os.environ.get("AGENTGUARD_SYNC") or "").strip().lower() in core.TRUE_VALUES:
        run_worker(payload, cfg)
        return
    env = dict(os.environ)
    env["AGENTGUARD_HOOK_PAYLOAD"] = json.dumps(payload)
    try:
        core.spawn_detached([sys.executable, os.path.abspath(__file__), "--worker"], env, IS_WINDOWS)
    except OSError as exc:
        log_error("could not start the background worker: %s: %s" % (type(exc).__name__, exc))


def worker_main():
    global _CFG
    cfg, _sources = core.load_config()
    _CFG = cfg
    if not cfg.has_keys:
        return
    try:
        payload = json.loads(os.environ.get("AGENTGUARD_HOOK_PAYLOAD") or "{}")
    except ValueError:
        log_error("worker payload was not JSON")
        return
    if isinstance(payload, dict):
        run_worker(payload, cfg)


# ---------------------------------------------------------------------------
# ~/.codex/config.toml: our notify entry (used by `agentguard setup` / `uninstall`)
# ---------------------------------------------------------------------------

def config_toml_path():
    return os.path.join(codex_home(), "config.toml")


_NOTIFY_LINE = re.compile(r"^(\s*)notify\s*=\s*(.*?)\s*$")


def _parse_toml_string_array(text):
    """A one-line TOML array of strings (basic "..." or literal '...'), or None if the text
    is anything else. Deliberately small: anything unusual is left to the developer."""
    text = text.strip()
    if "#" in text:  # strip a trailing comment outside strings
        out, quote = [], None
        for i, ch in enumerate(text):
            if quote:
                if ch == quote and (quote == "'" or text[i - 1] != "\\"):
                    quote = None
            elif ch in "\"'":
                quote = ch
            elif ch == "#":
                text = text[:i].strip()
                break
    if not (text.startswith("[") and text.endswith("]")):
        return None
    body, items, i = text[1:-1].strip(), [], 0
    while i < len(body):
        ch = body[i]
        if ch in " \t,":
            i += 1
            continue
        if ch == "'":
            j = body.find("'", i + 1)
            if j < 0:
                return None
            items.append(body[i + 1:j])
            i = j + 1
        elif ch == '"':
            j, buf = i + 1, ""
            while j < len(body) and body[j] != '"':
                if body[j] == "\\" and j + 1 < len(body):
                    j += 1
                    buf += {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(body[j], "\\" + body[j])
                else:
                    buf += body[j]
                j += 1
            if j >= len(body):
                return None
            items.append(buf)
            i = j + 1
        else:
            return None
    return items


def _toml_array(items):
    return "[" + ", ".join(json.dumps(a) for a in items) + "]"


def find_notify(text):
    """(line index, argv or None if not parseable) of the top-level notify key, or None."""
    lines = text.splitlines(True)
    for i, line in enumerate(lines):
        if line.lstrip().startswith("["):
            return None  # top-level keys end at the first table
        m = _NOTIFY_LINE.match(line.rstrip("\r\n"))
        if m and not line.lstrip().startswith("#"):
            return i, _parse_toml_string_array(m.group(2))
    return None


def configure_notify(our_argv, read=None, write=None):
    """Make Codex run `our_argv` as its notify program, keeping everything else in
    config.toml byte for byte. Returns (changed, chain_argv_or_None, message).

    - no notify yet: add ours above the first table;
    - ours already: nothing changes (idempotent);
    - another program: ours replaces it, and its argv is returned so setup can save it as
      the program to chain to (we keep calling it, with the same argument and environment);
    - a notify we can't parse (multi-line, or not strings): nothing changes, and the
      message says why."""
    path = config_toml_path()
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:  # keep CRLF as it is
            text = fh.read()
    except FileNotFoundError:
        text = ""
    found = find_notify(text)
    lines = text.splitlines(True)
    newline = "\r\n" if "\r\n" in text else "\n"
    entry = "notify = %s  # AgentGuard (agentguard setup)%s" % (_toml_array(our_argv), newline)
    chain = None
    if found is None:
        lines.insert(0, entry)
        message = "added notify to %s" % path
    else:
        index, current = found
        if current is None:
            return False, None, ("%s has a notify setting this tool cannot read safely (it spans several lines or is not a list of strings). "
                                 "Nothing was changed; set notify to %s by hand." % (path, _toml_array(our_argv)))
        if current == list(our_argv):
            return False, None, "notify in %s already runs AgentGuard" % path
        if "agentguard" in " ".join(current).lower() and "codex" in " ".join(current).lower():
            message = "updated the AgentGuard notify entry in %s" % path  # an older install of ours
        else:
            chain = current
            message = "replaced notify in %s; AgentGuard calls the previous program (%s) after each turn" % (path, os.path.basename(current[0]) if current else "?")
        lines[index] = entry
    new_text = "".join(lines)
    backup = path + ".agentguard-backup"
    core.ensure_dir(os.path.dirname(path))
    if os.path.exists(path) and not os.path.exists(backup):
        with open(path, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
    tmp = path + ".agentguard-tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(new_text)
    core.replace_file(tmp, path, is_windows=IS_WINDOWS)
    return True, chain, message


def remove_notify(chain=None):
    """Undo configure_notify: put the chained program back, or remove our line. Returns
    (changed, message). Lines that are not ours are never touched."""
    path = config_toml_path()
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:  # keep CRLF as it is
            text = fh.read()
    except FileNotFoundError:
        return False, "%s does not exist" % path
    found = find_notify(text)
    lines = text.splitlines(True)
    if found is None or found[1] is None or "agentguard" not in lines[found[0]].lower():
        return False, "no AgentGuard notify entry in %s" % path
    newline = "\r\n" if lines[found[0]].endswith("\r\n") else "\n"
    if chain:
        lines[found[0]] = "notify = %s%s" % (_toml_array(chain), newline)
        message = "restored the previous notify program in %s" % path
    else:
        del lines[found[0]]
        message = "removed the AgentGuard notify entry from %s" % path
    tmp = path + ".agentguard-tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write("".join(lines))
    core.replace_file(tmp, path, is_windows=IS_WINDOWS)
    return True, message


# ---------------------------------------------------------------------------
# Install: stable script copy and the notify command for this platform
# ---------------------------------------------------------------------------

# What Codex's notify needs from this folder.
INSTALL_FILES = ("agentguard_core.py", "codex_trace.py", "agentguard.sh", "agentguard.ps1")


def scripts_home():
    """Where setup copies the scripts. Stable across plugin updates, unlike the Claude
    plugin's own folder, whose path contains the plugin version."""
    return os.path.join(core.agentguard_home(), "scripts")


def install_scripts(dest=None):
    src = os.path.dirname(os.path.abspath(__file__))
    dest = dest or scripts_home()
    core.ensure_dir(dest)
    if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dest)):
        return dest
    for name in INSTALL_FILES:
        tmp = os.path.join(dest, name + ".tmp")
        shutil.copyfile(os.path.join(src, name), tmp)
        core.replace_file(tmp, os.path.join(dest, name), is_windows=IS_WINDOWS)
    return dest


def windows_pyw():
    """pyw.exe, the windowless Python launcher, if `py -3` starts Python 3.8 or newer. It keeps
    working when Python is upgraded, and opens no console window after every Codex turn."""
    pyw, py = shutil.which("pyw"), shutil.which("py")
    if not pyw or not py:
        return None
    try:
        proc = subprocess.run([py, "-3", "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=20, **({"creationflags": core.CREATE_NO_WINDOW} if IS_WINDOWS else {}))
    except (OSError, subprocess.SubprocessError):
        return None
    return pyw if proc.returncode == 0 else None


def notify_argv(scripts_dir, is_windows=None):
    """The program Codex should run after each turn (Codex appends the JSON payload).

    macOS / Linux: the sh launcher, which finds Python on every run (so a Python upgrade
    does not break it) and stays silent if there is none.
    Windows: Codex starts the program directly, so it must be a real .exe, and it should
    open no console window: pyw.exe -3, else pythonw.exe next to the Python running setup.
    (Not PowerShell: Windows PowerShell 5.1 can mangle quotes in a JSON argument.)"""
    is_windows = IS_WINDOWS if is_windows is None else is_windows
    if not is_windows:
        return ["/bin/sh", os.path.join(scripts_dir, "agentguard.sh"), "codex", "notify"]
    script = os.path.join(scripts_dir, "codex_trace.py")
    pyw = windows_pyw()
    if pyw:
        return [pyw, "-3", script, "notify"]
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if os.path.isfile(pythonw):
        return [pythonw, script, "notify"]
    return [sys.executable, script, "notify"]


# ---------------------------------------------------------------------------
# Setup and uninstall
# ---------------------------------------------------------------------------

def _read_secret_line():
    stream = getattr(sys.stdin, "buffer", None)
    line = stream.readline().decode("utf-8-sig", "replace") if stream is not None else sys.stdin.readline()
    return line.lstrip("\ufeff").strip()


def setup_main(argv):
    """Returns 0 when Codex is set up; 2 bad input; 3 keys refused or host unreachable;
    4 a config file could not be read or written. Never prints the secret key."""
    opts = {"host": None, "public_key": None, "secret_stdin": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--host", "--public-key") and i + 1 < len(argv):
            opts[a[2:].replace("-", "_")] = argv[i + 1]
            i += 2
            continue
        if a == "--secret-stdin":
            opts["secret_stdin"] = True
        i += 1
    try:
        saved = core.read_config_file(core.config_file_path())
    except ValueError as exc:
        print("ERROR: %s. Fix or delete it and run setup again; nothing was written." % exc)
        return 4
    current, _sources = core.load_config()
    interactive = sys.stdin is not None and sys.stdin.isatty() and not opts["secret_stdin"]
    host = opts["host"] or current.host
    public_key = opts["public_key"] or current.public_key
    secret = None
    if opts["secret_stdin"]:
        secret = _read_secret_line()
    elif interactive:
        host = input("AgentGuard host [%s]: " % host).strip() or host
        public_key = input("Public key%s: " % (" [%s]" % core.mask(public_key) if public_key else " (pk-ag-...)")).strip() or public_key
        secret = getpass.getpass("Secret key%s (hidden, nothing appears while you paste): " % (" [Enter keeps the saved one]" if current.secret_key else " (sk-ag-...)")).strip()
    secret = secret or current.secret_key
    host = (host or core.DEFAULT_HOST).strip().rstrip("/")
    if not re.match(r"^https?://[^\s/]+", host):
        print("ERROR: host must start with https:// (got %r)" % host)
        return 2
    if not public_key or not secret:
        print("ERROR: public key and secret key are both required.")
        return 2
    cfg = core.Config({"AGENTGUARD_HOST": host, "AGENTGUARD_PUBLIC_KEY": public_key, "AGENTGUARD_SECRET_KEY": secret})
    global _CFG
    _CFG = cfg
    ok, message = core.test_keys(cfg, http_request)
    print(("OK: " if ok else "ERROR: ") + message)
    if not ok:
        print("Nothing was changed.")
        return 3
    try:
        if (saved.get("AGENTGUARD_HOST"), saved.get("AGENTGUARD_PUBLIC_KEY"), saved.get("AGENTGUARD_SECRET_KEY")) != (host, public_key, secret):
            core.update_config_file({"host": host, "public_key": public_key, "secret_key": secret}, is_windows=IS_WINDOWS)
            print("Saved the keys in %s (readable only by you)." % core.config_file_path())
        dest = install_scripts()
        argv_ours = notify_argv(dest)
        changed, chain, msg = configure_notify(argv_ours)
        print(msg[0].upper() + msg[1:] + ".")
        if chain:
            core.update_config_file({"codex": {"chain_notify": chain}}, is_windows=IS_WINDOWS)
            print("Your previous notify program (%s) keeps running after each turn." % os.path.basename(chain[0]))
        if "cannot read safely" in msg:
            return 4
    except (OSError, ValueError) as exc:
        print("ERROR: %s" % exc)
        return 4
    print("Codex runs: %s" % " ".join(argv_ours))
    print("Restart Codex (quit VS Code completely, or restart the Codex CLI or app). Each finished turn is then sent to AgentGuard.")
    return 0


def uninstall_main():
    try:
        data = core.read_config_json(core.config_file_path())
    except ValueError:
        data = {}
    chain = (data.get("codex") or {}).get("chain_notify") if isinstance(data.get("codex"), dict) else None
    try:
        changed, msg = remove_notify(chain)
        if changed and chain:
            core.update_config_file({"codex": {"chain_notify": None}}, is_windows=IS_WINDOWS)
    except (OSError, ValueError) as exc:
        print("ERROR: %s" % exc)
        return 4
    print(msg[0].upper() + msg[1:] + ".")
    print("Keys in %s and the scripts in %s are kept for other tools." % (core.config_file_path(), scripts_home()))
    return 0


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def status_lines():
    cfg, sources = core.load_config()
    lines = ["Codex: rollout files in %s" % os.path.join(codex_home(), "sessions")]
    found = None
    try:
        with open(config_toml_path(), "r", encoding="utf-8") as fh:
            found = find_notify(fh.read())
    except OSError:
        pass
    if found and found[1] and any("codex_trace" in a or ("agentguard" in a.lower()) for a in found[1]):
        lines.append("notify: AgentGuard is configured in %s" % config_toml_path())
        missing = [a for a in found[1][:3] if (os.path.isabs(a) or a.lower().endswith(".exe")) and not os.path.exists(a)]
        if missing:
            lines.append("WARNING: notify points to %s, which does not exist (Python moved or was upgraded?). Run setup again." % missing[0])
    else:
        lines.append("notify: AgentGuard is NOT configured in %s (run agentguard setup)" % config_toml_path())
    if chained_notify():
        lines.append("Previous notify program, still called after each turn: %s" % chained_notify()[0])
    lines.append("Keys: %s" % ("set (%s)" % sources.get("AGENTGUARD_SECRET_KEY") if cfg.has_keys else "not set"))
    st = core.load_state(state_path())
    last = st.get("last_send")
    if last:
        lines.append("Last send: %s, %s%s: %s" % (last.get("time"), "OK" if last.get("ok") else "FAILED", " (HTTP %s)" % last["status"] if last.get("status") else "", last.get("detail")))
    else:
        lines.append("Last send: never")
    try:
        with open(error_log_path(), "r", encoding="utf-8") as fh:
            tail = fh.readlines()[-5:]
        lines.append("Last error log lines (%s):" % error_log_path())
        lines.extend("  " + core.redact(l.rstrip("\n"), cfg) for l in tail)
    except OSError:
        pass
    return lines


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "notify":
        try:
            notify_main(argv[1:])
        except BaseException as exc:  # noqa: B036 - nothing may escape a notify program
            log_error("notify: %s: %s" % (type(exc).__name__, exc))
        return 0
    if argv == ["--worker"]:
        try:
            worker_main()
        except BaseException as exc:
            log_error("worker: %s: %s" % (type(exc).__name__, exc))
        return 0
    if argv == ["--status"]:
        for line in status_lines():
            print(line)
        return 0
    if argv and argv[0] == "--setup":
        return setup_main(argv[1:])
    if argv == ["--uninstall"]:
        return uninstall_main()
    # Codex may also call the program with the JSON as the only argument
    if len(argv) == 1 and argv[0].lstrip().startswith("{"):
        try:
            notify_main(argv)
        except BaseException as exc:
            log_error("notify: %s: %s" % (type(exc).__name__, exc))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
