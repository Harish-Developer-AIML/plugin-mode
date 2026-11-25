#!/usr/bin/env python3
"""Send Claude Code session data to AgentGuard (Langfuse public ingestion API).

Modes
  (no arguments)  Hook mode, run by the Stop and SessionEnd hooks. Reads the hook JSON
                  on stdin, hands the work to a detached background process and exits 0
                  at once, so Claude Code is never slowed down.
  --worker        The background process: reads new transcript lines, maps them to
                  traces, generations and spans, and sends them.
  --status        Prints the plugin's state for /agentguard:status.
  --setup         Tests keys and writes them to ~/.claude/settings.json (/agentguard:setup).
  --check-otel    Lists old OpenTelemetry settings that would duplicate traces.

Python 3.8+ standard library only. Hook and worker modes never print anything (unless
AGENTGUARD_DEBUG=true), never raise, and always exit 0.

The agent-independent parts (config, redaction, HTTP, state, locking, spawning, status
helpers) live in agentguard_core.py next to this file. The names below that wrap them are
kept on this module on purpose: they pass this adapter's own paths and settings in.
"""

import argparse
import copy
import getpass
import json
import os
import re
import subprocess  # noqa: F401  (tests patch subprocess through this module)
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agentguard_core as core  # noqa: E402
from agentguard_core import (  # noqa: E402,F401  (re-exported: part of this module's interface)
    COMPOSE_DIR_LABEL,
    CREATE_BREAKAWAY_FROM_JOB,
    CREATE_NEW_PROCESS_GROUP,
    CREATE_NO_WINDOW,
    DEFAULT_HOST,
    DEFAULT_MAX_CONTENT_CHARS,
    DEFAULT_TIMEOUT_SECONDS,
    DETACHED_PROCESS,
    KEY_PATTERN,
    LOCK_WAIT_EXTRA_SECONDS,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    STATE_RETENTION_DAYS,
    TRUE_VALUES,
    Config,
    auth_header,
    cleanup_state,
    det_id,
    empty_state,
    ensure_dir,
    envelope,
    file_lock,
    mask,
    mask_secret,
    now_iso,
    parse_timeout,
    parse_ts,
    port_open,
    publishes_port,
    text_size,
    truncate,
)

PLUGIN_VERSION = "0.1.1"

# ---------------------------------------------------------------------------
# Usage keys. These names must match the usage keys in AgentGuard's model price
# table, or cost comes out as zero. Keys on the left are Anthropic's names in
# message.usage (and message.usage.cache_creation); keys on the right are what
# is sent as usageDetails. Set a value to None to stop sending that key.
# ---------------------------------------------------------------------------
USAGE_KEYS = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_read_input_tokens": "cache_read_input_tokens",
    "cache_creation_input_tokens": "cache_creation_input_tokens",
    # Cache writes broken down by cache lifetime. Claude Code writes its cache with
    # the 1-hour lifetime, which costs more than the 5-minute one (see NOTES.md).
    "ephemeral_5m_input_tokens": "input_cache_creation_5m",
    "ephemeral_1h_input_tokens": "input_cache_creation_1h",
}
# True: send cache writes as the 5m/1h keys when the breakdown is present, and only
# fall back to cache_creation_input_tokens without it. False: always send the single
# cache_creation_input_tokens key. Never both, so nothing is counted twice.
SPLIT_CACHE_CREATION_BY_TTL = True

TRACE_NAME = "claude_code.turn"
GENERATION_NAME = "claude_code.llm_call"
TRACE_TAGS = ["claude-code"]
# Settings this adapter passes to the core. Tests change them on this module.
MAX_BATCH_EVENTS = core.MAX_BATCH_EVENTS
MAX_BATCH_BYTES = core.MAX_BATCH_BYTES
ERROR_LOG_MAX_BYTES = core.ERROR_LOG_MAX_BYTES
IS_WINDOWS = core.IS_WINDOWS
OTEL_COLLECTOR_PORT = core.OTEL_COLLECTOR_PORT
AGENT_TOOL_NAMES = ("Agent", "Task")
# The only hook input fields the worker reads. The rest (Stop's last_assistant_message can be
# megabytes) must not be passed on: the payload travels in an environment variable, and
# environment size is limited (128 KB per variable on Linux, about 1 MB in total on macOS).
WORKER_PAYLOAD_KEYS = ("session_id", "transcript_path", "hook_event_name", "cwd")
OTEL_VAR_PATTERN = re.compile(r"^(CLAUDE_CODE_ENABLE_TELEMETRY|OTEL_[A-Z0-9_]+)$")  # Claude Code's own telemetry

# User entries that are not new turns (prefixes of their text).
NON_TURN_PREFIXES = (
    "[Request interrupted by user",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
)


# ---------------------------------------------------------------------------
# Paths, config, logging (this adapter's files; the logic is in the core)
# ---------------------------------------------------------------------------

def claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def data_dir():
    return os.environ.get("AGENTGUARD_HOME") or os.path.join(claude_dir(), "agentguard")


def state_path():
    return os.path.join(data_dir(), "state.json")


def error_log_path():
    return os.path.join(data_dir(), "error.log")


def settings_path():
    return os.path.join(claude_dir(), "settings.json")


def env_true(name):
    return (os.environ.get(name) or "").strip().lower() in TRUE_VALUES


_CFG = None  # set in main so logging can redact the secret


def redact(text, cfg=None):
    return core.redact(text, cfg or _CFG)


def replace_file(src, dst, attempts=10):
    return core.replace_file(src, dst, attempts, IS_WINDOWS)


def debug(msg):
    core.debug(msg, _CFG)


def log_error(msg):
    """Append to error.log (rotated at 1 MB). Never raises."""
    core.log_error(msg, error_log_path(), ERROR_LOG_MAX_BYTES, replace=replace_file, cfg=_CFG)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def blocks_text(content):
    """Text of a message content (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" and isinstance(b.get("text"), str):
            parts.append(b["text"])
        elif t == "image":
            parts.append("[image]")
        elif t == "document":
            parts.append("[document]")
        elif t == "tool_result":
            parts.append(blocks_text(b.get("content")))
    return "\n".join(p for p in parts if p)


def build_usage_details(usage):
    """message.usage -> usageDetails, using USAGE_KEYS. Ints only."""
    out = {}
    if not isinstance(usage, dict):
        return out

    def put(src, value):
        dst = USAGE_KEYS.get(src)
        if dst and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[dst] = out.get(dst, 0) + value

    for src in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
        put(src, usage.get(src))

    total_cc = usage.get("cache_creation_input_tokens")
    cc = usage.get("cache_creation")
    five_key, hour_key = USAGE_KEYS.get("ephemeral_5m_input_tokens"), USAGE_KEYS.get("ephemeral_1h_input_tokens")
    if (
        SPLIT_CACHE_CREATION_BY_TTL
        and five_key
        and hour_key
        and isinstance(cc, dict)
        and any(isinstance(cc.get(k), int) for k in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"))
    ):
        five = cc.get("ephemeral_5m_input_tokens") if isinstance(cc.get("ephemeral_5m_input_tokens"), int) else 0
        hour = cc.get("ephemeral_1h_input_tokens") if isinstance(cc.get("ephemeral_1h_input_tokens"), int) else 0
        if isinstance(total_cc, int) and total_cc > five + hour:
            five += total_cc - (five + hour)  # unexplained remainder: price at the 5m rate
        if five:
            put("ephemeral_5m_input_tokens", five)
        if hour:
            put("ephemeral_1h_input_tokens", hour)
    else:
        put("cache_creation_input_tokens", total_cc)
    return out


# ---------------------------------------------------------------------------
# State and locking
# ---------------------------------------------------------------------------

def load_state():
    return core.load_state(state_path(), log_error)


def save_state(st):
    core.save_state(st, state_path(), replace=replace_file)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def http_request(cfg, method, path, body=None, timeout=DEFAULT_TIMEOUT_SECONDS):
    """Returns (status, parsed_json_or_text). Raises on network errors and timeouts."""
    return core.http_request(cfg, method, path, body, timeout, user_agent="agentguard-claude-code/%s" % PLUGIN_VERSION)


def split_batches(events):
    return core.split_batches(events, MAX_BATCH_EVENTS, MAX_BATCH_BYTES)


class Sender(core.Sender):
    """Sends ingestion batches within one shared time budget, with this adapter's error
    log, batch limits and User-Agent."""

    def __init__(self, cfg, budget=None):
        core.Sender.__init__(
            self, cfg, budget=budget,
            log=lambda msg: log_error(msg),
            debug_log=lambda msg: debug(msg),
            split=lambda events: split_batches(events),
            request=lambda *a, **k: http_request(*a, **k),
        )


# ---------------------------------------------------------------------------
# Transcript -> events
# ---------------------------------------------------------------------------

def prompt_origin(entry, text):
    origin = entry.get("turnOrigin")
    stripped = text.lstrip()
    if stripped.startswith("<task-notification>"):
        return "task_notification"
    if stripped.startswith("<command-name>") or stripped.startswith("<command-message>"):
        return "command"
    if isinstance(origin, str) and origin:
        return origin
    return "human"


def is_turn_start(entry):
    """A user entry that begins a new turn (a prompt, not a tool result)."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isCompactSummary") or entry.get("isVisibleInTranscriptOnly"):
        return False
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return False
    elif not isinstance(content, str):
        return False
    text = blocks_text(content).lstrip()
    return not text.startswith(NON_TURN_PREFIXES)


def command_name(text):
    m = re.search(r"<command-name>\s*([^<\s]+)\s*</command-name>", text)
    return m.group(1) if m else None


class Mapper(object):
    """Turns transcript lines of one file into ingestion events.

    All mutable progress lives in `sess` (the session's entry in state.json), so a
    batch that fails to send can be discarded by restoring an earlier copy of it.
    """

    def __init__(self, cfg, session_id, sess, file_key, link=None):
        self.cfg = cfg
        self.session_id = session_id
        self.sess = sess
        self.file_key = file_key
        self.link = link  # for subagent files: {"trace_id", "span_id", "agent_id", "agent_type"}
        self.fstate = sess["files"].setdefault(file_key, {"offset": 0, "ctx": {}})
        self.ctx = self.fstate.setdefault("ctx", {})
        self.events = {}  # (type, body id) -> event; later versions replace earlier ones
        self.order = []
        self.dirty_turn = False

    # ---- content helpers -------------------------------------------------
    def content(self, value):
        """Value to send as input/output, or None when content logging is off."""
        if not self.cfg.log_content or value is None:
            return None
        limit = self.cfg.max_content_chars
        if isinstance(value, str):
            return redact(truncate(value, limit), self.cfg)
        try:
            dumped = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            dumped = str(value)
        if len(dumped) <= limit:
            return json.loads(redact(dumped, self.cfg))
        return redact(truncate(dumped, limit), self.cfg)

    def message_text(self, cur):
        text, full = cur.get("text") or "", cur.get("text_len") or 0
        if full > len(text):
            text += "... [truncated %d chars]" % (full - len(text))
        return redact(text, self.cfg)

    # ---- event plumbing --------------------------------------------------
    def emit(self, event_type, body):
        key = (event_type, body["id"])
        if key not in self.events:
            self.order.append(key)
        self.events[key] = body

    def take_events(self):
        # The open message stays in ctx: its next lines may come in a later batch or run.
        self.emit_turn()
        out = [envelope(t, self.events[(t, i)]) for (t, i) in self.order]
        self.events, self.order = {}, []
        return out

    def pending_count(self):
        return len(self.order)

    # ---- turns -----------------------------------------------------------
    def base_metadata(self, entry):
        md = {"plugin_version": PLUGIN_VERSION}
        for src, dst in (("cwd", "cwd"), ("gitBranch", "git_branch"), ("version", "claude_code_version"), ("entrypoint", "entrypoint"), ("permissionMode", "permission_mode")):
            v = entry.get(src)
            if isinstance(v, str) and v:
                md[dst] = v
        return md

    def start_turn(self, entry, origin, text):
        self.close_message()
        self.emit_turn()
        uuid = entry.get("uuid") or det_id(self.file_key, entry.get("timestamp"))
        md = self.base_metadata(entry)
        md["origin"] = origin
        cmd = command_name(text) if origin == "command" else None
        if cmd:
            md["command"] = cmd
        if isinstance(entry.get("promptId"), str):
            md["prompt_id"] = entry["promptId"]
        md["prompt_chars"] = len(text)
        self.ctx["turn"] = {
            "trace_id": det_id(self.session_id, "trace", uuid),
            "timestamp": entry.get("timestamp") or now_iso(),
            "metadata": md,
            "input": self.content(text),
            "output": None,
            "eligible": origin == "human",  # other turns appear only once they have activity
            "has_obs": False,
            "orphan": False,
        }
        self.dirty_turn = True

    def current_turn(self, entry):
        """The turn that new observations belong to; creates a fallback one if needed."""
        if self.link is not None:
            if self.link.get("orphan"):
                self.dirty_turn = True  # the subagent's own trace (emit_turn)
            return {"trace_id": self.link["trace_id"], "subagent": True}
        turn = self.ctx.get("turn")
        if not turn:
            uuid = entry.get("uuid") or det_id(self.file_key, entry.get("timestamp"))
            md = self.base_metadata(entry)
            md["origin"] = "unknown"
            turn = {
                "trace_id": det_id(self.session_id, "trace", uuid),
                "timestamp": entry.get("timestamp") or now_iso(),
                "metadata": md,
                "input": None,
                "output": None,
                "eligible": False,
                "has_obs": False,
                "orphan": False,
            }
            self.ctx["turn"] = turn
        if not turn.get("has_obs"):
            turn["has_obs"] = True
        self.dirty_turn = True
        return turn

    def emit_turn(self):
        if self.link is not None and not self.link.get("orphan"):
            return  # subagent activity lives in the parent's trace
        turn = self.ctx.get("turn")
        if not turn or not self.dirty_turn:
            return
        self.dirty_turn = False
        if not (turn.get("eligible") or turn.get("has_obs")):
            return
        body = {
            "id": turn["trace_id"],
            "timestamp": turn["timestamp"],
            "name": TRACE_NAME,
            "userId": self.sess.get("user_id"),
            "sessionId": self.session_id,
            "tags": list(TRACE_TAGS),
            "metadata": turn["metadata"],
        }
        if turn.get("input") is not None:
            body["input"] = turn["input"]
        if turn.get("output") is not None:
            body["output"] = turn["output"]
        self.emit("trace-create", body)

    # ---- model responses -------------------------------------------------
    def observation_meta(self, entry):
        md = {}
        if self.link is not None or entry.get("isSidechain"):
            md["sidechain"] = True
            agent_id = entry.get("agentId") or (self.link or {}).get("agent_id")
            if agent_id:
                md["agent_id"] = agent_id
            if self.link and self.link.get("agent_type"):
                md["agent_type"] = self.link["agent_type"]
        return md

    def parent_observation(self, entry):
        if self.link is not None:
            return self.link.get("span_id")
        if entry.get("isSidechain"):  # older transcripts wrote subagent lines inline
            return self.ctx.get("open_agent_span")
        return None

    def on_assistant(self, entry):
        msg = entry.get("message") or {}
        model = msg.get("model")
        if model == "<synthetic>" or not isinstance(model, str):
            return  # local notices (API errors etc.), not a model call
        mid = msg.get("id") or entry.get("requestId") or entry.get("uuid")
        ts = entry.get("timestamp") or now_iso()
        cur = self.ctx.get("msg")
        if not cur or cur.get("id") != mid:
            self.close_message()
            turn = self.current_turn(entry)
            trigger = self.ctx.get("trigger") or {}
            start = trigger.get("ts")
            st, ft = parse_ts(start), parse_ts(ts)
            if st is None or ft is None or st > ft:
                start = ts
            md = self.observation_meta(entry)
            md.update({"message_id": mid})
            if isinstance(entry.get("requestId"), str):
                md["request_id"] = entry["requestId"]
            cur = {
                "id": mid,
                "gen_id": det_id(self.session_id, "gen", mid),
                "trace_id": turn["trace_id"],
                "parent": self.parent_observation(entry),
                "model": model,
                "start": start,
                "first": ts,
                "last": ts,
                "usage": None,
                "speed": None,
                "metadata": md,
                "input": trigger.get("input"),
                "text": "",
                "blocks": {},
                "tools": [],
                "error": None,
            }
            self.ctx["msg"] = cur
        cur["last"] = ts
        usage = msg.get("usage")
        if isinstance(usage, dict):
            cur["usage"] = build_usage_details(usage)  # identical on every line of a message: replace, never add
            if isinstance(usage.get("speed"), str):
                cur["speed"] = usage["speed"]
            for k in ("service_tier", "inference_geo"):
                if isinstance(usage.get(k), str):
                    cur["metadata"][k] = usage[k]
        if msg.get("stop_reason"):
            cur["metadata"]["stop_reason"] = msg["stop_reason"]
        if entry.get("isApiErrorMessage") or entry.get("error"):
            cur["error"] = str(entry.get("error") or "api error")[:200]
        for b in msg.get("content") or []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type") or "unknown"
            cur["blocks"][bt] = cur["blocks"].get(bt, 0) + 1
            if bt == "text" and isinstance(b.get("text"), str) and self.cfg.log_content:
                # keep at most the first max_content_chars, but count the full length
                cur["text_len"] = cur.get("text_len", 0) + (1 if cur.get("text_len") else 0) + len(b["text"])
                joined = (cur["text"] + "\n" + b["text"]) if cur["text"] else b["text"]
                cur["text"] = joined[: self.cfg.max_content_chars]
                turn = self.ctx.get("turn")
                if self.link is None and turn and turn.get("trace_id") == cur["trace_id"]:
                    turn["output"] = self.message_text(cur)  # the trace output is the turn's last text
                    self.dirty_turn = True
            elif bt == "tool_use":
                self.on_tool_use(entry, cur, b, ts)
        self.emit_generation(cur)

    def emit_generation(self, cur):
        md = dict(cur["metadata"])
        md["content_blocks"] = dict(cur["blocks"])
        if cur["tools"]:
            md["tool_calls"] = list(cur["tools"])
        body = {
            "id": cur["gen_id"],
            "traceId": cur["trace_id"],
            "name": GENERATION_NAME,
            "model": cur["model"],
            "startTime": cur["start"],
            "completionStartTime": cur["first"],
            "endTime": cur["last"],
            "usageDetails": cur["usage"] or {},
            "metadata": md,
        }
        if cur.get("speed"):
            body["modelParameters"] = {"speed": cur["speed"]}
        if cur.get("parent"):
            body["parentObservationId"] = cur["parent"]
        if cur.get("error"):
            body["level"] = "ERROR"
            body["statusMessage"] = cur["error"]
        if cur.get("input") is not None:
            body["input"] = cur["input"]
        if self.cfg.log_content and cur.get("text"):
            body["output"] = self.message_text(cur)
        self.emit("generation-create", body)

    def close_message(self):
        self.ctx.pop("msg", None)

    # ---- tools -----------------------------------------------------------
    def on_tool_use(self, entry, cur, block, ts):
        tool_id = block.get("id")
        name = block.get("name") or "tool"
        if not isinstance(tool_id, str):
            return
        cur["tools"].append(name)
        span_id = det_id(self.session_id, "tool", tool_id)
        md = self.observation_meta(entry)
        md["tool_use_id"] = tool_id
        md["input_chars"] = text_size(block.get("input"))
        if name in AGENT_TOOL_NAMES and isinstance(block.get("input"), dict):
            sub_type = block["input"].get("subagent_type")
            if isinstance(sub_type, str):
                md["subagent_type"] = sub_type
        pending = {
            "span_id": span_id,
            "trace_id": cur["trace_id"],
            "parent": cur["gen_id"],
            "name": name,
            "start": ts,
            "metadata": md,
            "input": self.content(block.get("input")),
        }
        self.sess.setdefault("tools", {})[tool_id] = pending
        if name in AGENT_TOOL_NAMES:
            self.sess.setdefault("agents", {})[tool_id] = {"trace_id": cur["trace_id"], "span_id": span_id}
            if self.link is None:
                self.ctx["open_agent_span"] = span_id
        self.emit_span(pending, None)

    def emit_span(self, pending, result):
        body = {
            "id": pending["span_id"],
            "traceId": pending["trace_id"],
            "parentObservationId": pending["parent"],
            "name": pending["name"],
            "startTime": pending["start"],
            "metadata": dict(pending["metadata"]),
        }
        if pending.get("input") is not None:
            body["input"] = pending["input"]
        if result is not None:
            body["endTime"] = result["ts"]
            body["metadata"]["output_chars"] = result["chars"]
            body["metadata"]["is_error"] = result["is_error"]
            if result.get("agent_id"):
                body["metadata"]["agent_id"] = result["agent_id"]
            if result["is_error"]:
                body["level"] = "ERROR"
                body["statusMessage"] = "tool returned an error"
            if result.get("output") is not None:
                body["output"] = result["output"]
        self.emit("span-create", body)

    def on_user(self, entry):
        msg = entry.get("message") or {}
        content = msg.get("content")
        ts = entry.get("timestamp") or now_iso()
        results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"] if isinstance(content, list) else []
        if results:
            self.close_message()
            inputs = []
            for b in results:
                tool_id = b.get("tool_use_id")
                out = b.get("content")
                is_error = b.get("is_error") is True
                pending = self.sess.get("tools", {}).pop(tool_id, None) if isinstance(tool_id, str) else None
                if pending is None:
                    if not isinstance(tool_id, str):
                        continue
                    turn = self.current_turn(entry)
                    md = self.observation_meta(entry)
                    md.update({"tool_use_id": tool_id, "note": "tool call not found in transcript"})
                    pending = {"span_id": det_id(self.session_id, "tool", tool_id), "trace_id": turn["trace_id"], "parent": self.parent_observation(entry), "name": "tool", "start": ts, "metadata": md, "input": None}
                tur = entry.get("toolUseResult")
                agent_id = tur.get("agentId") if isinstance(tur, dict) and pending["name"] in AGENT_TOOL_NAMES else None
                self.emit_span(pending, {"ts": ts, "is_error": is_error, "chars": text_size(out), "output": self.content(blocks_text(out)), "agent_id": agent_id})
                if pending["span_id"] == self.ctx.get("open_agent_span") and not (isinstance(tur, dict) and tur.get("isAsync")):
                    self.ctx.pop("open_agent_span", None)
                if self.cfg.log_content:
                    inputs.append({"tool": pending["name"], "result": blocks_text(out)})
            self.ctx["trigger"] = {"ts": ts, "input": self.content(inputs) if inputs else None}
            return
        if self.link is None and not entry.get("isSidechain") and is_turn_start(entry):
            text = blocks_text(content)
            self.start_turn(entry, prompt_origin(entry, text), text)
            self.ctx["trigger"] = {"ts": ts, "input": self.content(text)}
            return
        # Subagent prompt, meta lines, interruptions: they still trigger the next request.
        self.close_message()
        text = blocks_text(content)
        self.ctx["trigger"] = {"ts": ts, "input": self.content(text) if text else None}

    def consume(self, entry):
        if not isinstance(entry, dict):
            return
        t = entry.get("type")
        if self.link is None and not self.sess.get("cwd") and isinstance(entry.get("cwd"), str):
            self.sess["cwd"] = entry["cwd"]
        if t == "assistant":
            self.on_assistant(entry)
        elif t == "user":
            self.on_user(entry)
        # every other entry type is bookkeeping and is ignored


# ---------------------------------------------------------------------------
# Processing a session
# ---------------------------------------------------------------------------

def resolve_user(cfg, cwd):
    return core.resolve_user(cfg, cwd, IS_WINDOWS)


def complete_lines(path, offset):
    """Yields (raw_line, end_offset) for every complete line after offset."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    pos = 0
    while True:
        nl = data.find(b"\n", pos)
        if nl < 0:
            return  # a partly written last line waits for the next run
        yield data[pos:nl], offset + nl + 1
        pos = nl + 1


def process_file(cfg, sender, session_id, st, path, file_key, link=None):
    """Process one transcript file in batches. Returns False when a send failed."""
    sess = st["sessions"][session_id]
    fstate = sess["files"].setdefault(file_key, {"offset": 0, "ctx": {}})
    try:
        size = os.path.getsize(path)
    except OSError:
        return True
    if size < fstate.get("offset", 0):  # file was replaced: start over (ids make it safe)
        log_error("%s is shorter than the saved offset; reading it again" % os.path.basename(path))
        fstate["offset"], fstate["ctx"] = 0, {}
    if size == fstate.get("offset", 0):
        return True

    work = copy.deepcopy(sess)
    mapper = Mapper(cfg, session_id, work, file_key, link)
    committed_offset = fstate["offset"]
    last_end = committed_offset

    def commit(end):
        work["files"][file_key]["offset"] = end
        work["updated"] = time.time()
        st["sessions"][session_id] = copy.deepcopy(work)

    for raw, end in complete_lines(path, fstate["offset"]):
        last_end = end
        line = raw.strip()
        if line:
            try:
                entry = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                log_error("skipped a malformed line in %s at byte %d: %s" % (os.path.basename(path), end - len(raw) - 1, exc))
                entry = None
            if entry is not None:
                try:
                    mapper.consume(entry)
                except Exception as exc:
                    log_error("could not map a line in %s at byte %d: %s: %s" % (os.path.basename(path), end - len(raw) - 1, type(exc).__name__, exc))
        if mapper.pending_count() >= MAX_BATCH_EVENTS:
            if not sender.send(mapper.take_events()):
                return False
            commit(end)
    events = mapper.take_events()
    if events and not sender.send(events):
        return False
    commit(last_end)
    return True


def subagent_files(transcript_path):
    base = transcript_path[:-6] if transcript_path.endswith(".jsonl") else transcript_path
    folder = os.path.join(base, "subagents")
    if not os.path.isdir(folder):
        return []
    out = []
    for name in os.listdir(folder):
        if not (name.startswith("agent-") and name.endswith(".jsonl")):
            continue
        agent_id = name[len("agent-"):-len(".jsonl")]
        meta = {}
        try:
            with open(os.path.join(folder, "agent-%s.meta.json" % agent_id), "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            pass
        depth = meta.get("spawnDepth") if isinstance(meta.get("spawnDepth"), int) else 1
        out.append((depth, name, agent_id, os.path.join(folder, name), meta))
    out.sort()
    return out


def process_session(cfg, payload, st, sender):
    session_id = payload.get("session_id")
    transcript = payload.get("transcript_path")
    event = payload.get("hook_event_name") or ""
    if not isinstance(session_id, str) or not session_id or not isinstance(transcript, str) or not os.path.isfile(transcript):
        debug("no session or transcript in hook input; nothing to do")
        return True
    sess = st["sessions"].setdefault(session_id, {"files": {}, "tools": {}, "agents": {}, "updated": time.time()})
    sess.setdefault("files", {})
    sess.setdefault("tools", {})
    sess.setdefault("agents", {})
    sess["transcript_path"] = transcript
    if not sess.get("user_id"):
        sess["user_id"] = resolve_user(cfg, payload.get("cwd"))

    if not process_file(cfg, sender, session_id, st, transcript, "main"):
        return False

    # Subagents: their Agent call may sit in another subagent's file, so loop until no progress.
    todo = subagent_files(transcript)
    for _ in range(4):
        waiting = []
        for item in todo:
            _depth, _name, agent_id, path, meta = item
            sess = st["sessions"][session_id]
            tool_use_id = meta.get("toolUseId")
            parent = sess["agents"].get(tool_use_id) if isinstance(tool_use_id, str) else None
            key = "agent-" + agent_id
            known = sess["files"].get(key, {}).get("link")
            if known:
                link = known
            elif parent:
                link = {"trace_id": parent["trace_id"], "span_id": parent["span_id"], "agent_id": agent_id, "agent_type": meta.get("agentType")}
            else:
                waiting.append(item)
                continue
            sess["files"].setdefault(key, {"offset": 0, "ctx": {}})["link"] = link
            if not process_file(cfg, sender, session_id, st, path, key, link):
                return False
        if len(waiting) == len(todo):
            break
        todo = waiting
    if todo and event == "SessionEnd":
        for _depth, _name, agent_id, path, meta in todo:
            sess = st["sessions"][session_id]
            key = "agent-" + agent_id
            trace_id = det_id(session_id, "trace", "agent", agent_id)
            link = {"trace_id": trace_id, "span_id": None, "agent_id": agent_id, "agent_type": meta.get("agentType"), "orphan": True}
            fs = sess["files"].setdefault(key, {"offset": 0, "ctx": {}})
            fs["link"] = link
            fs["ctx"]["turn"] = {"trace_id": trace_id, "timestamp": now_iso(), "metadata": {"plugin_version": PLUGIN_VERSION, "origin": "subagent", "agent_id": agent_id, "sidechain": True}, "input": None, "output": None, "eligible": True, "has_obs": True, "orphan": True}
            if not process_file(cfg, sender, session_id, st, path, key, link):
                return False
    elif todo:
        debug("%d subagent file(s) wait for their parent Agent call" % len(todo))
    if event == "SessionEnd":
        st["sessions"][session_id]["ended"] = time.time()
    return True


def run_worker(payload, cfg):
    wait = cfg.timeout + LOCK_WAIT_EXTRA_SECONDS
    with file_lock(os.path.join(data_dir(), "state.lock"), wait=wait) as acquired:
        if not acquired:
            log_error("another run held the lock for %.0fs; this run is skipped (its data goes next time)" % wait)
            return
        st = load_state()
        cleanup_state(st)
        # A bad AGENTGUARD_TIMEOUT_SECONDS is logged once per distinct value, not on every run.
        if cfg.timeout_warning and st.get("timeout_warning") != cfg.timeout_warning:
            log_error(cfg.timeout_warning)
        st["timeout_warning"] = cfg.timeout_warning
        sender = Sender(cfg)
        ok = True
        try:
            ok = process_session(cfg, payload, st, sender)
        finally:
            if sender.last is not None:
                st["last_send"] = dict(sender.last, session_id=payload.get("session_id"), hook=payload.get("hook_event_name"))
            save_state(st)
        debug("done: %s, %d event(s) sent" % ("ok" if ok else "will retry", sender.sent_events))


# ---------------------------------------------------------------------------
# Hook entry: detach and return
# ---------------------------------------------------------------------------

def spawn_worker(raw_payload):
    env = dict(os.environ)
    env["AGENTGUARD_HOOK_PAYLOAD"] = raw_payload
    core.spawn_detached([sys.executable, os.path.abspath(__file__), "--worker"], env, IS_WINDOWS)


def hook_main():
    global _CFG
    cfg = Config()
    _CFG = cfg
    if not cfg.has_keys:
        return  # not set up: stay silent
    # Claude Code writes UTF-8. Decode it ourselves: on Windows, text-mode stdin would use the
    # ANSI code page and garble non-ASCII paths.
    stream = getattr(sys.stdin, "buffer", None)
    raw = stream.read().decode("utf-8-sig", "replace") if stream is not None else sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        log_error("hook input was not JSON")
        return
    if not isinstance(payload, dict):
        return
    payload = {k: payload[k] for k in WORKER_PAYLOAD_KEYS if k in payload}
    if cfg.debug or env_true("AGENTGUARD_SYNC"):
        run_worker(payload, cfg)
        return
    try:
        spawn_worker(json.dumps(payload))
    except OSError as exc:
        log_error("could not start the background worker: %s: %s" % (type(exc).__name__, exc))


def worker_main():
    global _CFG
    cfg = Config()
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
# Status, setup and OTel check (run by the slash commands; these do print)
# ---------------------------------------------------------------------------

def read_settings():
    path = settings_path()
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("settings.json is not a JSON object")
    return data


def otel_vars(settings_env, process_env):
    in_settings = sorted(k for k in settings_env if OTEL_VAR_PATTERN.match(k))
    in_process = sorted(k for k in process_env if OTEL_VAR_PATTERN.match(k) and k not in settings_env)
    return in_settings, in_process


# Every setting the plugin reads. --status shows them from the environment first, and from the
# env block of settings.json when the environment lacks them (as in a plain terminal).
SETTING_NAMES = (
    "AGENTGUARD_HOST", "AGENTGUARD_PUBLIC_KEY", "AGENTGUARD_SECRET_KEY", "AGENTGUARD_LOG_CONTENT",
    "AGENTGUARD_MAX_CONTENT_CHARS", "AGENTGUARD_USER", "AGENTGUARD_DEBUG", "AGENTGUARD_TIMEOUT_SECONDS",
)
SOURCE_NOTES = {
    "environment": "from the environment",
    "settings.json": "from settings.json, used by new Claude Code sessions",
}


def setting_text(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""


def status_config(settings_env, environ=None):
    """Config as --status shows it, and where each setting came from. The environment wins;
    a setting missing there is read from settings.json's env block (read only), because Claude
    Code passes that block only to its own sessions, not to a terminal."""
    environ = os.environ if environ is None else environ
    merged, sources = {}, {}
    for name in SETTING_NAMES:
        if setting_text(environ.get(name)):
            merged[name], sources[name] = setting_text(environ.get(name)), "environment"
        elif setting_text(settings_env.get(name)):
            merged[name], sources[name] = setting_text(settings_env.get(name)), "settings.json"
    return Config(merged), sources


def status_main():
    try:
        settings_env = read_settings().get("env") or {}
        if not isinstance(settings_env, dict):
            raise ValueError('"env" is not a JSON object')
        settings_error = None
    except Exception as exc:
        settings_env, settings_error = {}, exc
    cfg, sources = status_config(settings_env)

    def source(name, default=""):
        return " (%s)" % SOURCE_NOTES[sources[name]] if name in sources else default

    print("AgentGuard plugin %s" % PLUGIN_VERSION)
    print("Python: %s (%s)" % (sys.executable, sys.version.split()[0]))
    if settings_error is not None:
        print("WARNING: could not read %s: %s" % (settings_path(), settings_error))
    print("Host: %s%s" % (cfg.host, source("AGENTGUARD_HOST", " (default)")))
    print("Public key: %s%s" % (mask(cfg.public_key), source("AGENTGUARD_PUBLIC_KEY")))
    print("Secret key: %s%s" % (mask_secret(cfg.secret_key), source("AGENTGUARD_SECRET_KEY")))
    key_names = ("AGENTGUARD_PUBLIC_KEY", "AGENTGUARD_SECRET_KEY")
    if not cfg.has_keys:
        print("Keys are not set: run /agentguard:setup. Until then nothing is sent.")
    elif os.environ.get("CLAUDECODE") == "1" and any(sources.get(n) == "settings.json" for n in key_names):
        print("These keys are in settings.json but not in this Claude Code session: restart Claude Code to use them.")
    for name in key_names:  # environment wins; say so when settings.json would give new sessions another key
        other = setting_text(settings_env.get(name))
        if sources.get(name) == "environment" and other and other != setting_text(os.environ.get(name)):
            print("Note: settings.json has a different %s; new Claude Code sessions will use that one." % ("public key" if name.endswith("PUBLIC_KEY") else "secret key"))
    print("Content logging: %s (max %d chars per field)" % ("ON" if cfg.log_content else "off", cfg.max_content_chars))
    print("Timeout: %gs for all requests in one send (AGENTGUARD_TIMEOUT_SECONDS)%s" % (cfg.timeout, "; WARNING: " + cfg.timeout_warning if cfg.timeout_warning else ""))
    print("User id: %s" % (cfg.user or "from git config user.email, else OS user"))
    st = load_state()
    last = st.get("last_send")
    if last:
        print("Last send: %s, %s%s: %s" % (last.get("time"), "OK" if last.get("ok") else "FAILED", " (HTTP %s)" % last["status"] if last.get("status") else "", last.get("detail")))
    else:
        print("Last send: never")
    print("Sessions tracked (last %d days): %d" % (STATE_RETENTION_DAYS, len(st.get("sessions") or {})))
    s_otel, p_otel = otel_vars(settings_env, os.environ)
    if s_otel or p_otel:
        print("WARNING: old OpenTelemetry settings found (they cause duplicate traces): %s" % ", ".join(s_otel + p_otel))
    print_collector_advice()
    print("Error log: %s" % error_log_path())
    try:
        with open(error_log_path(), "r", encoding="utf-8") as fh:
            tail = fh.readlines()[-10:]
        print("Last %d error log line(s):" % len(tail))
        for line in tail:
            print("  " + redact(line.rstrip("\n"), cfg))
    except OSError:
        print("Error log: empty")
    return 0


def docker_containers_on_port(port):
    return core.docker_containers_on_port(port, IS_WINDOWS)


def quote_path(path):
    return core.quote_path(path, IS_WINDOWS)


def collector_advice(port=None):
    """Lines telling the user how to stop a still-running old collector; [] if nothing
    listens on its port. Only prints instructions: it never stops anything itself."""
    return core.collector_advice(OTEL_COLLECTOR_PORT if port is None else port, IS_WINDOWS)


def print_collector_advice():
    try:
        for line in collector_advice():
            print(line)
    except Exception:
        pass  # never let this check break status or setup


def check_otel_main():
    try:
        settings_env = read_settings().get("env") or {}
    except Exception as exc:
        print("Could not read %s: %s" % (settings_path(), exc))
        return 4
    s_otel, p_otel = otel_vars(settings_env, os.environ)
    print(json.dumps({"settings_file": settings_path(), "in_settings_env": s_otel, "elsewhere_in_environment": p_otel}))
    return 0


def test_keys(cfg):
    """Returns (ok, message)."""
    return core.test_keys(cfg, http_request)


def write_settings(updates, remove_keys):
    """Merge into settings.json env without touching anything else. Returns the backup path."""
    path = settings_path()
    data = read_settings()  # raises on invalid JSON, so a broken file is never overwritten
    backup = None
    if os.path.exists(path):
        backup = path + ".agentguard-backup"
        with open(path, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
        try:
            os.chmod(backup, 0o600)
        except OSError:
            pass
    env = data.get("env")
    if not isinstance(env, dict):
        env = {}
    for k in remove_keys:
        env.pop(k, None)
    env.update(updates)
    data["env"] = env
    ensure_dir(os.path.dirname(path))
    tmp = path + ".agentguard-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    try:
        replace_file(tmp, path)
    except PermissionError:
        # Windows: something keeps settings.json open without delete sharing. Overwriting
        # in place still works there; the backup above guards against a half-written file.
        with open(path, "w", encoding="utf-8") as fh, open(tmp, "r", encoding="utf-8") as src:
            fh.write(src.read())
        os.remove(tmp)
    return backup


def read_secret_line():
    """First line of stdin as the secret. Read as bytes and decoded as UTF-8 with any BOM
    removed: Windows PowerShell 5.1 puts a BOM in front of text it pipes to a program, and
    text-mode stdin on Windows would decode with the ANSI code page."""
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        line = sys.stdin.readline()
    else:
        line = stream.readline().decode("utf-8-sig", "replace")
    return line.lstrip("\ufeff").strip()


def setup_main(args):
    interactive = sys.stdin.isatty() and not args.secret_stdin
    host = args.host
    public_key = args.public_key
    if interactive:
        if not host:
            host = input("AgentGuard host [%s]: " % DEFAULT_HOST).strip() or DEFAULT_HOST
        if not public_key:
            public_key = input("Public key (pk-ag-...): ").strip()
        secret = getpass.getpass("Secret key (sk-ag-..., hidden): ").strip()
    else:
        secret = read_secret_line() if args.secret_stdin else (os.environ.get("AGENTGUARD_SECRET_KEY") or "").strip()
    host = (host or DEFAULT_HOST).strip().rstrip("/")
    if not re.match(r"^https?://[^\s/]+", host):
        print("ERROR: host must start with https:// (got %r)" % host)
        return 2
    if not public_key or not secret:
        print("ERROR: public key and secret key are both required.")
        return 2
    cfg = Config({"AGENTGUARD_HOST": host, "AGENTGUARD_PUBLIC_KEY": public_key, "AGENTGUARD_SECRET_KEY": secret})
    global _CFG
    _CFG = cfg
    ok, message = test_keys(cfg)
    print(("OK: " if ok else "ERROR: ") + message)
    if not ok:
        print("Nothing was written to %s." % settings_path())
        return 3
    try:
        settings_env = read_settings().get("env") or {}
    except Exception as exc:
        print("ERROR: could not read %s (%s). Fix the file and run setup again; nothing was written." % (settings_path(), exc))
        return 4
    s_otel, p_otel = otel_vars(settings_env, os.environ)
    remove = s_otel if args.remove_otel else []
    updates = {"AGENTGUARD_HOST": host, "AGENTGUARD_PUBLIC_KEY": public_key, "AGENTGUARD_SECRET_KEY": secret}
    if interactive and s_otel and not args.remove_otel:
        answer = input("Remove old OpenTelemetry settings %s from settings.json? They cause duplicate traces. [Y/n]: " % ", ".join(s_otel)).strip().lower()
        if answer in ("", "y", "yes"):
            remove = s_otel
    try:
        backup = write_settings(updates, remove)
    except Exception as exc:
        print("ERROR: could not write %s: %s" % (settings_path(), exc))
        return 4
    print("Saved AGENTGUARD_HOST, AGENTGUARD_PUBLIC_KEY and AGENTGUARD_SECRET_KEY to the env block of %s." % settings_path())
    if backup:
        print("The previous file is kept at %s." % backup)
    if remove:
        print("Removed old OpenTelemetry settings: %s" % ", ".join(remove))
    elif s_otel:
        print("WARNING: old OpenTelemetry settings are still in settings.json and will cause duplicate traces: %s" % ", ".join(s_otel))
    if p_otel:
        print("WARNING: these OpenTelemetry variables come from your shell or another settings file; remove them there: %s" % ", ".join(p_otel))
    print_collector_advice()
    print("Restart Claude Code so the hooks see the new keys.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="AgentGuard plugin for Claude Code")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--check-otel", action="store_true")
    parser.add_argument("--host")
    parser.add_argument("--public-key")
    parser.add_argument("--secret-stdin", action="store_true", help="read the secret key from the first line of stdin")
    parser.add_argument("--remove-otel", action="store_true")
    argv = sys.argv[1:] if argv is None else argv

    if not argv:  # hook mode: must never fail or print
        try:
            hook_main()
        except BaseException as exc:  # noqa: B036 - nothing may escape a hook
            log_error("hook: %s: %s" % (type(exc).__name__, exc))
        return 0
    if argv == ["--worker"]:
        try:
            worker_main()
        except BaseException as exc:
            log_error("worker: %s: %s" % (type(exc).__name__, exc))
        return 0

    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # e.g. a non-ASCII path on a Windows code page
        except (AttributeError, ValueError):
            pass
    if args.status:
        return status_main()
    if args.check_otel:
        return check_otel_main()
    if args.setup:
        return setup_main(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
