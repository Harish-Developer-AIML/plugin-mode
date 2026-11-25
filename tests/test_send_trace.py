"""Tests for plugins/agentguard/scripts/send_trace.py (standard library unittest).

    python3 -m unittest discover -s tests -v
"""

import base64
import gc
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN = os.path.join(ROOT, "plugins", "agentguard")
SCRIPT = os.path.join(PLUGIN, "scripts", "send_trace.py")
FIXTURES = os.path.join(HERE, "fixtures")
SESSION = "11111111-2222-3333-4444-555555555555"

sys.path.insert(0, HERE)
from fake_server import FakeAgentGuard  # noqa: E402

# A ResourceWarning raised while an object is garbage collected never fails a test on its own,
# even with -W error: Python reports it through sys.unraisablehook and carries on. Collect those
# here and fail the run in tearDownModule, so "-W error::ResourceWarning" really means no leaks.
_UNRAISABLE = []
_previous_hook = sys.unraisablehook


def _record_unraisable(info):
    if isinstance(info.exc_value, ResourceWarning):
        _UNRAISABLE.append("%s: %r" % (info.exc_value, info.object))
    _previous_hook(info)


def setUpModule():
    sys.unraisablehook = _record_unraisable


def tearDownModule():
    gc.collect()
    sys.unraisablehook = _previous_hook
    if _UNRAISABLE:
        raise AssertionError("unclosed resources: " + "; ".join(_UNRAISABLE))


def load_module():
    spec = importlib.util.spec_from_file_location("send_trace_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Base(unittest.TestCase):
    """Each test gets a temp dir with a copy of the fixtures, a fresh state dir and a fake server."""

    server_status = 207
    server_delay = 0.0

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agentguard-test-")
        self.project = os.path.join(self.tmp, "projects")
        shutil.copytree(FIXTURES, self.project, ignore=shutil.ignore_patterns("*.py", "__pycache__"))
        self.transcript = os.path.join(self.project, SESSION + ".jsonl")
        self.home = os.path.join(self.tmp, "agentguard")
        self.claude_dir = os.path.join(self.tmp, "claude")
        os.makedirs(self.claude_dir)
        self.server = FakeAgentGuard(status=self.server_status, delay=self.server_delay).start()
        self.mod = load_module()

    def tearDown(self):
        self.server.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENTGUARD_", "OTEL_")) and k not in ("CLAUDE_CODE_ENABLE_TELEMETRY", "CLAUDE_CONFIG_DIR")}
        env.update({
            "AGENTGUARD_HOST": self.server.url,
            "AGENTGUARD_PUBLIC_KEY": self.server.public_key,
            "AGENTGUARD_SECRET_KEY": self.server.secret_key,
            "AGENTGUARD_HOME": self.home,
            "AGENTGUARD_USER": "dev@example.com",
            "AGENTGUARD_SYNC": "1",
            "CLAUDE_CONFIG_DIR": self.claude_dir,
        })
        env.update({k: v for k, v in extra.items() if v is not None})
        for k, v in extra.items():
            if v is None:
                env.pop(k, None)
        return env

    def payload(self, event="Stop", transcript=None, **extra):
        data = {"session_id": SESSION, "transcript_path": transcript or self.transcript, "cwd": self.project, "hook_event_name": event, "stop_hook_active": False}
        data.update(extra)
        return json.dumps(data)

    def wait_for_offset(self, key, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline and self.offset(key) == 0:
            time.sleep(0.2)
        return self.offset(key)

    def run_hook(self, event="Stop", stdin=None, timeout=30, **env):
        start = time.monotonic()
        proc = subprocess.run([sys.executable, SCRIPT], input=(self.payload(event) if stdin is None else stdin).encode(), env=self.env(**env), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return proc, time.monotonic() - start

    def assertQuietSuccess(self, proc):
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")
        self.assertEqual(proc.stderr, b"")

    def state(self):
        with open(os.path.join(self.home, "state.json")) as fh:
            return json.load(fh)

    def offset(self, key="main"):
        try:
            return self.state()["sessions"][SESSION]["files"][key]["offset"]
        except (OSError, KeyError):
            return 0

    def bodies(self, event_type=None):
        """Latest body per (type, id), like the server's upsert."""
        out = {}
        for ev in self.server.events():
            if event_type is None or ev["type"] == event_type:
                out[(ev["type"], ev["body"]["id"])] = ev["body"]
        return out

    def by_type(self, event_type):
        return {i: b for (t, i), b in self.bodies(event_type).items()}

    def error_log(self):
        try:
            with open(os.path.join(self.home, "error.log")) as fh:
                return fh.read()
        except OSError:
            return ""


class MappingTests(Base):
    def setUp(self):
        super().setUp()
        proc, _ = self.run_hook()
        self.assertQuietSuccess(proc)
        self.traces = self.by_type("trace-create")
        self.gens = self.by_type("generation-create")
        self.spans = self.by_type("span-create")
        self.det = lambda *p: self.mod.det_id(SESSION, *p)

    def test_counts(self):
        # prompts 1 and 2 plus the task notification; /clear made no model call so it has no trace
        self.assertEqual(len(self.traces), 3)
        # msg A, B, C, D, E in the main file and S1, S2 in the subagent; the <synthetic> one is skipped
        self.assertEqual(len(self.gens), 7)
        self.assertEqual(len(self.spans), 4)  # Bash, Agent, Read, Grep

    def test_trace_fields(self):
        t = self.traces[self.det("trace", "u-1")]
        self.assertEqual(t["name"], "claude_code.turn")
        self.assertEqual(t["sessionId"], SESSION)
        self.assertEqual(t["userId"], "dev@example.com")
        self.assertEqual(t["tags"], ["claude-code"])
        self.assertEqual(t["timestamp"], "2026-11-20T10:00:00.100Z")
        md = t["metadata"]
        self.assertEqual(md["cwd"], "/home/dev/example-project")
        self.assertEqual(md["git_branch"], "feature/demo")
        self.assertEqual(md["plugin_version"], self.mod.PLUGIN_VERSION)
        self.assertEqual(md["claude_code_version"], "2.1.330")
        self.assertEqual(md["origin"], "human")
        notif = self.traces[self.det("trace", "u-3")]
        self.assertEqual(notif["metadata"]["origin"], "task_notification")

    def test_generation_fields_and_timing(self):
        g = self.gens[self.det("gen", "msg_A")]
        self.assertEqual(g["name"], "claude_code.llm_call")
        self.assertEqual(g["traceId"], self.det("trace", "u-1"))
        self.assertEqual(g["model"], "claude-opus-5-5")
        self.assertEqual(g["startTime"], "2026-11-20T10:00:00.100Z")  # the prompt that triggered it
        self.assertEqual(g["completionStartTime"], "2026-11-20T10:00:02.000Z")
        self.assertEqual(g["endTime"], "2026-11-20T10:00:03.000Z")  # last line of the message
        self.assertEqual(g["modelParameters"], {"speed": "standard"})
        self.assertEqual(g["metadata"]["content_blocks"], {"thinking": 1, "text": 1, "tool_use": 1})
        self.assertEqual(g["metadata"]["tool_calls"], ["Bash"])
        self.assertEqual(g["metadata"]["stop_reason"], "tool_use")
        g2 = self.gens[self.det("gen", "msg_C")]
        self.assertEqual(g2["startTime"], "2026-11-20T10:00:09.000Z")  # the tool result it answered

    def test_usage_counted_once_per_message_id(self):
        # msg_A is written as three lines that each repeat the same usage
        g = self.gens[self.det("gen", "msg_A")]
        self.assertEqual(g["usageDetails"], {"input": 3, "output": 120, "cache_read_input_tokens": 1000, "input_cache_creation_1h": 500})
        total_output = sum(x["usageDetails"].get("output", 0) for x in self.gens.values())
        self.assertEqual(total_output, 120 + 80 + 40 + 60 + 30 + 50 + 70)

    def test_usage_without_breakdown_falls_back(self):
        g = self.gens[self.det("gen", "msg_D")]
        self.assertEqual(g["usageDetails"], {"input": 5, "output": 60, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 900})
        e = self.gens[self.det("gen", "msg_E")]
        self.assertEqual(e["usageDetails"]["input_cache_creation_5m"], 300)
        self.assertNotIn("cache_creation_input_tokens", e["usageDetails"])

    def test_tool_spans(self):
        bash = self.spans[self.det("tool", "toolu_A1")]
        self.assertEqual(bash["name"], "Bash")
        self.assertEqual(bash["traceId"], self.det("trace", "u-1"))
        self.assertEqual(bash["parentObservationId"], self.det("gen", "msg_A"))
        self.assertEqual(bash["startTime"], "2026-11-20T10:00:03.000Z")
        self.assertEqual(bash["endTime"], "2026-11-20T10:00:04.000Z")
        self.assertNotIn("level", bash)
        read = self.spans[self.det("tool", "toolu_B2")]
        self.assertEqual(read["level"], "ERROR")
        self.assertTrue(read["metadata"]["is_error"])
        agent = self.spans[self.det("tool", "toolu_B1")]
        self.assertEqual(agent["metadata"]["agent_id"], "afixture01")
        self.assertEqual(agent["metadata"]["subagent_type"], "Explore")

    def test_subagent_nested_under_agent_span(self):
        span_id = self.det("tool", "toolu_B1")
        for mid in ("msg_S1", "msg_S2"):
            g = self.gens[self.det("gen", mid)]
            self.assertEqual(g["traceId"], self.det("trace", "u-1"))
            self.assertEqual(g["parentObservationId"], span_id)
            self.assertEqual(g["model"], "claude-haiku-5-5")
            self.assertTrue(g["metadata"]["sidechain"])
            self.assertEqual(g["metadata"]["agent_id"], "afixture01")
            self.assertEqual(g["metadata"]["agent_type"], "Explore")
        grep = self.spans[self.det("tool", "toolu_S1")]
        self.assertEqual(grep["parentObservationId"], self.det("gen", "msg_S1"))
        self.assertTrue(grep["metadata"]["sidechain"])

    def test_no_content_by_default(self):
        for body in self.bodies().values():
            self.assertNotIn("input", body)
            self.assertNotIn("output", body)
        raw = b"".join(r["raw"] for r in self.server.requests)
        for marker in (b"PROMPT-ONE", b"RESPONSE-A", b"TOOL-OUTPUT-A1", b"THINKING-SECRET", b"SUBAGENT-PROMPT", b"ls -la", b"sk-lf-0123456789abcdef"):
            self.assertNotIn(marker, raw)

    def test_rerun_sends_nothing_new(self):
        before = len(self.server.requests)
        proc, _ = self.run_hook()
        self.assertQuietSuccess(proc)
        self.assertEqual(len(self.server.requests), before)

    def test_every_event_sent_once(self):
        ids = [(e["type"], e["body"]["id"]) for e in self.server.events()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_last_send_recorded(self):
        last = self.state()["last_send"]
        self.assertTrue(last["ok"])
        self.assertEqual(last["status"], 207)
        self.assertIn("time", last)


class PayloadTests(Base):
    def test_exact_request(self):
        proc, _ = self.run_hook()
        self.assertQuietSuccess(proc)
        req = [r for r in self.server.requests if r["path"] == "/api/public/ingestion"][0]
        expected_auth = "Basic " + base64.b64encode(b"pk-lf-test:sk-lf-test-secret-0000").decode()
        self.assertEqual(req["headers"]["Authorization"], expected_auth)
        self.assertEqual(req["headers"]["Content-Type"], "application/json")
        body = json.loads(req["raw"])
        self.assertEqual(list(body), ["batch"])
        for ev in body["batch"]:
            self.assertEqual(sorted(ev), ["body", "id", "timestamp", "type"])
            self.assertIn(ev["type"], ("trace-create", "generation-create", "span-create"))
            self.assertRegex(ev["timestamp"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        det = lambda *p: self.mod.det_id(SESSION, *p)
        gen = [ev for ev in body["batch"] if ev["type"] == "generation-create" and ev["body"]["id"] == det("gen", "msg_C")][0]
        self.assertEqual(gen["body"], {
            "id": det("gen", "msg_C"),
            "traceId": det("trace", "u-1"),
            "name": "claude_code.llm_call",
            "model": "claude-opus-5-5",
            "startTime": "2026-11-20T10:00:09.000Z",
            "completionStartTime": "2026-11-20T10:00:10.000Z",
            "endTime": "2026-11-20T10:00:10.000Z",
            "usageDetails": {"input": 1, "output": 40, "cache_read_input_tokens": 1700},
            "modelParameters": {"speed": "standard"},
            "metadata": {
                "message_id": "msg_C",
                "request_id": "req_C",
                "service_tier": "standard",
                "inference_geo": "global",
                "stop_reason": "end_turn",
                "content_blocks": {"text": 1},
            },
        })
        trace = [ev for ev in body["batch"] if ev["type"] == "trace-create" and ev["body"]["id"] == det("trace", "u-2")][0]
        self.assertEqual(trace["body"], {
            "id": det("trace", "u-2"),
            "timestamp": "2026-11-20T10:02:00.000Z",
            "name": "claude_code.turn",
            "userId": "dev@example.com",
            "sessionId": SESSION,
            "tags": ["claude-code"],
            "metadata": {
                "plugin_version": self.mod.PLUGIN_VERSION,
                "cwd": "/home/dev/example-project",
                "git_branch": "feature/demo",
                "claude_code_version": "2.1.330",
                "entrypoint": "cli",
                "permission_mode": "default",
                "origin": "human",
                "prompt_id": "p-2",
                "prompt_chars": len("PROMPT-TWO: my key is sk-lf-0123456789abcdef, keep it safe"),
            },
        })

    def test_envelope_ids_stable_for_same_body(self):
        proc, _ = self.run_hook()
        first = {(e["type"], e["body"]["id"]): e["id"] for e in self.server.events()}
        shutil.rmtree(self.home)
        self.server.requests.clear()
        proc, _ = self.run_hook()
        second = {(e["type"], e["body"]["id"]): e["id"] for e in self.server.events()}
        self.assertEqual(first, second)

    def test_batches_are_split(self):
        self.mod.MAX_BATCH_EVENTS = 3
        events = [{"id": str(i), "type": "span-create", "body": {"id": str(i)}} for i in range(10)]
        batches = self.mod.split_batches(events)
        self.assertEqual([len(b) for b in batches], [3, 3, 3, 1])
        self.mod.MAX_BATCH_EVENTS = 500
        self.mod.MAX_BATCH_BYTES = 200
        big = [{"id": str(i), "type": "span-create", "body": {"x": "y" * 80}} for i in range(4)]
        self.assertEqual(len(self.mod.split_batches(big)), 4)

    def test_large_session_sent_in_several_requests(self):
        # patch the batch size through a wrapper so the subprocess uses it
        wrapper = os.path.join(self.tmp, "wrapped.py")
        with open(wrapper, "w") as fh:
            fh.write("import importlib.util,sys\nspec=importlib.util.spec_from_file_location('st', %r)\nm=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\nm.MAX_BATCH_EVENTS=4\nsys.exit(m.main([]))\n" % SCRIPT)
        proc = subprocess.run([sys.executable, wrapper], input=self.payload().encode(), env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertQuietSuccess(proc)
        self.assertGreater(len(self.server.requests), 2)
        self.assertEqual(len(self.by_type("generation-create")), 7)


class DeterminismTests(Base):
    def test_same_ids_from_fresh_state(self):
        self.run_hook()
        first = set(self.bodies())
        shutil.rmtree(self.home)
        self.server.requests.clear()
        self.run_hook()
        self.assertEqual(first, set(self.bodies()))

    def test_ids_are_hashes_of_session_and_uuid(self):
        self.assertEqual(self.mod.det_id("s", "trace", "u"), self.mod.det_id("s", "trace", "u"))
        self.assertNotEqual(self.mod.det_id("s", "trace", "u"), self.mod.det_id("s2", "trace", "u"))
        self.assertRegex(self.mod.det_id("s", "gen", "m"), r"^[0-9a-f]{32}$")


class IncrementalTests(Base):
    def _write_lines(self, lines, partial_tail=b""):
        with open(self.transcript, "wb") as fh:
            fh.writelines(lines)
            fh.write(partial_tail)

    def test_two_runs_equal_one_run(self):
        with open(self.transcript, "rb") as fh:
            lines = fh.readlines()
        # split in the middle of msg_A (lines 4..6 share message.id)
        self._write_lines(lines[:4])
        p1, _ = self.run_hook()
        self.assertQuietSuccess(p1)
        first_offset = self.offset()
        self.assertEqual(first_offset, sum(len(l) for l in lines[:4]))
        self._write_lines(lines)
        p2, _ = self.run_hook()
        self.assertQuietSuccess(p2)
        self.assertEqual(self.offset(), sum(len(l) for l in lines))
        split = self.bodies()
        # compare with a single run from scratch
        shutil.rmtree(self.home)
        self.server.requests.clear()
        self.run_hook()
        single = self.bodies()
        self.assertEqual(set(split), set(single))
        for key in single:
            if key[0] == "generation-create":
                self.assertEqual(split[key], single[key], key)
        g = split[("generation-create", self.mod.det_id(SESSION, "gen", "msg_A"))]
        self.assertEqual(g["usageDetails"]["output"], 120)  # not doubled across runs

    def test_second_run_only_sends_new_lines(self):
        with open(self.transcript, "rb") as fh:
            lines = fh.readlines()
        self._write_lines(lines[:13])
        self.run_hook()
        n_first = len(self.server.events())
        self._write_lines(lines)
        self.run_hook()
        second = self.server.events()[n_first:]
        gens = {e["body"]["id"] for e in second if e["type"] == "generation-create"}
        det = lambda *p: self.mod.det_id(SESSION, *p)
        self.assertNotIn(det("gen", "msg_A"), gens)
        self.assertIn(det("gen", "msg_D"), gens)

    def test_partial_last_line_waits(self):
        with open(self.transcript, "rb") as fh:
            lines = fh.readlines()
        self._write_lines(lines[:3], partial_tail=lines[3][:40])
        self.run_hook()
        self.assertEqual(self.offset(), sum(len(l) for l in lines[:3]))

    def test_tool_result_in_later_run_completes_span(self):
        with open(self.transcript, "rb") as fh:
            lines = fh.readlines()
        self._write_lines(lines[:6])  # up to the tool_use, no result yet
        self.run_hook()
        span_id = self.mod.det_id(SESSION, "tool", "toolu_A1")
        self.assertNotIn("endTime", self.by_type("span-create")[span_id])
        self._write_lines(lines)
        self.run_hook()
        span = self.by_type("span-create")[span_id]
        self.assertEqual(span["endTime"], "2026-11-20T10:00:04.000Z")
        self.assertEqual(span["name"], "Bash")
        self.assertEqual(span["parentObservationId"], self.mod.det_id(SESSION, "gen", "msg_A"))

    def test_truncated_file_is_read_again(self):
        self.run_hook()
        with open(self.transcript, "rb") as fh:
            lines = fh.readlines()
        self._write_lines(lines[:5])
        proc, _ = self.run_hook()
        self.assertQuietSuccess(proc)
        self.assertEqual(self.offset(), sum(len(l) for l in lines[:5]))

    def test_concurrent_runs_do_not_double_send(self):
        # Hook input comes from a file, not a pipe, so all four start at once and
        # communicate() has no stdin to write (on 3.12 it fails on a closed stdin pipe).
        stdin_path = os.path.join(self.tmp, "hook-input.json")
        with open(stdin_path, "w") as fh:
            fh.write(self.payload())
        procs = []
        for _ in range(4):
            with open(stdin_path, "rb") as stdin:
                procs.append(subprocess.Popen([sys.executable, SCRIPT], stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env()))
        for p in procs:
            out, err = p.communicate(timeout=60)  # also closes the stdout/stderr pipes
            self.assertEqual((p.returncode, out, err), (0, b"", b""))
        ids = [(e["type"], e["body"]["id"]) for e in self.server.events()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(self.by_type("generation-create")), 7)

    def test_old_sessions_are_cleaned_up(self):
        st = self.mod.empty_state()
        st["sessions"]["old"] = {"updated": time.time() - 8 * 86400, "files": {}}
        st["sessions"]["new"] = {"updated": time.time() - 1 * 86400, "files": {}}
        self.mod.cleanup_state(st)
        self.assertEqual(list(st["sessions"]), ["new"])


class ContentTests(Base):
    def test_content_on_with_truncation_and_redaction(self):
        proc, _ = self.run_hook(AGENTGUARD_LOG_CONTENT="true", AGENTGUARD_MAX_CONTENT_CHARS="100")
        self.assertQuietSuccess(proc)
        det = lambda *p: self.mod.det_id(SESSION, *p)
        t1 = self.by_type("trace-create")[det("trace", "u-1")]
        self.assertEqual(t1["input"], "PROMPT-ONE: list the files in this project")
        self.assertEqual(t1["output"], "FINAL-ANSWER-ONE: there are three entries.")
        t2 = self.by_type("trace-create")[det("trace", "u-2")]
        self.assertIn("[REDACTED]", t2["input"])
        self.assertNotIn("sk-lf-0123456789abcdef", t2["input"])
        g = self.by_type("generation-create")[det("gen", "msg_A")]
        self.assertEqual(g["input"], "PROMPT-ONE: list the files in this project")
        self.assertEqual(g["output"], "RESPONSE-A: I will look.")
        span = self.by_type("span-create")[det("tool", "toolu_A1")]
        self.assertEqual(span["input"]["command"], "ls -la /home/dev/example-project")
        self.assertEqual(span["output"], "TOOL-OUTPUT-A1: README.md src tests")
        raw = b"".join(r["raw"] for r in self.server.requests)
        self.assertNotIn(b"THINKING-SECRET", raw)  # thinking is never sent
        self.assertNotIn(self.server.secret_key.encode(), raw)

    def test_long_fields_are_truncated(self):
        with open(self.transcript, "a") as fh:
            fh.write(json.dumps({"type": "user", "uuid": "u-long", "parentUuid": None, "timestamp": "2026-11-20T11:00:00.000Z", "sessionId": SESSION, "turnOrigin": "human", "message": {"role": "user", "content": "X" * 5000}}) + "\n")
        self.run_hook(AGENTGUARD_LOG_CONTENT="1", AGENTGUARD_MAX_CONTENT_CHARS="200")
        t = self.by_type("trace-create")[self.mod.det_id(SESSION, "trace", "u-long")]
        self.assertTrue(t["input"].startswith("X" * 200 + "... [truncated 4800 chars]"))

    def test_long_response_truncation_reports_full_length(self):
        with open(self.transcript, "a") as fh:
            for i, text in enumerate(("A" * 300, "B" * 300)):
                fh.write(json.dumps({"type": "assistant", "uuid": "lr-%d" % i, "timestamp": "2026-11-20T11:00:0%d.000Z" % i, "sessionId": SESSION, "message": {"id": "msg_LONG", "model": "claude-opus-5-5", "content": [{"type": "text", "text": text}], "usage": {"input_tokens": 1, "output_tokens": 2}}}) + "\n")
        self.run_hook(AGENTGUARD_LOG_CONTENT="true", AGENTGUARD_MAX_CONTENT_CHARS="200")
        g = self.by_type("generation-create")[self.mod.det_id(SESSION, "gen", "msg_LONG")]
        self.assertEqual(g["output"], "A" * 200 + "... [truncated 401 chars]")  # 300 + newline + 300 = 601

    def test_agentguard_format_keys_redacted_in_content(self):
        sk, pk = RedactionTests.AG_SECRET, RedactionTests.AG_PUBLIC
        with open(self.transcript, "a") as fh:
            fh.write(json.dumps({"type": "user", "uuid": "u-ag", "parentUuid": None, "timestamp": "2026-11-20T11:00:00.000Z", "sessionId": SESSION, "turnOrigin": "human", "message": {"role": "user", "content": "keys: %s and %s" % (sk, pk)}}) + "\n")
        self.run_hook(AGENTGUARD_LOG_CONTENT="true")
        t = self.by_type("trace-create")[self.mod.det_id(SESSION, "trace", "u-ag")]
        self.assertEqual(t["input"], "keys: [REDACTED] and [REDACTED]")
        raw = b"".join(r["raw"] for r in self.server.requests)
        self.assertNotIn(sk.encode(), raw)
        self.assertNotIn(pk.encode(), raw)

    def test_configured_secret_redacted_even_in_content(self):
        with open(self.transcript, "a") as fh:
            fh.write(json.dumps({"type": "user", "uuid": "u-sec", "parentUuid": None, "timestamp": "2026-11-20T11:00:00.000Z", "sessionId": SESSION, "turnOrigin": "human", "message": {"role": "user", "content": "the key is " + self.server.secret_key}}) + "\n")
        self.run_hook(AGENTGUARD_LOG_CONTENT="true")
        raw = b"".join(r["raw"] for r in self.server.requests)
        self.assertNotIn(self.server.secret_key.encode(), raw)


class UsageMappingTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()

    def test_split_by_ttl(self):
        u = {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 10, "cache_creation": {"ephemeral_5m_input_tokens": 4, "ephemeral_1h_input_tokens": 6}}
        self.assertEqual(self.mod.build_usage_details(u), {"input": 1, "output": 2, "cache_read_input_tokens": 3, "input_cache_creation_5m": 4, "input_cache_creation_1h": 6})

    def test_split_off_uses_single_key(self):
        self.mod.SPLIT_CACHE_CREATION_BY_TTL = False
        u = {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 10, "cache_creation": {"ephemeral_5m_input_tokens": 4, "ephemeral_1h_input_tokens": 6}}
        self.assertEqual(self.mod.build_usage_details(u), {"input": 1, "output": 2, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 10})

    def test_unexplained_remainder_goes_to_5m(self):
        u = {"cache_creation_input_tokens": 10, "cache_creation": {"ephemeral_1h_input_tokens": 6}}
        self.assertEqual(self.mod.build_usage_details(u), {"input_cache_creation_5m": 4, "input_cache_creation_1h": 6})

    def test_key_names_come_from_one_place(self):
        self.mod.USAGE_KEYS = dict(self.mod.USAGE_KEYS, input_tokens="input_tokens", output_tokens=None)
        self.assertEqual(self.mod.build_usage_details({"input_tokens": 5, "output_tokens": 7}), {"input_tokens": 5})

    def test_bad_values_ignored(self):
        self.assertEqual(self.mod.build_usage_details({"input_tokens": "5", "output_tokens": True, "cache_read_input_tokens": -1}), {})
        self.assertEqual(self.mod.build_usage_details(None), {})


class FailureTests(Base):
    def test_wrong_keys_401(self):
        proc, _ = self.run_hook(AGENTGUARD_SECRET_KEY="sk-lf-wrong-key-123456")
        self.assertQuietSuccess(proc)
        self.assertEqual(self.offset(), 0)
        last = self.state()["last_send"]
        self.assertFalse(last["ok"])
        self.assertEqual(last["status"], 401)
        log = self.error_log()
        self.assertIn("HTTP 401", log)
        self.assertNotIn("sk-lf-wrong-key-123456", log)
        self.assertNotIn("sk-lf-wrong-key-123456", json.dumps(self.state()))

    def test_403_points_to_the_server_message_not_the_keys(self):
        self.server.status = 403
        proc, _ = self.run_hook()
        self.assertQuietSuccess(proc)
        self.assertEqual(self.offset(), 0)
        detail = self.state()["last_send"]["detail"]
        self.assertIn("HTTP 403: fake failure (AgentGuard refused the data; see its message)", detail)
        self.assertNotIn("check AGENTGUARD_PUBLIC_KEY", detail)

    def test_server_down(self):
        proc, elapsed = self.run_hook(AGENTGUARD_HOST="http://127.0.0.1:%d" % free_port())
        self.assertQuietSuccess(proc)
        self.assertEqual(self.offset(), 0)
        self.assertFalse(self.state()["last_send"]["ok"])
        self.assertLess(elapsed, 5)

    def test_timeout_limits_the_run(self):
        self.server.delay = 6
        proc, elapsed = self.run_hook(AGENTGUARD_TIMEOUT_SECONDS="2")
        self.assertQuietSuccess(proc)
        self.assertLess(elapsed, 4.0)
        self.assertEqual(self.offset(), 0)
        self.assertIn("timed out", self.error_log().lower())

    def test_retried_after_failure(self):
        self.server.status = 503
        self.run_hook()
        self.assertEqual(self.offset(), 0)
        self.server.status = 207
        proc, _ = self.run_hook()
        self.assertQuietSuccess(proc)
        self.assertGreater(self.offset(), 0)
        self.assertEqual(len(self.by_type("generation-create")), 7)
        self.assertTrue(self.state()["last_send"]["ok"])

    def test_per_event_server_error_is_retried(self):
        self.server.event_errors = lambda ev: (500, "boom") if ev["type"] == "span-create" else None
        self.run_hook()
        self.assertEqual(self.offset(), 0)

    def test_per_event_client_error_is_not_retried(self):
        self.server.event_errors = lambda ev: (400, "invalid") if ev["type"] == "span-create" else None
        proc, _ = self.run_hook()
        self.assertQuietSuccess(proc)
        self.assertGreater(self.offset(), 0)
        self.assertIn("rejected", self.error_log())

    def test_malformed_line_is_skipped(self):
        with open(self.transcript, "rb") as fh:
            lines = fh.readlines()
        lines.insert(5, b"{this is not json\n")
        lines.insert(6, b"[1, 2, 3]\n")
        lines.insert(7, json.dumps({"type": "assistant", "message": "not a dict"}).encode() + b"\n")
        with open(self.transcript, "wb") as fh:
            fh.writelines(lines)
        proc, _ = self.run_hook()
        self.assertQuietSuccess(proc)
        self.assertEqual(self.offset(), sum(len(l) for l in lines))
        self.assertEqual(len(self.by_type("generation-create")), 7)
        self.assertIn("malformed", self.error_log())

    def test_bad_stdin(self):
        for stdin in ("", "not json", "[]", json.dumps({"session_id": SESSION}), json.dumps({"session_id": SESSION, "transcript_path": "/nonexistent/x.jsonl"})):
            proc, _ = self.run_hook(stdin=stdin)
            self.assertQuietSuccess(proc)

    def test_missing_keys_is_silent_noop(self):
        proc, _ = self.run_hook(AGENTGUARD_PUBLIC_KEY=None, AGENTGUARD_SECRET_KEY=None)
        self.assertQuietSuccess(proc)
        self.assertEqual(self.server.requests, [])
        self.assertFalse(os.path.exists(self.home))

    def test_corrupt_state_recovers(self):
        os.makedirs(self.home)
        with open(os.path.join(self.home, "state.json"), "w") as fh:
            fh.write("{broken")
        proc, _ = self.run_hook()
        self.assertQuietSuccess(proc)
        self.assertEqual(len(self.by_type("generation-create")), 7)

    def test_unwritable_home_still_exits_0(self):
        blocker = os.path.join(self.tmp, "file-not-dir")
        open(blocker, "w").close()
        proc, _ = self.run_hook(AGENTGUARD_HOME=os.path.join(blocker, "sub"))
        self.assertQuietSuccess(proc)

    def test_debug_prints_to_stderr_only(self):
        proc, _ = self.run_hook(AGENTGUARD_DEBUG="true", AGENTGUARD_SECRET_KEY="sk-lf-wrong-key-123456")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")
        self.assertIn(b"[agentguard]", proc.stderr)
        self.assertNotIn(b"sk-lf-wrong-key-123456", proc.stderr)

    def test_error_log_rotates(self):
        self.mod.ERROR_LOG_MAX_BYTES = 1000
        os.environ["AGENTGUARD_HOME"] = self.home
        try:
            for _ in range(40):
                self.mod.log_error("x" * 50)
        finally:
            del os.environ["AGENTGUARD_HOME"]
        self.assertTrue(os.path.exists(os.path.join(self.home, "error.log.1")))
        self.assertLess(os.path.getsize(os.path.join(self.home, "error.log")), 1200)


class TimeoutTests(Base):
    """AGENTGUARD_TIMEOUT_SECONDS: the total time one run may spend sending."""

    def test_default_is_15_seconds(self):
        cfg = self.mod.Config({})
        self.assertEqual((cfg.timeout, cfg.timeout_warning), (15.0, None))
        self.assertEqual(self.mod.Sender(cfg).budget, 15.0)

    def test_custom_values(self):
        for raw, expected in (("30", 30.0), (" 2.5 ", 2.5), ("1", 1.0), ("60", 60.0)):
            cfg = self.mod.Config({"AGENTGUARD_TIMEOUT_SECONDS": raw})
            self.assertEqual((cfg.timeout, cfg.timeout_warning), (expected, None), raw)

    def test_out_of_range_falls_back(self):
        for raw in ("0", "0.5", "61", "-3", "inf"):
            cfg = self.mod.Config({"AGENTGUARD_TIMEOUT_SECONDS": raw})
            self.assertEqual(cfg.timeout, 15.0, raw)
            self.assertIn("is not a number from 1 to 60; using 15", cfg.timeout_warning)

    def test_invalid_text_falls_back(self):
        for raw in ("abc", "nan", "15s", "1e"):
            cfg = self.mod.Config({"AGENTGUARD_TIMEOUT_SECONDS": raw})
            self.assertEqual(cfg.timeout, 15.0, raw)
            self.assertTrue(cfg.timeout_warning, raw)

    def test_invalid_value_is_logged_once(self):
        for _ in range(3):
            proc, _ = self.run_hook(AGENTGUARD_TIMEOUT_SECONDS="abc")
            self.assertQuietSuccess(proc)
        self.assertEqual(self.error_log().count("AGENTGUARD_TIMEOUT_SECONDS='abc'"), 1)
        self.run_hook(AGENTGUARD_TIMEOUT_SECONDS="999")  # a different bad value is logged again
        self.assertEqual(self.error_log().count("AGENTGUARD_TIMEOUT_SECONDS="), 2)
        self.assertEqual(len(self.by_type("generation-create")), 7)  # and sending still works

    def test_slow_server_succeeds_with_default(self):
        self.server.delay = 5
        proc, elapsed = self.run_hook()
        self.assertQuietSuccess(proc)
        self.assertGreaterEqual(elapsed, 5)
        self.assertGreater(self.offset(), 0)
        self.assertTrue(self.state()["last_send"]["ok"])
        self.assertEqual(len(self.by_type("generation-create")), 7)

    def test_slow_server_fails_with_2_seconds_and_is_retried(self):
        self.server.delay = 5
        proc, elapsed = self.run_hook(AGENTGUARD_TIMEOUT_SECONDS="2")
        self.assertQuietSuccess(proc)
        self.assertLess(elapsed, 4.5)
        self.assertEqual(self.offset(), 0)
        last = self.state()["last_send"]
        self.assertFalse(last["ok"])
        self.assertIn("timed out", last["detail"].lower())

    def test_status_shows_timeout(self):
        out = subprocess.run([sys.executable, SCRIPT, "--status"], env=self.env(AGENTGUARD_TIMEOUT_SECONDS="25"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30).stdout.decode()
        self.assertIn("Timeout: 25s for all requests in one send (AGENTGUARD_TIMEOUT_SECONDS)", out)
        out = subprocess.run([sys.executable, SCRIPT, "--status"], env=self.env(AGENTGUARD_TIMEOUT_SECONDS="zero"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30).stdout.decode()
        self.assertIn("Timeout: 15s", out)
        self.assertIn("WARNING: AGENTGUARD_TIMEOUT_SECONDS='zero'", out)


class DetachTests(Base):
    server_delay = 0.7  # slower than the hook may take, well inside the default budget

    def test_hook_returns_immediately_and_worker_sends(self):
        proc, elapsed = self.run_hook(AGENTGUARD_SYNC=None)
        self.assertQuietSuccess(proc)
        self.assertLess(elapsed, 1.0)
        deadline = time.time() + 15
        while time.time() < deadline and self.offset("agent-afixture01") == 0:
            time.sleep(0.2)
        self.assertGreater(self.offset(), 0)
        self.assertEqual(len(self.by_type("generation-create")), 7)

    def _large_input_is_sent(self, size):
        stdin = self.payload(last_assistant_message="x" * size)
        proc, elapsed = self.run_hook(stdin=stdin, AGENTGUARD_SYNC=None)
        self.assertQuietSuccess(proc)
        self.assertLess(elapsed, 1.5)
        self.assertGreater(self.wait_for_offset("agent-afixture01"), 0, self.error_log())
        self.assertEqual(len(self.by_type("generation-create")), 7)
        self.assertNotIn("worker", self.error_log())

    def test_500kb_last_assistant_message(self):
        # over Linux's 128 KB limit for one environment variable
        self._large_input_is_sent(500 * 1024)

    def test_2mb_last_assistant_message(self):
        # over macOS's ~1 MB limit for the whole environment
        self._large_input_is_sent(2 * 1024 * 1024)


class HookPayloadTests(unittest.TestCase):
    """In-process checks of what hook mode hands to the worker."""

    HOOK_INPUT = {
        "session_id": SESSION,
        "transcript_path": "/tmp/x/%s.jsonl" % SESSION,
        "cwd": "/tmp/x",
        "hook_event_name": "Stop",
        "prompt_id": "p-1",
        "permission_mode": "default",
        "stop_hook_active": False,
        "last_assistant_message": "y" * (500 * 1024),
        "background_tasks": [],
    }

    def setUp(self):
        self.mod = load_module()
        self.tmp = tempfile.mkdtemp(prefix="agentguard-hook-")
        self.env = mock.patch.dict(os.environ, {"AGENTGUARD_PUBLIC_KEY": "pk-lf-test", "AGENTGUARD_SECRET_KEY": "sk-lf-test-secret-0000", "AGENTGUARD_HOME": self.tmp})
        self.env.start()
        for name in ("AGENTGUARD_SYNC", "AGENTGUARD_DEBUG"):
            os.environ.pop(name, None)

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_main(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(self.HOOK_INPUT))), mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            code = self.mod.main([])
        self.assertEqual((code, out.getvalue(), err.getvalue()), (0, "", ""))

    def test_worker_env_has_only_the_four_fields(self):
        with mock.patch.object(self.mod.subprocess, "Popen") as popen:
            self.run_main()
        self.assertEqual(popen.call_count, 1)
        env = popen.call_args[1]["env"]
        payload = json.loads(env["AGENTGUARD_HOOK_PAYLOAD"])
        self.assertEqual(payload, {"session_id": SESSION, "transcript_path": "/tmp/x/%s.jsonl" % SESSION, "cwd": "/tmp/x", "hook_event_name": "Stop"})
        self.assertLess(sum(len(k) + len(v) for k, v in env.items()), 100 * 1024)

    def test_sync_mode_gets_the_same_four_fields(self):
        os.environ["AGENTGUARD_SYNC"] = "1"
        with mock.patch.object(self.mod, "run_worker") as run_worker:
            self.run_main()
        self.assertEqual(sorted(run_worker.call_args[0][0]), ["cwd", "hook_event_name", "session_id", "transcript_path"])

    def test_missing_fields_are_left_out(self):
        self.HOOK_INPUT = {"session_id": SESSION, "last_assistant_message": "z"}
        with mock.patch.object(self.mod.subprocess, "Popen") as popen:
            self.run_main()
        self.assertEqual(json.loads(popen.call_args[1]["env"]["AGENTGUARD_HOOK_PAYLOAD"]), {"session_id": SESSION})

    def test_spawn_oserror_is_logged_and_exits_0(self):
        with mock.patch.object(self.mod.subprocess, "Popen", side_effect=OSError(7, "Argument list too long")):
            self.run_main()
        with open(os.path.join(self.tmp, "error.log")) as fh:
            log = fh.read()
        self.assertIn("could not start the background worker: OSError", log)


class RedactionTests(unittest.TestCase):
    AG_SECRET = "sk-ag-3f2a9c1e-7b4d-4e8a-9c2f-1a2b3c4d5e6f"
    AG_PUBLIC = "pk-ag-0e1d2c3b-4a59-4687-b7c6-d5e4f3a2b1c0"
    LF_SECRET = "sk-lf-0123456789abcdef"
    LF_PUBLIC = "pk-lf-fedcba9876543210"

    def setUp(self):
        self.mod = load_module()

    def test_each_format_is_redacted(self):
        for key in (self.AG_SECRET, self.AG_PUBLIC, self.LF_SECRET, self.LF_PUBLIC):
            self.assertEqual(self.mod.redact("auth with %s failed" % key), "auth with [REDACTED] failed", key)

    def test_other_text_is_left_alone(self):
        for text in ("pk-ag-", "sk-ag-short", "task-ag-12345678abcdef", "disk-lf-12345678abcdef", "sk-xx-12345678abcdef"):
            self.assertEqual(self.mod.redact(text), text)

    def test_error_log_never_holds_keys(self):
        tmp = tempfile.mkdtemp(prefix="agentguard-redact-")
        try:
            with mock.patch.dict(os.environ, {"AGENTGUARD_HOME": tmp}):
                self.mod.log_error("keys %s %s %s %s" % (self.AG_SECRET, self.AG_PUBLIC, self.LF_SECRET, self.LF_PUBLIC))
            with open(os.path.join(tmp, "error.log")) as fh:
                log = fh.read()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(log.count("[REDACTED]"), 4)
        for key in (self.AG_SECRET, self.AG_PUBLIC, self.LF_SECRET, self.LF_PUBLIC):
            self.assertNotIn(key, log)


class SubagentOrderTests(Base):
    def _hide_agent_call(self):
        with open(self.transcript, "rb") as fh:
            lines = fh.readlines()
        keep = [l for l in lines if b"toolu_B1" not in l]
        with open(self.transcript, "wb") as fh:
            fh.writelines(keep)

    def test_subagent_waits_for_parent_then_orphan_on_session_end(self):
        self._hide_agent_call()
        self.run_hook("Stop")
        det = lambda *p: self.mod.det_id(SESSION, *p)
        self.assertNotIn(det("gen", "msg_S1"), self.by_type("generation-create"))
        self.assertEqual(self.offset("agent-afixture01"), 0)
        self.run_hook("SessionEnd")
        g = self.by_type("generation-create")[det("gen", "msg_S1")]
        orphan_trace = det("trace", "agent", "afixture01")
        self.assertEqual(g["traceId"], orphan_trace)
        t = self.by_type("trace-create")[orphan_trace]
        self.assertEqual(t["metadata"]["origin"], "subagent")
        self.assertEqual(t["sessionId"], SESSION)

    def test_subagent_appended_later_is_sent(self):
        sub = os.path.join(self.project, SESSION, "subagents", "agent-afixture01.jsonl")
        with open(sub, "rb") as fh:
            lines = fh.readlines()
        with open(sub, "wb") as fh:
            fh.writelines(lines[:2])
        self.run_hook()
        with open(sub, "wb") as fh:
            fh.writelines(lines)
        self.run_hook()
        g = self.by_type("generation-create")[self.mod.det_id(SESSION, "gen", "msg_S2")]
        self.assertEqual(g["parentObservationId"], self.mod.det_id(SESSION, "tool", "toolu_B1"))


class UserIdTests(Base):
    def test_env_user_wins(self):
        cfg = self.mod.Config({"AGENTGUARD_USER": "someone@example.com"})
        self.assertEqual(self.mod.resolve_user(cfg, None), "someone@example.com")

    def test_falls_back_to_git_then_os_user(self):
        cfg = self.mod.Config({})
        user = self.mod.resolve_user(cfg, self.tmp)
        self.assertTrue(user)


class PluginFilesTests(unittest.TestCase):
    def test_versions_match(self):
        mod = load_module()
        with open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json")) as fh:
            plugin = json.load(fh)
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as fh:
            market = json.load(fh)
        self.assertEqual(plugin["name"], "agentguard")
        self.assertEqual(plugin["version"], mod.PLUGIN_VERSION)
        self.assertEqual(plugin["license"], "Apache-2.0")
        with open(os.path.join(ROOT, "LICENSE")) as fh:
            self.assertIn("Apache License", fh.read(200))
        with open(os.path.join(ROOT, "CHANGELOG.md")) as fh:
            self.assertIn("## %s" % mod.PLUGIN_VERSION, fh.read())
        self.assertEqual(market["name"], "actaclad")
        entry = market["plugins"][0]
        self.assertEqual(entry["name"], "agentguard")
        self.assertEqual(entry["source"], "./plugins/agentguard")
        self.assertEqual(entry["version"], mod.PLUGIN_VERSION)

    def test_hooks(self):
        with open(os.path.join(PLUGIN, "hooks", "hooks.json")) as fh:
            hooks = json.load(fh)["hooks"]
        self.assertEqual(sorted(hooks), ["SessionEnd", "Stop"])
        self.assertEqual(hooks["Stop"], hooks["SessionEnd"])
        h = hooks["Stop"][0]["hooks"][0]
        self.assertEqual(h["type"], "command")
        self.assertNotIn("args", h)  # shell form: the same text must run in sh and in PowerShell
        self.assertNotIn("shell", h)  # default shell: sh / Git Bash, or PowerShell without Git Bash
        lines = h["command"].split("\n")
        self.assertEqual(lines[0], "echo `# <#` >/dev/null")  # sh: no-op; PowerShell: opens a block comment
        self.assertEqual(lines[1], 'sh "${CLAUDE_PLUGIN_ROOT}/scripts/agentguard.sh"; exit 0')
        self.assertTrue(lines[2].startswith("#> > $null; "))  # sh: comment; PowerShell: closes it
        self.assertIn("agentguard.ps1", lines[2])
        self.assertTrue(lines[2].endswith("exit 0"))
        for name in ("agentguard.sh", "agentguard.ps1", "send_trace.py"):
            self.assertTrue(os.path.isfile(os.path.join(PLUGIN, "scripts", name)), name)

    def test_launchers_have_lf_line_endings(self):
        for name in ("agentguard.sh", "agentguard.ps1"):
            with open(os.path.join(PLUGIN, "scripts", name), "rb") as fh:
                self.assertNotIn(b"\r\n", fh.read(), name)
        with open(os.path.join(ROOT, ".gitattributes")) as fh:
            self.assertIn("eol=lf", fh.read())

    def test_python38_syntax(self):
        import ast
        for path in (SCRIPT,):
            with open(path) as fh:
                ast.parse(fh.read(), feature_version=(3, 8))


class StatusSourcesTests(Base):
    """--status shows keys from the environment, or else from settings.json's env block."""

    ENV_PK = "pk-ag-11110000-aaaa-4bbb-8ccc-00000000aaaa"
    ENV_SK = "sk-ag-22220000-aaaa-4bbb-8ccc-00000000bbbb"
    FILE_PK = "pk-ag-08710000-dddd-4eee-8fff-000000003dd3"
    FILE_SK = "sk-ag-99990000-dddd-4eee-8fff-00000000cafe"

    def status(self, settings=None, raw_settings=None, **env):
        path = os.path.join(self.claude_dir, "settings.json")
        if raw_settings is not None:
            with open(path, "w") as fh:
                fh.write(raw_settings)
        elif settings is not None:
            with open(path, "w") as fh:
                json.dump(settings, fh)
        env.setdefault("CLAUDECODE", None)  # as in a plain terminal
        p = subprocess.run([sys.executable, SCRIPT, "--status"], env=self.env(**env), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = p.stdout.decode()
        for secret in (self.ENV_SK, self.FILE_SK):
            self.assertNotIn(secret, out)
        with open(path) if os.path.exists(path) else io.StringIO("") as fh:
            before = fh.read()
        return out, before

    def test_keys_only_in_environment(self):
        out, _ = self.status(AGENTGUARD_PUBLIC_KEY=self.ENV_PK, AGENTGUARD_SECRET_KEY=self.ENV_SK)
        self.assertIn("Public key: pk-ag-...aaaa (from the environment)", out)
        self.assertIn("Secret key: set (ends ...bbbb) (from the environment)", out)
        self.assertNotIn("Keys are not set", out)

    def test_keys_only_in_settings_json(self):
        settings = {"model": "opus", "env": {"AGENTGUARD_HOST": "https://ag.example.com", "AGENTGUARD_PUBLIC_KEY": self.FILE_PK, "AGENTGUARD_SECRET_KEY": self.FILE_SK, "AGENTGUARD_TIMEOUT_SECONDS": 20}}
        out, _ = self.status(settings, AGENTGUARD_HOST=None, AGENTGUARD_PUBLIC_KEY=None, AGENTGUARD_SECRET_KEY=None)
        self.assertIn("Host: https://ag.example.com (from settings.json, used by new Claude Code sessions)", out)
        self.assertIn("Public key: pk-ag-...3dd3 (from settings.json, used by new Claude Code sessions)", out)
        self.assertIn("Secret key: set (ends ...cafe) (from settings.json, used by new Claude Code sessions)", out)
        self.assertIn("Timeout: 20s", out)
        self.assertNotIn("Keys are not set", out)
        self.assertNotIn("restart Claude Code", out)  # normal in a terminal
        with open(os.path.join(self.claude_dir, "settings.json")) as fh:
            self.assertEqual(json.load(fh), settings)  # read only

    def test_settings_only_inside_claude_code_says_restart(self):
        settings = {"env": {"AGENTGUARD_PUBLIC_KEY": self.FILE_PK, "AGENTGUARD_SECRET_KEY": self.FILE_SK}}
        out, _ = self.status(settings, CLAUDECODE="1", AGENTGUARD_PUBLIC_KEY=None, AGENTGUARD_SECRET_KEY=None)
        self.assertIn("not in this Claude Code session: restart Claude Code", out)

    def test_keys_in_both_environment_wins(self):
        settings = {"env": {"AGENTGUARD_PUBLIC_KEY": self.FILE_PK, "AGENTGUARD_SECRET_KEY": self.FILE_SK}}
        out, _ = self.status(settings, AGENTGUARD_PUBLIC_KEY=self.ENV_PK, AGENTGUARD_SECRET_KEY=self.ENV_SK)
        self.assertIn("Public key: pk-ag-...aaaa (from the environment)", out)
        self.assertIn("Secret key: set (ends ...bbbb) (from the environment)", out)
        self.assertIn("Note: settings.json has a different public key", out)
        self.assertIn("Note: settings.json has a different secret key", out)

    def test_same_keys_in_both_no_note(self):
        settings = {"env": {"AGENTGUARD_PUBLIC_KEY": self.ENV_PK, "AGENTGUARD_SECRET_KEY": self.ENV_SK}}
        out, _ = self.status(settings, AGENTGUARD_PUBLIC_KEY=self.ENV_PK, AGENTGUARD_SECRET_KEY=self.ENV_SK)
        self.assertNotIn("Note:", out)

    def test_keys_in_neither(self):
        out, _ = self.status({"env": {"FOO": "1"}}, AGENTGUARD_HOST=None, AGENTGUARD_PUBLIC_KEY=None, AGENTGUARD_SECRET_KEY=None)
        self.assertIn("Host: https://ag-dev.actaclad.com (default)", out)
        self.assertIn("Public key: (not set)", out)
        self.assertIn("Secret key: (not set)", out)
        self.assertIn("Keys are not set: run /agentguard:setup", out)

    def test_broken_settings_json(self):
        for raw in ("{ not json", "[1, 2]", '{"env": "oops"}'):
            out, before = self.status(raw_settings=raw, AGENTGUARD_PUBLIC_KEY=None, AGENTGUARD_SECRET_KEY=None)
            self.assertIn("WARNING: could not read", out, raw)
            self.assertIn("Keys are not set", out, raw)
            self.assertEqual(before, raw)  # untouched


class CollectorDetectionTests(Base):
    """Old OpenTelemetry collector on the OTLP/HTTP port: found with a socket connect plus a
    fake docker on PATH (a sh script, or docker.cmd on Windows). Never stopped, only reported."""

    def setUp(self):
        super().setUp()
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(5)
        self.port = self.listener.getsockname()[1]
        self.mod.OTEL_COLLECTOR_PORT = self.port
        self.bin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.bin)
        self.docker_out = os.path.join(self.tmp, "docker-out.txt")
        self.docker_mark = os.path.join(self.tmp, "docker-called")

    def tearDown(self):
        self.listener.close()
        super().tearDown()

    def fake_docker(self, lines, exit_code=0):
        with open(self.docker_out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(l + "\n" for l in lines))
        if os.name == "nt":
            path = os.path.join(self.bin, "docker.cmd")
            with open(path, "w", newline="\r\n") as fh:
                fh.write('@echo off\necho %%*>>"%s"\ntype "%s"\nexit /b %d\n' % (self.docker_mark, self.docker_out, exit_code))
        else:  # shell builtins only: PATH holds nothing else
            path = os.path.join(self.bin, "docker")
            with open(path, "w") as fh:
                fh.write('#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s"\nwhile IFS= read -r l; do printf "%%s\\n" "$l"; done < "%s"\nexit %d\n' % (self.docker_mark, self.docker_out, exit_code))
            os.chmod(path, 0o755)

    def advice(self, path=None):
        with mock.patch.dict(os.environ, {"PATH": self.bin if path is None else path}):
            return self.mod.collector_advice()

    def expected_cd(self, workdir):
        if os.name == "nt":
            return "  cd '%s'; docker compose down" % workdir.replace("'", "''")
        return "  cd '%s' && docker compose down" % workdir.replace("'", "'\\''")

    def test_compose_project_gives_cd_and_down(self):
        workdir = os.path.join(self.tmp, "old setup", "claude's telemetry")
        self.fake_docker(["claude-telemetry-otel-collector-1\t0.0.0.0:%d->4318/tcp, [::]:%d->4318/tcp\t%s" % (self.port, self.port, workdir)])
        lines = self.advice()
        self.assertIn("WARNING: an OpenTelemetry collector is still running (container claude-telemetry-otel-collector-1). Stop it with:", lines)
        self.assertIn(self.expected_cd(workdir), lines)
        self.assertIn("only in the folder shown", lines[-1])

    def test_port_ranges_and_other_ports(self):
        p = self.mod.publishes_port
        self.assertTrue(p("0.0.0.0:4318->4318/tcp", 4318))
        self.assertTrue(p("0.0.0.0:4317-4319->4317-4319/tcp", 4318))
        self.assertTrue(p("[::]:4318->4318/tcp", 4318))
        self.assertFalse(p("0.0.0.0:14318->4318/tcp", 4318))  # host port differs
        self.assertFalse(p("4318/tcp", 4318))  # exposed, not published
        self.assertFalse(p("0.0.0.0:5432->5432/tcp", 4318))
        self.assertFalse(p("", 4318))

    def test_no_compose_label_gives_stop_and_restart_no(self):
        self.fake_docker(["otel\t0.0.0.0:%d->4318/tcp\t" % self.port])
        lines = self.advice()
        self.assertEqual(lines[:3], ["WARNING: an OpenTelemetry collector is still running (container otel). Stop it with:", "  docker stop otel", "  docker update --restart=no otel"])

    def test_one_command_per_compose_project(self):
        self.fake_docker(["a-1\t0.0.0.0:%d->4318/tcp\t/srv/otel" % self.port, "b-1\t127.0.0.1:%d->4318/tcp\t/srv/otel" % self.port, "db\t0.0.0.0:5432->5432/tcp\t/srv/other"])
        lines = self.advice()
        self.assertEqual([l for l in lines if "docker compose down" in l and l.startswith("  cd")], [self.expected_cd("/srv/otel")])
        self.assertIn("(container a-1, b-1)", " ".join(lines))
        self.assertNotIn("/srv/other", " ".join(lines))

    def test_listening_but_no_matching_container(self):
        self.fake_docker(["db\t0.0.0.0:5432->5432/tcp\t/srv/db"])
        lines = self.advice()
        self.assertEqual(len(lines), 1)
        self.assertIn("something is listening on 127.0.0.1:%d" % self.port, lines[0])
        self.assertIn("docker-compose.yml, and nowhere else", lines[0])

    def test_docker_missing_or_failing_is_quiet(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        self.assertEqual(len(self.advice(path=empty)), 1)  # only the generic note
        self.fake_docker(["garbage"], exit_code=1)
        self.assertEqual(len(self.advice()), 1)
        with mock.patch.object(self.mod.subprocess, "run", side_effect=OSError("no docker daemon")):
            self.assertEqual(len(self.advice()), 1)

    def test_nothing_listening_means_no_output_and_no_docker_call(self):
        self.listener.close()
        self.fake_docker(["otel\t0.0.0.0:%d->4318/tcp\t/srv/otel" % self.port])
        self.assertEqual(self.advice(), [])
        self.assertFalse(os.path.exists(self.docker_mark))

    def test_status_and_setup_print_it_and_never_run_compose(self):
        self.fake_docker(["otel-1\t0.0.0.0:%d->4318/tcp\t/srv/otel" % self.port])
        out = io.StringIO()
        env = {"PATH": self.bin, "AGENTGUARD_HOME": self.home, "CLAUDE_CONFIG_DIR": self.claude_dir,
               "AGENTGUARD_PUBLIC_KEY": self.server.public_key, "AGENTGUARD_SECRET_KEY": self.server.secret_key, "AGENTGUARD_HOST": self.server.url}
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "stdout", out):
            self.mod.status_main()
        self.assertIn(self.expected_cd("/srv/otel"), out.getvalue())
        out = io.StringIO()
        stdin = mock.Mock()
        stdin.buffer = io.BytesIO((self.server.secret_key + "\n").encode())
        stdin.isatty = lambda: False
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stdin", stdin):
            code = self.mod.main(["--setup", "--host", self.server.url, "--public-key", self.server.public_key, "--secret-stdin"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn(self.expected_cd("/srv/otel"), out.getvalue())
        # docker was only ever asked to list containers: nothing was stopped or removed
        with open(self.docker_mark) as fh:
            calls = [c.strip() for c in fh.read().splitlines() if c.strip()]
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertTrue(call.startswith("ps --format"), call)


class WindowsCodePathTests(unittest.TestCase):
    """The Windows-only branches, exercised with mocks on every platform."""

    def setUp(self):
        self.mod = load_module()
        self.mod.IS_WINDOWS = True
        self.tmp = tempfile.mkdtemp(prefix="agentguard-win-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_worker_breaks_away_from_job_then_falls_back(self):
        with mock.patch.object(self.mod.subprocess, "Popen", side_effect=[OSError(5, "Access is denied"), mock.Mock()]) as popen:
            self.mod.spawn_worker("{}")
        first, second = popen.call_args_list
        self.assertEqual(first[1]["creationflags"], 0x00000008 | 0x00000200 | 0x01000000)
        self.assertEqual(second[1]["creationflags"], 0x00000008 | 0x00000200)
        for call in (first, second):
            self.assertNotIn("start_new_session", call[1])
            self.assertEqual(call[1]["stdin"], subprocess.DEVNULL)
            self.assertTrue(call[1]["close_fds"])

    def test_git_runs_without_a_console_window(self):
        with mock.patch.object(self.mod.subprocess, "run", return_value=mock.Mock(stdout=b"a@b.c\n")) as run:
            self.assertEqual(self.mod.resolve_user(self.mod.Config({}), None), "a@b.c")
        self.assertEqual(run.call_args[1]["creationflags"], 0x08000000)

    def test_replace_retries_while_file_is_open(self):
        with mock.patch.object(self.mod.os, "replace", side_effect=[PermissionError(13, "in use"), PermissionError(13, "in use"), None]) as rep, mock.patch.object(self.mod.time, "sleep"):
            self.mod.replace_file("a", "b")
        self.assertEqual(rep.call_count, 3)

    def test_replace_gives_up_after_attempts(self):
        with mock.patch.object(self.mod.os, "replace", side_effect=PermissionError(13, "in use")), mock.patch.object(self.mod.time, "sleep"):
            with self.assertRaises(PermissionError):
                self.mod.replace_file("a", "b", attempts=3)

    def test_not_windows_does_not_retry(self):
        self.mod.IS_WINDOWS = False
        with mock.patch.object(self.mod.os, "replace", side_effect=PermissionError(13, "denied")) as rep:
            with self.assertRaises(PermissionError):
                self.mod.replace_file("a", "b")
        self.assertEqual(rep.call_count, 1)

    def test_settings_written_in_place_when_replace_keeps_failing(self):
        path = os.path.join(self.tmp, "settings.json")
        with open(path, "w") as fh:
            json.dump({"model": "opus", "env": {"FOO": "1"}}, fh)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.tmp}), mock.patch.object(self.mod, "replace_file", side_effect=PermissionError(32, "in use")):
            self.mod.write_settings({"AGENTGUARD_HOST": "https://h"}, [])
        with open(path) as fh:
            self.assertEqual(json.load(fh), {"model": "opus", "env": {"FOO": "1", "AGENTGUARD_HOST": "https://h"}})
        self.assertEqual(sorted(os.listdir(self.tmp)), ["settings.json", "settings.json.agentguard-backup"])

    def test_hook_reads_stdin_as_utf8_bytes(self):
        data = json.dumps({"session_id": "s", "transcript_path": "C:\\Users\\Jos\u00e9\\t.jsonl", "hook_event_name": "Stop"}, ensure_ascii=False).encode("utf-8")
        stdin = mock.Mock()
        stdin.buffer = io.BytesIO(b"\xef\xbb\xbf" + data)  # with a BOM, as PowerShell may add
        env = {"AGENTGUARD_PUBLIC_KEY": "pk-lf-x", "AGENTGUARD_SECRET_KEY": "sk-lf-x", "AGENTGUARD_HOME": self.tmp, "AGENTGUARD_SYNC": "1"}
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "stdin", stdin), mock.patch.object(self.mod, "run_worker") as run_worker:
            self.mod.main([])
        self.assertEqual(run_worker.call_args[0][0]["transcript_path"], "C:\\Users\\Jos\u00e9\\t.jsonl")

    def test_status_shows_python(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"AGENTGUARD_HOME": self.tmp, "CLAUDE_CONFIG_DIR": self.tmp}), mock.patch.object(sys, "stdout", out):
            self.mod.status_main()
        self.assertIn("Python: %s (" % sys.executable, out.getvalue())


class SetupAndStatusTests(Base):
    def run_cli(self, args, stdin="", **env):
        return subprocess.run([sys.executable, SCRIPT] + args, input=stdin.encode(), env=self.env(**env), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)

    def settings(self):
        with open(os.path.join(self.claude_dir, "settings.json")) as fh:
            return json.load(fh)

    def write_settings(self, data):
        with open(os.path.join(self.claude_dir, "settings.json"), "w") as fh:
            json.dump(data, fh)

    def test_setup_writes_env_and_keeps_other_settings(self):
        self.write_settings({"model": "opus", "env": {"FOO": "bar", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://x", "CLAUDE_CODE_ENABLE_TELEMETRY": "1"}, "permissions": {"allow": ["Bash(ls)"]}})
        p = self.run_cli(["--setup", "--host", self.server.url, "--public-key", self.server.public_key, "--secret-stdin"], stdin=self.server.secret_key + "\n", AGENTGUARD_PUBLIC_KEY=None, AGENTGUARD_SECRET_KEY=None)
        out = p.stdout.decode()
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("OK: keys work (project: test-project)", out)
        self.assertIn("duplicate traces", out)  # warned, not removed without --remove-otel
        self.assertNotIn(self.server.secret_key, out)
        s = self.settings()
        self.assertEqual(s["model"], "opus")
        self.assertEqual(s["permissions"], {"allow": ["Bash(ls)"]})
        self.assertEqual(s["env"]["FOO"], "bar")
        self.assertEqual(s["env"]["AGENTGUARD_HOST"], self.server.url)
        self.assertEqual(s["env"]["AGENTGUARD_PUBLIC_KEY"], self.server.public_key)
        self.assertEqual(s["env"]["AGENTGUARD_SECRET_KEY"], self.server.secret_key)
        self.assertIn("OTEL_EXPORTER_OTLP_ENDPOINT", s["env"])
        self.assertTrue(os.path.exists(os.path.join(self.claude_dir, "settings.json.agentguard-backup")))

    def test_secret_on_stdin_with_bom_and_crlf(self):
        # Windows PowerShell 5.1 pipes text with a UTF-8 BOM and CRLF line endings
        p = subprocess.run([sys.executable, SCRIPT, "--setup", "--host", self.server.url, "--public-key", self.server.public_key, "--secret-stdin"], input=b"\xef\xbb\xbf" + self.server.secret_key.encode() + b"\r\n", env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(b"OK: keys work", p.stdout)
        self.assertEqual(self.settings()["env"]["AGENTGUARD_SECRET_KEY"], self.server.secret_key)

    def test_setup_removes_otel_when_asked(self):
        self.write_settings({"env": {"FOO": "bar", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://x", "OTEL_EXPORTER_OTLP_HEADERS": "a=b", "CLAUDE_CODE_ENABLE_TELEMETRY": "1"}})
        p = self.run_cli(["--setup", "--host", self.server.url, "--public-key", self.server.public_key, "--secret-stdin", "--remove-otel"], stdin=self.server.secret_key + "\n")
        self.assertEqual(p.returncode, 0, p.stdout)
        env = self.settings()["env"]
        self.assertEqual(sorted(env), ["AGENTGUARD_HOST", "AGENTGUARD_PUBLIC_KEY", "AGENTGUARD_SECRET_KEY", "FOO"])

    def test_setup_with_wrong_keys_writes_nothing(self):
        self.write_settings({"env": {"FOO": "bar"}})
        p = self.run_cli(["--setup", "--host", self.server.url, "--public-key", self.server.public_key, "--secret-stdin"], stdin="sk-lf-wrong-key-123456\n")
        self.assertEqual(p.returncode, 3)
        self.assertIn("refused the keys (HTTP 401)", p.stdout.decode())
        self.assertNotIn(b"sk-lf-wrong-key-123456", p.stdout)
        self.assertEqual(self.settings(), {"env": {"FOO": "bar"}})

    def test_setup_unreachable_host(self):
        p = self.run_cli(["--setup", "--host", "http://127.0.0.1:%d" % free_port(), "--public-key", "pk", "--secret-stdin"], stdin="sk\n")
        self.assertEqual(p.returncode, 3)
        self.assertIn("could not reach", p.stdout.decode())
        self.assertFalse(os.path.exists(os.path.join(self.claude_dir, "settings.json")))

    def test_setup_never_overwrites_broken_settings(self):
        path = os.path.join(self.claude_dir, "settings.json")
        with open(path, "w") as fh:
            fh.write("{ not json")
        p = self.run_cli(["--setup", "--host", self.server.url, "--public-key", self.server.public_key, "--secret-stdin"], stdin=self.server.secret_key + "\n")
        self.assertEqual(p.returncode, 4)
        with open(path) as fh:
            self.assertEqual(fh.read(), "{ not json")

    def test_check_otel(self):
        self.write_settings({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://x", "FOO": "1"}})
        p = self.run_cli(["--check-otel"], OTEL_EXPORTER_OTLP_PROTOCOL="grpc")
        data = json.loads(p.stdout)
        self.assertEqual(data["in_settings_env"], ["OTEL_EXPORTER_OTLP_ENDPOINT"])
        self.assertEqual(data["elsewhere_in_environment"], ["OTEL_EXPORTER_OTLP_PROTOCOL"])

    def test_status_masks_keys(self):
        self.run_hook(AGENTGUARD_SECRET_KEY="sk-lf-wrong-key-123456")
        p = self.run_cli(["--status"])
        out = p.stdout.decode()
        self.assertEqual(p.returncode, 0)
        self.assertNotIn(self.server.secret_key, out)
        self.assertNotIn(self.server.public_key, out)
        self.assertIn("Secret key: set (ends ...0000)", out)
        self.assertIn("Last send:", out)
        self.assertIn("FAILED (HTTP 401)", out)
        self.assertIn("error log line", out)

    def test_status_when_not_set_up(self):
        p = self.run_cli(["--status"], AGENTGUARD_PUBLIC_KEY=None, AGENTGUARD_SECRET_KEY=None)
        out = p.stdout.decode()
        self.assertIn("Keys are not set", out)
        self.assertIn("Last send: never", out)


if __name__ == "__main__":
    unittest.main()
