"""Golden test for the Claude Code adapter: the complete ingestion payload for the fixture
transcript (with content logging off and on) must stay exactly the same across refactors.

The golden files were written by the code as it was before the shared core existed (0.1.1).
To rewrite them on purpose, after checking the diff:

    python3 tests/test_golden.py --write
"""

import hashlib
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_send_trace import Base  # noqa: E402

GOLDEN_DIR = os.path.join(HERE, "fixtures", "golden")
CASES = {
    "claude_payload_content_off.json": {},
    "claude_payload_content_on.json": {"AGENTGUARD_LOG_CONTENT": "true", "AGENTGUARD_MAX_CONTENT_CHARS": "120"},
}


def envelope_id(event_type, body):
    """The documented envelope id: a hash of the event type and the canonical body."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(("%s:%s" % (event_type, canonical)).encode("utf-8")).hexdigest()[:32]


def normalize(events):
    """Sorted, version-independent form of everything sent. Envelope timestamps are the send
    time, so they are dropped; the plugin version is replaced by a placeholder; the envelope
    id is checked against its body and then left out (it hashes the version too)."""
    out = []
    for ev in events:
        assert ev["id"] == envelope_id(ev["type"], ev["body"]), "envelope id is not the hash of its body"
        body = json.loads(json.dumps(ev["body"]))
        md = body.get("metadata")
        if isinstance(md, dict) and "plugin_version" in md:
            md["plugin_version"] = "<plugin_version>"
        out.append({"type": ev["type"], "body": body})
    out.sort(key=lambda e: (e["type"], e["body"]["id"]))
    return json.dumps(out, sort_keys=True, indent=1, ensure_ascii=False) + "\n"


class GoldenPayloadTests(Base):
    def payload_for(self, env):
        proc, _ = self.run_hook(**env)
        self.assertQuietSuccess(proc)
        events = self.server.events()
        keys = [(e["type"], e["body"]["id"]) for e in events]
        self.assertEqual(len(keys), len(set(keys)), "an event was sent twice")
        return normalize(events)

    def test_payloads_match_golden_files(self):
        for name, env in sorted(CASES.items()):
            with self.subTest(golden=name):
                self.server.requests.clear()
                import shutil
                shutil.rmtree(self.home, ignore_errors=True)
                actual = self.payload_for(env)
                with open(os.path.join(GOLDEN_DIR, name), encoding="utf-8") as fh:
                    expected = fh.read()
                self.assertEqual(actual, expected, "the Claude payload changed: %s" % name)


def write_golden():
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    for name, env in sorted(CASES.items()):
        t = GoldenPayloadTests("test_payloads_match_golden_files")
        t.setUp()
        try:
            text = t.payload_for(env)
        finally:
            t.tearDown()
        with open(os.path.join(GOLDEN_DIR, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print("wrote", name, len(json.loads(text)), "events")


if __name__ == "__main__":
    if "--write" in sys.argv:
        write_golden()
    else:
        unittest.main()
