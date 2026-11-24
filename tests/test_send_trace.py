"""Tests for plugins/agentguard/scripts/send_trace.py (standard library unittest).

    python3 -m unittest discover -s tests -v
"""

import base64
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
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN = os.path.join(ROOT, "plugins", "agentguard")
SCRIPT = os.path.join(PLUGIN, "scripts", "send_trace.py")
FIXTURES = os.path.join(HERE, "fixtures")
SESSION = "11111111-2222-3333-4444-555555555555"

sys.path.insert(0, HERE)
from fake_server import FakeAgentGuard  # noqa: E402


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

    def payload(self, event="Stop", transcript=None):
        return json.dumps({"session_id": SESSION, "transcript_path": transcript or self.transcript, "cwd": self.project, "hook_event_name": event, "stop_hook_active": False})

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
        procs = [subprocess.Popen([sys.executable, SCRIPT], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env()) for _ in range(4)]
        for p in procs:
            p.stdin.write(self.payload().encode())
            p.stdin.close()
        for p in procs:
            p.wait(timeout=60)
            self.assertEqual(p.returncode, 0)
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

    def test_server_down(self):
        proc, elapsed = self.run_hook(AGENTGUARD_HOST="http://127.0.0.1:%d" % free_port())
        self.assertQuietSuccess(proc)
        self.assertEqual(self.offset(), 0)
        self.assertFalse(self.state()["last_send"]["ok"])
        self.assertLess(elapsed, 5)

    def test_timeout_is_three_seconds(self):
        self.server.delay = 6
        proc, elapsed = self.run_hook()
        self.assertQuietSuccess(proc)
        self.assertLess(elapsed, 5.0)
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


class DetachTests(Base):
    server_delay = 0.7  # slower than the hook may take, well inside the 3 s budget

    def test_hook_returns_immediately_and_worker_sends(self):
        proc, elapsed = self.run_hook(AGENTGUARD_SYNC=None)
        self.assertQuietSuccess(proc)
        self.assertLess(elapsed, 1.0)
        deadline = time.time() + 15
        while time.time() < deadline and self.offset("agent-afixture01") == 0:
            time.sleep(0.2)
        self.assertGreater(self.offset(), 0)
        self.assertEqual(len(self.by_type("generation-create")), 7)


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
        self.assertEqual(market["name"], "actaclad")
        entry = market["plugins"][0]
        self.assertEqual(entry["name"], "agentguard")
        self.assertEqual(entry["source"], "./plugins/agentguard")
        self.assertEqual(entry["version"], mod.PLUGIN_VERSION)

    def test_hooks(self):
        with open(os.path.join(PLUGIN, "hooks", "hooks.json")) as fh:
            hooks = json.load(fh)["hooks"]
        self.assertEqual(sorted(hooks), ["SessionEnd", "Stop"])
        for event in ("Stop", "SessionEnd"):
            h = hooks[event][0]["hooks"][0]
            self.assertEqual(h["type"], "command")
            self.assertEqual(h["command"], "python3")
            self.assertEqual(h["args"], ["${CLAUDE_PLUGIN_ROOT}/scripts/send_trace.py"])

    def test_python38_syntax(self):
        import ast
        for path in (SCRIPT,):
            with open(path) as fh:
                ast.parse(fh.read(), feature_version=(3, 8))


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
