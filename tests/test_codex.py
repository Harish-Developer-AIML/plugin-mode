"""Tests for the Codex adapter (plugins/agentguard/scripts/codex_trace.py).

    python3 -m unittest tests.test_codex -v
"""

import gc
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "plugins", "agentguard", "scripts")
SCRIPT = os.path.join(SCRIPTS, "codex_trace.py")
FIXTURE_HOME = os.path.join(HERE, "fixtures", "codex_home")
sys.path.insert(0, HERE)
from fake_server import FakeAgentGuard  # noqa: E402

MAIN = "019a0000-0000-7000-8000-00000000aaaa"
GUARD = "019a0000-0000-7000-8000-00000000bbbb"
OTHER = "019a0000-0000-7000-8000-00000000cccc"
HELPER = "019a0000-0000-7000-8000-00000000dddd"
T1, T2 = "019a0000-0000-7000-8000-0000000000t1", "019a0000-0000-7000-8000-0000000000t2"
DAY = os.path.join("sessions", "2026", "11", "20")
MAIN_FILE = "rollout-2026-11-20T10-00-00-%s.jsonl" % MAIN
GUARD_FILE = "rollout-2026-11-20T10-00-05-%s.jsonl" % GUARD

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
    spec = importlib.util.spec_from_file_location("codex_trace_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def payload(thread_id=MAIN, turn_id=T1, client="VS Code"):
    return json.dumps({"type": "agent-turn-complete", "thread-id": thread_id, "turn-id": turn_id, "cwd": "/home/dev/example-project",
                       "client": client, "input-messages": ["PROMPT-ONE"], "last-assistant-message": "FINAL-ONE"})


class CodexBase(unittest.TestCase):
    server_status = 207

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agentguard-codex-")
        self.codex_home = os.path.join(self.tmp, "codex_home")
        shutil.copytree(FIXTURE_HOME, self.codex_home)
        self.ag_home = os.path.join(self.tmp, "agentguard")
        self.server = FakeAgentGuard(status=self.server_status).start()
        self.mod = load_module()
        self.det = lambda *p: self.mod.core.det_id("codex", MAIN, *p)

    def tearDown(self):
        self.server.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENTGUARD_", "CODEX_"))}
        env.update({"AGENTGUARD_HOST": self.server.url, "AGENTGUARD_PUBLIC_KEY": self.server.public_key, "AGENTGUARD_SECRET_KEY": self.server.secret_key,
                    "AGENTGUARD_HOME": self.ag_home, "AGENTGUARD_USER": "dev@example.com", "AGENTGUARD_SYNC": "1", "CODEX_HOME": self.codex_home})
        for k, v in extra.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        return env

    def notify(self, data=None, args=None, timeout=60, **env):
        argv = [sys.executable, SCRIPT] + (args if args is not None else ["notify", data or payload()])
        start = time.monotonic()
        p = subprocess.run(argv, env=self.env(**env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return p, time.monotonic() - start

    def assertQuiet(self, p):
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))

    def bodies(self, event_type):
        out = {}
        for ev in self.server.events():
            if ev["type"] == event_type:
                out[ev["body"]["id"]] = ev["body"]
        return out

    def state(self):
        with open(os.path.join(self.ag_home, "state", "codex.json")) as fh:
            return json.load(fh)

    def error_log(self):
        try:
            with open(os.path.join(self.ag_home, "logs", "codex.log")) as fh:
                return fh.read()
        except OSError:
            return ""

    def rollout(self, name=MAIN_FILE):
        return os.path.join(self.codex_home, DAY, name)


class CodexMappingTests(CodexBase):
    def setUp(self):
        super().setUp()
        p, _ = self.notify()
        self.assertQuiet(p)
        self.traces = self.bodies("trace-create")
        self.gens = self.bodies("generation-create")
        self.spans = self.bodies("span-create")

    def test_counts(self):
        self.assertEqual(sorted(self.traces), sorted([self.det("trace", T1), self.det("trace", T2)]))
        self.assertEqual(len(self.gens), 5)  # R1-R4 and the guardian's RG1
        self.assertEqual(len(self.spans), 3)  # two tool calls and the guardian run

    def test_trace_fields(self):
        t = self.traces[self.det("trace", T1)]
        self.assertEqual(t["name"], "codex.turn")
        self.assertEqual(t["sessionId"], "codex:" + MAIN)
        self.assertEqual(t["tags"], ["codex"])
        self.assertEqual(t["userId"], "dev@example.com")
        self.assertEqual(t["timestamp"], "2026-11-20T10:00:00.010Z")
        md = t["metadata"]
        self.assertEqual(md["agent"], "codex")
        self.assertEqual(md["model"], "gpt-6.1-sol")
        self.assertEqual(md["effort"], "high")
        self.assertEqual(md["status"], "completed")
        self.assertEqual(md["duration_ms"], 11990)
        self.assertEqual(md["time_to_first_token_ms"], 800)
        self.assertEqual(md["git_branch"], "feature/demo")
        self.assertEqual(md["codex_version"], "0.171.0-alpha.13.2")
        self.assertEqual(md["originator"], "codex_vscode")
        self.assertEqual((md["commands"], md["commands_failed"], md["file_changes"]), (1, 1, 1))
        self.assertEqual(md["prompt_chars"], len("PROMPT-ONE: fix the failing test"))
        t2 = self.traces[self.det("trace", T2)]
        self.assertEqual(t2["metadata"]["status"], "aborted")
        self.assertEqual(t2["metadata"]["abort_reason"], "interrupted")

    def test_usage_subtracts_cached_and_reasoning(self):
        g1 = self.gens[self.det("gen", "resp_R1")]
        self.assertEqual(g1["usageDetails"], {"input": 7783 - 4864, "input_cached_tokens": 4864, "output": 138 - 88, "output_reasoning_tokens": 88})
        g2 = self.gens[self.det("gen", "resp_R2")]
        self.assertEqual(g2["usageDetails"], {"input": 9000 - 8000 - 500, "input_cached_tokens": 8000, "input_cache_write_tokens": 500, "output": 200, "output_reasoning_tokens": 100})
        g3 = self.gens[self.det("gen", "resp_R3")]
        self.assertEqual(g3["usageDetails"], {"input": 500, "input_cached_tokens": 9000, "output": 50})
        for g in self.gens.values():  # never more tokens than Codex reported
            u = g["usageDetails"]
            self.assertLessEqual(sum(u.values()), 9600 + 300)

    def test_generation_fields_and_timing(self):
        g1 = self.gens[self.det("gen", "resp_R1")]
        self.assertEqual(g1["name"], "codex.llm_call")
        self.assertEqual(g1["model"], "gpt-6.1-sol")
        self.assertEqual(g1["traceId"], self.det("trace", T1))
        self.assertEqual(g1["startTime"], "2026-11-20T10:00:00.010Z")  # turn start
        self.assertEqual(g1["endTime"], "2026-11-20T10:00:02.100Z")
        self.assertEqual(g1["metadata"]["content_blocks"], {"reasoning": 1, "text": 1})
        self.assertEqual(g1["metadata"]["tool_calls"], ["exec"])
        g2 = self.gens[self.det("gen", "resp_R2")]
        self.assertEqual(g2["startTime"], "2026-11-20T10:00:04.100Z")  # after the previous tool output
        self.assertEqual(g2["metadata"]["tool_calls"], ["apply_patch"])

    def test_tool_spans(self):
        c1 = self.spans[self.det("tool", "call_C1")]
        self.assertEqual(c1["name"], "exec")
        self.assertEqual(c1["traceId"], self.det("trace", T1))
        self.assertEqual(c1["parentObservationId"], self.det("gen", "resp_R1"))
        self.assertEqual((c1["startTime"], c1["endTime"]), ("2026-11-20T10:00:02.000Z", "2026-11-20T10:00:04.100Z"))
        self.assertEqual(c1["metadata"]["tool_kind"], "custom_tool_call")
        c2 = self.spans[self.det("tool", "call_C2")]
        self.assertEqual(c2["name"], "apply_patch")
        self.assertEqual(c2["parentObservationId"], self.det("gen", "resp_R2"))

    def test_guardian_nested_under_parent_turn(self):
        span_id = self.det("subagent", GUARD, "turn_G1")
        span = self.spans[span_id]
        self.assertEqual(span["name"], "codex.subagent")
        self.assertEqual(span["traceId"], self.det("trace", T1))
        self.assertEqual(span["metadata"]["agent_type"], "guardian")
        self.assertEqual(span["endTime"], "2026-11-20T10:00:06.050Z")
        g = self.gens[self.det("gen", "resp_RG1")]
        self.assertEqual(g["model"], "codex-auto-review")
        self.assertEqual(g["traceId"], self.det("trace", T1))
        self.assertEqual(g["parentObservationId"], span_id)
        self.assertTrue(g["metadata"]["sidechain"])
        self.assertEqual(g["usageDetails"], {"input": 3000, "output": 10, "output_reasoning_tokens": 10})

    def test_other_session_in_same_folder_is_not_sent(self):
        raw = b"".join(r["raw"] for r in self.server.requests)
        self.assertNotIn(b"resp_RO1", raw)
        self.assertNotIn(OTHER.encode(), raw)

    def test_no_content_by_default(self):
        raw = b"".join(r["raw"] for r in self.server.requests)
        for marker in (b"PROMPT-ONE", b"RESPONSE-ONE", b"FINAL-ONE", b"TOOL-OUTPUT", b"SCRIPT-INPUT", b"PATCH-TEXT", b"ENCRYPTED-REASONING",
                       b"REVIEW-", b"BASE-INSTRUCTIONS", b"sk-ag-"):
            self.assertNotIn(marker, raw)
        for body in list(self.traces.values()) + list(self.gens.values()) + list(self.spans.values()):
            self.assertNotIn("input", body)
            self.assertNotIn("output", body)

    def test_rerun_sends_nothing_and_ids_are_stable(self):
        before = len(self.server.requests)
        p, _ = self.notify()
        self.assertQuiet(p)
        self.assertEqual(len(self.server.requests), before)
        ids = [(e["type"], e["body"]["id"]) for e in self.server.events()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_last_send_recorded(self):
        last = self.state()["last_send"]
        self.assertTrue(last["ok"])
        self.assertEqual(last["client"], "VS Code")


class CodexContentTests(CodexBase):
    def test_content_on_with_redaction_and_truncation(self):
        p, _ = self.notify(AGENTGUARD_LOG_CONTENT="true", AGENTGUARD_MAX_CONTENT_CHARS="100")
        self.assertQuiet(p)
        t1 = self.bodies("trace-create")[self.det("trace", T1)]
        self.assertEqual(t1["input"], "PROMPT-ONE: fix the failing test")  # the typed prompt, not the injected context
        self.assertEqual(t1["output"], "FINAL-ONE: fixed the test")
        t2 = self.bodies("trace-create")[self.det("trace", T2)]
        self.assertEqual(t2["input"], "PROMPT-TWO: the key is [REDACTED]")
        g1 = self.bodies("generation-create")[self.det("gen", "resp_R1")]
        self.assertEqual(g1["input"], "PROMPT-ONE: fix the failing test")
        self.assertEqual(g1["output"], "RESPONSE-ONE-A: let me look")
        c1 = self.bodies("span-create")[self.det("tool", "call_C1")]
        self.assertEqual(c1["input"], "SCRIPT-INPUT: run tests")
        self.assertEqual(c1["output"], "TOOL-OUTPUT-C1: 1 failed")
        raw = b"".join(r["raw"] for r in self.server.requests)
        self.assertNotIn(b"ENCRYPTED-REASONING", raw)  # reasoning is never sent
        self.assertNotIn(b"sk-ag-3f2a9c1e", raw)


class CodexIncrementalTests(CodexBase):
    def test_append_sends_only_new_lines(self):
        path = self.rollout()
        with open(path, "rb") as fh:
            lines = fh.readlines()
        cut = next(i for i, l in enumerate(lines) if b'"task_started"' in l and b"0000t2" in l)
        with open(path, "wb") as fh:
            fh.writelines(lines[:cut])
        self.notify()
        first = {(e["type"], e["body"]["id"]) for e in self.server.events()}
        self.assertNotIn(("generation-create", self.det("gen", "resp_R4")), first)
        self.server.requests.clear()
        with open(path, "wb") as fh:
            fh.writelines(lines)
        self.notify(payload(turn_id=T2))
        second = {(e["type"], e["body"]["id"]) for e in self.server.events()}
        self.assertIn(("generation-create", self.det("gen", "resp_R4")), second)
        self.assertNotIn(("generation-create", self.det("gen", "resp_R1")), second)

    def test_rewritten_file_is_not_sent_twice(self):
        self.notify()
        sent = len(self.server.events())
        path = self.rollout()
        with open(path, "rb") as fh:
            lines = fh.readlines()
        # Codex migrates files by rewriting them: same ordinals, different bytes and length
        with open(path, "wb") as fh:
            for l in lines:
                fh.write(json.dumps(json.loads(l), indent=None, separators=(", ", ": ")).encode() + b"\n")
        self.server.requests.clear()
        p, _ = self.notify()
        self.assertQuiet(p)
        self.assertEqual(self.server.events(), [])
        self.assertGreater(sent, 0)

    def test_helper_thread_without_rollout_is_skipped(self):
        p, _ = self.notify(payload(thread_id=HELPER))
        self.assertQuiet(p)
        self.assertEqual(self.server.requests, [])

    def test_notify_for_guardian_thread_processes_parent(self):
        p, _ = self.notify(payload(thread_id=GUARD, turn_id="turn_G1"))
        self.assertQuiet(p)
        self.assertIn(self.det("gen", "resp_RG1"), self.bodies("generation-create"))
        self.assertIn(self.det("trace", T1), self.bodies("trace-create"))

    def test_partial_last_line_waits(self):
        path = self.rollout()
        with open(path, "rb") as fh:
            data = fh.read()
        with open(path, "wb") as fh:
            fh.write(data[:-30])  # the last line is cut
        self.notify()
        st = self.state()["sessions"][MAIN]["files"][MAIN_FILE]
        self.assertEqual(st["offset"], data[:-30].rfind(b"\n") + 1)


class CodexFailureTests(CodexBase):
    def test_server_down_retries_later(self):
        p, _ = self.notify(AGENTGUARD_HOST="http://127.0.0.1:9")
        self.assertQuiet(p)
        self.assertFalse(self.state()["last_send"]["ok"])
        self.assertEqual(self.state()["sessions"][MAIN]["files"][MAIN_FILE]["offset"], 0)
        p, _ = self.notify()
        self.assertEqual(len(self.bodies("generation-create")), 5)

    def test_wrong_keys(self):
        p, _ = self.notify(AGENTGUARD_SECRET_KEY="sk-ag-wrong-wrong-wrong")
        self.assertQuiet(p)
        self.assertEqual(self.state()["last_send"]["status"], 401)
        self.assertNotIn("sk-ag-wrong-wrong-wrong", self.error_log())

    def test_bad_input_always_exits_0_silently(self):
        for args in (["notify"], ["notify", "not json"], ["notify", "[]"], ["notify", '{"type":"other"}'], ["notify", '{"thread-id": "../../etc"}'], ["unknown-mode"], []):
            p, _ = self.notify(args=args)
            self.assertQuiet(p)

    def test_no_keys_is_silent(self):
        p, _ = self.notify(AGENTGUARD_PUBLIC_KEY=None, AGENTGUARD_SECRET_KEY=None)
        self.assertQuiet(p)
        self.assertEqual(self.server.requests, [])

    def test_keys_from_config_file(self):
        os.makedirs(self.ag_home)
        with open(os.path.join(self.ag_home, "config.json"), "w") as fh:
            json.dump({"host": self.server.url, "public_key": self.server.public_key, "secret_key": self.server.secret_key}, fh)
        p, _ = self.notify(AGENTGUARD_HOST=None, AGENTGUARD_PUBLIC_KEY=None, AGENTGUARD_SECRET_KEY=None)
        self.assertQuiet(p)
        self.assertEqual(len(self.bodies("generation-create")), 5)

    def test_detached_worker_returns_fast_and_sends(self):
        self.server.delay = 1.0
        p, elapsed = self.notify(AGENTGUARD_SYNC=None)
        self.assertQuiet(p)
        self.assertLess(elapsed, 1.0)
        deadline = time.time() + 20
        while time.time() < deadline and len(self.bodies("generation-create")) < 5:
            time.sleep(0.2)
        self.assertEqual(len(self.bodies("generation-create")), 5)

    def test_json_as_only_argument(self):
        p, _ = self.notify(args=[payload()])
        self.assertQuiet(p)
        self.assertEqual(len(self.bodies("generation-create")), 5)


@unittest.skipIf(os.name == "nt", "the chained test program is a POSIX shell script")
class CodexChainTests(CodexBase):
    def test_previous_notify_program_gets_same_argument_and_environment(self):
        marker = os.path.join(self.tmp, "chained.txt")
        prog = os.path.join(self.tmp, "old-notify.sh")
        with open(prog, "w") as fh:
            fh.write('#!/bin/sh\nprintf "%%s\\n%%s\\n%%s\\n" "$1" "$#" "$OLD_NOTIFY_VAR" > "%s"\n' % marker)
        os.chmod(prog, os.stat(prog).st_mode | stat.S_IXUSR)
        os.makedirs(self.ag_home)
        with open(os.path.join(self.ag_home, "config.json"), "w") as fh:
            json.dump({"codex": {"chain_notify": [prog]}}, fh)
        data = payload()
        p, _ = self.notify(data, OLD_NOTIFY_VAR="kept")
        self.assertQuiet(p)
        deadline = time.time() + 10
        while time.time() < deadline and not os.path.exists(marker):
            time.sleep(0.1)
        time.sleep(0.2)
        with open(marker) as fh:
            arg, argc, var = fh.read().splitlines()
        self.assertEqual((arg, argc, var), (data, "1", "kept"))
        self.assertEqual(len(self.bodies("generation-create")), 5)

    def test_chain_runs_even_without_keys(self):
        marker = os.path.join(self.tmp, "chained.txt")
        prog = os.path.join(self.tmp, "old-notify.sh")
        with open(prog, "w") as fh:
            fh.write('#!/bin/sh\necho ran > "%s"\n' % marker)
        os.chmod(prog, 0o755)
        os.makedirs(self.ag_home)
        with open(os.path.join(self.ag_home, "config.json"), "w") as fh:
            json.dump({"codex": {"chain_notify": [prog]}}, fh)
        self.notify(AGENTGUARD_PUBLIC_KEY=None, AGENTGUARD_SECRET_KEY=None)
        deadline = time.time() + 10
        while time.time() < deadline and not os.path.exists(marker):
            time.sleep(0.1)
        self.assertTrue(os.path.exists(marker))


class CodexLegacyFormatTests(CodexBase):
    """A file from an older Codex: responses only as token_count events, then the switch to
    token_usage_record; and a guardian review in the next day's folder."""

    LEGACY = "019a0000-0000-7000-8000-00000000eeee"
    LEGACY_GUARD = "019a0000-0000-7000-8000-00000000ffff"

    def setUp(self):
        super().setUp()
        p, _ = self.notify(payload(thread_id=self.LEGACY, turn_id="turn_L1"))
        self.assertQuiet(p)
        self.gens = self.bodies("generation-create")
        self.ldet = lambda *parts: self.mod.core.det_id("codex", self.LEGACY, *parts)

    def test_one_generation_per_response(self):
        # two changed token_counts before the first record, then the record; the repeated
        # total, the null info and the record's own token_count add nothing
        legacy = [g for g in self.gens.values() if g["model"] == "gpt-5.6-sol"]
        self.assertEqual(len(legacy), 3)
        totals = {k: sum(g["usageDetails"].get(k, 0) for g in legacy) for k in ("input", "input_cached_tokens", "output", "output_reasoning_tokens")}
        self.assertEqual(totals, {"input": (5000 - 4000) + (6000 - 5000) + (7000 - 6000), "input_cached_tokens": 4000 + 5000 + 6000,
                                  "output": (100 - 40) + 60 + (30 - 10), "output_reasoning_tokens": 40 + 10})
        self.assertIn(self.ldet("gen", "resp_L3"), self.gens)

    def test_tool_parent_is_the_token_count_generation(self):
        span = self.bodies("span-create")[self.ldet("tool", "call_L1")]
        parent = self.gens[span["parentObservationId"]]
        self.assertEqual(parent["usageDetails"]["input"], 1000)

    def test_guardian_in_next_day_folder_is_nested(self):
        g = self.gens[self.ldet("gen", "resp_LG")]
        self.assertEqual(g["model"], "codex-auto-review")
        self.assertEqual(g["traceId"], self.ldet("trace", "turn_L1"))
        self.assertEqual(g["parentObservationId"], self.ldet("subagent", self.LEGACY_GUARD, "turn_LG"))

    def test_day_folders_are_capped(self):
        import datetime as dt
        path = os.path.join(self.codex_home, "sessions", "2026", "01", "30", "rollout-x.jsonl")
        folders = self.mod.day_folders(path, today=dt.date(2026, 3, 30))
        self.assertEqual(len(folders), self.mod.MAX_CHILD_DAYS)
        self.assertTrue(folders[0].endswith(os.path.join("2026", "01", "30")))
        self.assertTrue(folders[1].endswith(os.path.join("2026", "01", "31")))
        self.assertTrue(folders[2].endswith(os.path.join("2026", "02", "01")))


class CodexConfigTomlTests(unittest.TestCase):
    OURS = ["/home/dev/.agentguard/bin/agentguard", "notify", "codex"]

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agentguard-codex-toml-")
        self.mod = load_module()
        self.env = {"CODEX_HOME": self.tmp}
        self.path = os.path.join(self.tmp, "config.toml")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, text):
        with open(self.path, "w", newline="") as fh:
            fh.write(text)

    def read(self):
        with open(self.path, newline="") as fh:
            return fh.read()

    def configure(self):
        from unittest import mock
        with mock.patch.dict(os.environ, self.env):
            return self.mod.configure_notify(self.OURS)

    def remove(self, chain=None):
        from unittest import mock
        with mock.patch.dict(os.environ, self.env):
            return self.mod.remove_notify(chain)

    def parse(self):
        try:
            import tomllib
        except ImportError:  # Python < 3.11: check the line only
            return None
        with open(self.path, "rb") as fh:
            return tomllib.load(fh)

    def test_added_above_first_table_and_rest_untouched(self):
        original = '# my settings\nmodel = "gpt-6.1-sol"\n\n[projects."/x"]\ntrust_level = "trusted"\n'
        self.write(original)
        changed, chain, msg = self.configure()
        self.assertTrue(changed)
        self.assertIsNone(chain)
        text = self.read()
        self.assertTrue(text.startswith('notify = ["/home/dev/.agentguard/bin/agentguard", "notify", "codex"]  # AgentGuard'))
        self.assertEqual(text.split("\n", 1)[1], original)
        data = self.parse()
        if data is not None:
            self.assertEqual(data["notify"], self.OURS)
            self.assertEqual(data["projects"]["/x"]["trust_level"], "trusted")
        with open(self.path + ".agentguard-backup") as fh:
            self.assertEqual(fh.read(), original)

    def test_idempotent(self):
        self.write('model = "x"\n')
        self.configure()
        first = self.read()
        changed, chain, msg = self.configure()
        self.assertFalse(changed)
        self.assertEqual(self.read(), first)
        self.assertIn("already", msg)

    def test_existing_notify_is_chained(self):
        self.write('notify = ["python3", "/home/dev/notify.py"]  # mine\nmodel = "x"\n[tui]\nnotifications = true\n')
        changed, chain, msg = self.configure()
        self.assertTrue(changed)
        self.assertEqual(chain, ["python3", "/home/dev/notify.py"])
        text = self.read()
        self.assertEqual(text.count("notify ="), 1)
        self.assertIn('model = "x"\n[tui]\nnotifications = true\n', text)
        changed, msg = self.remove(chain)
        self.assertTrue(changed)
        self.assertTrue(self.read().startswith('notify = ["python3", "/home/dev/notify.py"]\n'))

    def test_literal_and_escaped_strings(self):
        self.write("notify = ['C:\\Tools\\n.exe', \"a \\\"q\\\"\"]\n")
        changed, chain, msg = self.configure()
        self.assertEqual(chain, ["C:\\Tools\\n.exe", 'a "q"'])

    def test_multiline_notify_is_left_alone(self):
        original = 'notify = [\n  "python3",\n  "x.py",\n]\n'
        self.write(original)
        changed, chain, msg = self.configure()
        self.assertFalse(changed)
        self.assertEqual(self.read(), original)
        self.assertIn("cannot read safely", msg)

    def test_notify_inside_a_table_is_not_top_level(self):
        self.write('[tui]\nnotify = ["x"]\n')
        changed, chain, msg = self.configure()
        self.assertTrue(changed)
        self.assertIsNone(chain)
        self.assertTrue(self.read().startswith("notify = "))
        self.assertIn('[tui]\nnotify = ["x"]\n', self.read())

    def test_missing_file_is_created(self):
        changed, chain, msg = self.configure()
        self.assertTrue(changed)
        self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + ".agentguard-backup"))

    def test_crlf_kept(self):
        self.write('model = "x"\r\n[projects."/x"]\r\ntrust_level = "trusted"\r\n')
        self.configure()
        self.assertNotIn("\n", self.read().replace("\r\n", ""))

    def test_remove_without_chain_deletes_only_our_line(self):
        original = 'model = "x"\n'
        self.write(original)
        self.configure()
        changed, msg = self.remove()
        self.assertTrue(changed)
        self.assertEqual(self.read(), original)
        changed, msg = self.remove()
        self.assertFalse(changed)


class CodexInstallTests(CodexBase):
    """--setup / --uninstall, and the notify command they write, run the way Codex runs it."""

    def setup_cmd(self, secret=None, extra=(), **env):
        args = [sys.executable, SCRIPT, "--setup", "--host", self.server.url, "--public-key", self.server.public_key, "--secret-stdin"] + list(extra)
        e = self.env(AGENTGUARD_HOST=None, AGENTGUARD_PUBLIC_KEY=None, AGENTGUARD_SECRET_KEY=None, **env)
        return subprocess.run(args, input=((secret or self.server.secret_key) + "\n").encode(), env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)

    def configured_notify(self):
        with open(os.path.join(self.codex_home, "config.toml"), encoding="utf-8") as fh:
            found = self.mod.find_notify(fh.read())
        self.assertIsNotNone(found)
        return found[1]

    def test_setup_then_codex_runs_the_notify_command(self):
        p = self.setup_cmd()
        out = p.stdout.decode()
        self.assertEqual(p.returncode, 0, out + p.stderr.decode())
        self.assertIn("OK: keys work", out)
        self.assertNotIn(self.server.secret_key, out + p.stderr.decode())
        argv = self.configured_notify()
        scripts = os.path.join(self.ag_home, "scripts")
        for name in self.mod.INSTALL_FILES:
            self.assertTrue(os.path.isfile(os.path.join(scripts, name)), name)
        if os.name == "nt":
            self.assertTrue(argv[0].lower().endswith(("pyw.exe", "pythonw.exe", "python.exe")), argv)
            self.assertEqual(argv[-2:], [os.path.join(scripts, "codex_trace.py"), "notify"])
        else:
            self.assertEqual(argv, ["/bin/sh", os.path.join(scripts, "agentguard.sh"), "codex", "notify"])
        # Run it as Codex does: the configured argv plus the JSON payload, keys only in the config file
        env = self.env(AGENTGUARD_HOST=None, AGENTGUARD_PUBLIC_KEY=None, AGENTGUARD_SECRET_KEY=None)
        run = subprocess.run(argv + [payload()], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        self.assertEqual((run.returncode, run.stdout, run.stderr), (0, b"", b""))
        self.assertEqual(len(self.bodies("generation-create")), 5)

    def test_config_file_is_private_and_holds_the_keys(self):
        self.setup_cmd()
        path = os.path.join(self.ag_home, "config.json")
        with open(path) as fh:
            data = json.load(fh)
        self.assertEqual((data["host"], data["public_key"], data["secret_key"]), (self.server.url, self.server.public_key, self.server.secret_key))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_setup_twice_changes_nothing(self):
        self.setup_cmd()
        with open(os.path.join(self.codex_home, "config.toml"), "rb") as fh:
            first = fh.read()
        p = self.setup_cmd()
        self.assertEqual(p.returncode, 0)
        self.assertIn("already runs AgentGuard", p.stdout.decode())
        with open(os.path.join(self.codex_home, "config.toml"), "rb") as fh:
            self.assertEqual(fh.read(), first)

    def test_wrong_keys_change_nothing(self):
        with open(os.path.join(self.codex_home, "config.toml"), "rb") as fh:
            before = fh.read()
        p = self.setup_cmd(secret="sk-ag-wrong-wrong-wrong")
        self.assertEqual(p.returncode, 3)
        self.assertNotIn(b"sk-ag-wrong-wrong-wrong", p.stdout + p.stderr)
        with open(os.path.join(self.codex_home, "config.toml"), "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertFalse(os.path.exists(os.path.join(self.ag_home, "config.json")))

    def test_existing_notify_is_chained_and_restored(self):
        path = os.path.join(self.codex_home, "config.toml")
        with open(path, encoding="utf-8") as fh:
            original = fh.read()
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write('notify = ["my-notifier", "--sound"]\n' + original)
        p = self.setup_cmd()
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("previous notify program (my-notifier) keeps running", p.stdout.decode())
        with open(os.path.join(self.ag_home, "config.json")) as fh:
            self.assertEqual(json.load(fh)["codex"]["chain_notify"], ["my-notifier", "--sound"])
        u = subprocess.run([sys.executable, SCRIPT, "--uninstall"], env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(u.returncode, 0)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), 'notify = ["my-notifier", "--sound"]\n' + original)
        with open(os.path.join(self.ag_home, "config.json")) as fh:
            self.assertNotIn("chain_notify", json.load(fh).get("codex", {}))

    def test_status_reports_setup(self):
        self.setup_cmd()
        out = subprocess.run([sys.executable, SCRIPT, "--status"], env=self.env(AGENTGUARD_PUBLIC_KEY=None, AGENTGUARD_SECRET_KEY=None),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60).stdout.decode()
        self.assertIn("notify: AgentGuard is configured", out)
        self.assertIn("Keys: set (config file)", out)
        self.assertNotIn(self.server.secret_key, out)


class CodexWindowsNotifyTests(unittest.TestCase):
    """Which program Windows' notify gets, checked with mocks on every platform."""

    def setUp(self):
        self.mod = load_module()

    def test_pyw_launcher_preferred(self):
        from unittest import mock
        with mock.patch.object(self.mod, "windows_pyw", return_value=r"C:\Windows\pyw.exe"):
            argv = self.mod.notify_argv(r"C:\Users\dev\.agentguard\scripts", is_windows=True)
        self.assertEqual(argv[:2], [r"C:\Windows\pyw.exe", "-3"])
        self.assertTrue(argv[2].endswith("codex_trace.py"))
        self.assertEqual(argv[-1], "notify")

    def test_pythonw_next_to_python_without_launcher(self):
        from unittest import mock
        tmp = tempfile.mkdtemp(prefix="agentguard-pyw-")
        try:
            fake_python = os.path.join(tmp, "python.exe")
            open(os.path.join(tmp, "pythonw.exe"), "w").close()
            with mock.patch.object(self.mod, "windows_pyw", return_value=None), mock.patch.object(self.mod.sys, "executable", fake_python):
                argv = self.mod.notify_argv(tmp, is_windows=True)
            self.assertEqual(argv[0], os.path.join(tmp, "pythonw.exe"))
            self.assertEqual(argv[-1], "notify")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_posix_uses_the_sh_launcher(self):
        scripts = "/home/dev/.agentguard/scripts"
        argv = self.mod.notify_argv(scripts, is_windows=False)
        # os.path.join, as the code: this case only runs for real on macOS and Linux
        self.assertEqual(argv, ["/bin/sh", os.path.join(scripts, "agentguard.sh"), "codex", "notify"])


@unittest.skipIf(os.name == "nt" or not shutil.which("sh"), "POSIX launcher")
class CodexLauncherTests(CodexBase):
    def test_launcher_dispatches_to_codex(self):
        sh = os.path.join(SCRIPTS, "agentguard.sh")
        p = subprocess.run(["/bin/sh", sh, "codex", "notify", payload()], env=self.env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))
        self.assertEqual(len(self.bodies("generation-create")), 5)
        s = subprocess.run(["/bin/sh", sh, "codex", "--status"], env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertIn(b"Codex: rollout files in", s.stdout)

    def test_launcher_without_python_is_silent_for_notify(self):
        empty = os.path.join(self.tmp, "nopython")
        os.makedirs(empty)
        env = self.env()
        env["PATH"] = empty
        sh = os.path.join(SCRIPTS, "agentguard.sh")
        p = subprocess.run(["/bin/sh", sh, "codex", "notify", payload()], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))
        s = subprocess.run(["/bin/sh", sh, "codex", "--status"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertIn(b"AgentGuard needs Python 3.8 or newer", s.stdout)


class CodexPowerShellLauncherTests(CodexBase):
    def test_ps1_dispatches_codex_status(self):
        shells = [s for s in (shutil.which("pwsh"), shutil.which("powershell")) if s]
        if not shells:
            self.skipTest("PowerShell is not installed here (runs in CI)")
        for exe in shells:
            with self.subTest(shell=exe):
                p = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", os.path.join(SCRIPTS, "agentguard.ps1"), "codex", "--status"],
                                   env=self.env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertIn(b"Codex: rollout files in", p.stdout)


class CodexUsageTests(unittest.TestCase):
    def test_mapping(self):
        mod = load_module()
        u = {"input_tokens": 100, "cached_input_tokens": 60, "cache_write_input_tokens": 10, "output_tokens": 30, "reasoning_output_tokens": 12}
        self.assertEqual(mod.codex_usage_details(u), {"input": 30, "input_cached_tokens": 60, "input_cache_write_tokens": 10, "output": 18, "output_reasoning_tokens": 12})
        self.assertEqual(mod.codex_usage_details({"input_tokens": 5, "output_tokens": 1}), {"input": 5, "output": 1})
        self.assertEqual(mod.codex_usage_details({"input_tokens": 5, "cached_input_tokens": 9, "output_tokens": "x"}), {"input": 0, "input_cached_tokens": 9, "output": 0})
        self.assertEqual(mod.codex_usage_details(None), {})

    def test_keys_come_from_one_constant(self):
        mod = load_module()
        mod.CODEX_USAGE_KEYS = dict(mod.CODEX_USAGE_KEYS, cached_input="input_cache_read", reasoning=None)
        self.assertEqual(mod.codex_usage_details({"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 5, "reasoning_output_tokens": 2}),
                         {"input": 6, "input_cache_read": 4, "output": 3})


if __name__ == "__main__":
    unittest.main()
