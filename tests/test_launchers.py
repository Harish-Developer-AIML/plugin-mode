"""Tests for the launchers (scripts/agentguard.sh, scripts/agentguard.ps1) and for the hook
command in hooks.json, run through every shell Claude Code may use for it.

Shells that are not installed are skipped: locally that is usually PowerShell; in CI, Linux
and macOS have sh, bash and pwsh, and Windows has Git Bash, pwsh and Windows PowerShell 5.1.
"""

import gc
import json
import os
import shutil
import stat
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_send_trace import PLUGIN, SESSION, Base  # noqa: E402

SCRIPTS = os.path.join(PLUGIN, "scripts")
SH_LAUNCHER = os.path.join(SCRIPTS, "agentguard.sh")
PS_LAUNCHER = os.path.join(SCRIPTS, "agentguard.ps1")
IS_WINDOWS = os.name == "nt"
NO_PYTHON_MESSAGE = "AgentGuard needs Python 3.8 or newer, and none was found."

with open(os.path.join(PLUGIN, "hooks", "hooks.json")) as _fh:
    HOOK_COMMAND = json.load(_fh)["hooks"]["Stop"][0]["hooks"][0]["command"]

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


def find_posix_shells():
    """(label, argv prefix) for each POSIX shell present. Windows: Git Bash only."""
    shells = []
    if IS_WINDOWS:
        for path in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\sh.exe"):
            if os.path.exists(path):
                shells.append(("git-bash:" + os.path.basename(path), [path, "-c"]))
        return shells
    for name in ("sh", "dash", "bash", "zsh"):
        path = shutil.which(name)
        if path:
            shells.append((name, [path, "-c"]))
    return shells


def find_powershells():
    shells = []
    for name in ("pwsh", "powershell"):
        path = shutil.which(name)
        if path:
            shells.append((name, path))
    return shells


def posix_sh():
    if IS_WINDOWS:
        path = r"C:\Program Files\Git\bin\sh.exe"
        return path if os.path.exists(path) else None
    return shutil.which("sh")


class ShimDir(object):
    """A directory of fake python3 / python / py programs (POSIX shell scripts). Each one
    appends its name and arguments to a log, then acts as told."""

    def __init__(self, root, subdir="bin"):
        self.dir = os.path.join(root, subdir)
        os.makedirs(self.dir, exist_ok=True)
        self.log = os.path.join(root, "calls.log")

    def add(self, name, kind):
        real = sys.executable
        body = {
            "good": 'exec "%s" "$@"' % real,
            "old": "exit 1",  # fails the version check, like Python 2 or 3.7
            "py": '[ "$1" = "-3" ] || exit 2\nshift\nexec "%s" "$@"' % real,
            "crash": "exit 3",
        }[kind]
        path = os.path.join(self.dir, name)
        with open(path, "w") as fh:
            fh.write('#!/bin/sh\necho "%s $*" >> "%s"\n%s\n' % (name, self.log, body))
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def raw_calls(self):
        try:
            with open(self.log) as fh:
                return fh.read().splitlines()
        except OSError:
            return []


@unittest.skipIf(IS_WINDOWS or not posix_sh(), "fake-python tests need a POSIX sh (they run in CI on Linux and macOS)")
class ShLauncherTests(Base):
    def setUp(self):
        super().setUp()
        self.shims = ShimDir(self.tmp)

    def run_launcher(self, args, stdin="", path=None, **env):
        e = self.env(**env)
        e["PATH"] = path if path is not None else self.shims.dir
        return subprocess.run([posix_sh(), SH_LAUNCHER] + args, input=stdin.encode(), env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)

    def test_python3_first(self):
        self.shims.add("python3", "good")
        self.shims.add("python", "good")
        p = self.run_launcher(["--status"])
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn(b"AgentGuard plugin", p.stdout)
        self.assertTrue(all(c.startswith("python3") for c in self.shims.raw_calls()), self.shims.raw_calls())

    def test_too_old_python3_falls_back_to_python(self):
        self.shims.add("python3", "old")
        self.shims.add("python", "good")
        p = self.run_launcher(["--status"])
        self.assertIn(b"AgentGuard plugin", p.stdout)
        calls = self.shims.raw_calls()
        self.assertTrue(calls[0].startswith("python3 -c"))
        self.assertTrue(calls[-1].startswith("python ") and "send_trace.py --status" in calls[-1], calls)

    def test_py_launcher_with_dash_3(self):
        self.shims.add("py", "py")
        p = self.run_launcher(["--status"])
        self.assertIn(b"AgentGuard plugin", p.stdout)
        self.assertTrue(self.shims.raw_calls()[-1].startswith("py -3 "), self.shims.raw_calls())

    def test_store_stub_is_never_run(self):
        store = ShimDir(self.tmp, os.path.join("Local", "Microsoft", "WindowsApps"))
        store.log = self.shims.log
        store.add("python3", "crash")  # a stub: running it would "open the Store"
        self.shims.add("python", "good")
        p = self.run_launcher(["--status"], path=store.dir + os.pathsep + self.shims.dir)
        self.assertIn(b"AgentGuard plugin", p.stdout)
        self.assertFalse([c for c in self.shims.raw_calls() if c.startswith("python3")], self.shims.raw_calls())

    def test_store_python_that_is_installed_is_used(self):
        store = ShimDir(self.tmp, os.path.join("Local", "Microsoft", "WindowsApps"))
        store.log = self.shims.log
        os.makedirs(os.path.join(store.dir, "PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0"))
        store.add("python3", "good")
        p = self.run_launcher(["--status"], path=store.dir)
        self.assertIn(b"AgentGuard plugin", p.stdout)
        self.assertTrue(self.shims.raw_calls()[-1].startswith("python3 "))

    def test_no_python_hook_mode_is_silent(self):
        p = self.run_launcher([], stdin=self.payload())
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))

    def test_no_python_status_explains(self):
        self.shims.add("python3", "old")
        p = self.run_launcher(["--status"])
        self.assertEqual(p.returncode, 0)
        out = p.stdout.decode()
        self.assertIn(NO_PYTHON_MESSAGE, out)
        for line in ("brew install python3", "sudo apt install python3", "winget install Python.Python.3.12", "Add python.exe to PATH"):
            self.assertIn(line, out)

    def test_no_python_setup_fails_with_message(self):
        p = self.run_launcher(["--setup"])
        self.assertEqual(p.returncode, 1)
        self.assertIn(NO_PYTHON_MESSAGE, p.stdout.decode())

    def test_hook_mode_passes_stdin_and_sends(self):
        self.shims.add("python3", "good")
        p = self.run_launcher([], stdin=self.payload())
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))
        self.assertEqual(len(self.by_type("generation-create")), 7)

    def test_hook_mode_exits_0_when_python_crashes(self):
        self.shims.add("python3", "good")
        bad = os.path.join(self.tmp, "bad-plugin")
        os.makedirs(bad)
        shutil.copy(SH_LAUNCHER, bad)
        with open(os.path.join(bad, "send_trace.py"), "w") as fh:
            fh.write("raise SystemExit(5)\n")
        e = self.env()
        e["PATH"] = self.shims.dir
        p = subprocess.run([posix_sh(), os.path.join(bad, "agentguard.sh")], input=b"{}", env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))

    def test_secret_stdin_reaches_setup(self):
        self.shims.add("python3", "good")
        p = self.run_launcher(["--setup", "--host", self.server.url, "--public-key", self.server.public_key, "--secret-stdin"], stdin=self.server.secret_key + "\n")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn(b"OK: keys work", p.stdout)


@unittest.skipIf(IS_WINDOWS or not shutil.which("pwsh"), "fake-python PowerShell tests need pwsh on Linux or macOS (CI)")
class PsLauncherShimTests(Base):
    """The same rules for agentguard.ps1, with the same fake programs (pwsh on Linux/macOS)."""

    def setUp(self):
        super().setUp()
        self.shims = ShimDir(self.tmp)

    def run_ps(self, args, stdin="", path=None):
        e = self.env()
        e["PATH"] = path if path is not None else self.shims.dir
        return subprocess.run([shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", PS_LAUNCHER] + args, input=stdin.encode(), env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)

    def test_order_and_fallback(self):
        self.shims.add("python3", "old")
        self.shims.add("python", "good")
        p = self.run_ps(["--status"])
        self.assertIn(b"AgentGuard plugin", p.stdout, p.stdout + p.stderr)
        self.assertTrue(self.shims.raw_calls()[-1].startswith("python "), self.shims.raw_calls())

    def test_py_launcher(self):
        self.shims.add("py", "py")
        p = self.run_ps(["--status"])
        self.assertIn(b"AgentGuard plugin", p.stdout, p.stdout + p.stderr)
        self.assertTrue(self.shims.raw_calls()[-1].startswith("py -3 "))

    def test_store_stub_is_never_run(self):
        store = ShimDir(self.tmp, os.path.join("Local", "Microsoft", "WindowsApps"))
        store.log = self.shims.log
        store.add("python3", "crash")
        self.shims.add("python", "good")
        p = self.run_ps(["--status"], path=store.dir + os.pathsep + self.shims.dir)
        self.assertIn(b"AgentGuard plugin", p.stdout, p.stdout + p.stderr)
        self.assertFalse([c for c in self.shims.raw_calls() if c.startswith("python3")])

    def test_no_python(self):
        p = self.run_ps(["--status"])
        self.assertEqual(p.returncode, 0)
        self.assertIn(NO_PYTHON_MESSAGE, p.stdout.decode())
        p = self.run_ps([], stdin=self.payload())
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))


class HookCommandTests(Base):
    """The exact command from hooks.json, run the way Claude Code runs a shell-form hook:
    sh -c on macOS/Linux, Git Bash on Windows, PowerShell on Windows without Git Bash."""

    def hook_env(self, path=None, **extra):
        e = self.env(**extra)
        e["CLAUDE_PLUGIN_ROOT"] = PLUGIN
        if path is not None:
            e["PATH"] = path
        return e

    def check_sends(self, argv, label):
        self.server.requests.clear()
        shutil.rmtree(self.home, ignore_errors=True)
        p = subprocess.run(argv, input=self.payload().encode(), env=self.hook_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""), label)
        self.assertEqual(len(self.by_type("generation-create")), 7, label)

    def test_posix_shells(self):
        shells = find_posix_shells()
        if not shells:
            self.skipTest("no POSIX shell here")
        for label, prefix in shells:
            with self.subTest(shell=label):
                self.check_sends(prefix + [HOOK_COMMAND], label)

    def test_powershells(self):
        shells = find_powershells()
        if not shells:
            self.skipTest("no PowerShell here (runs in CI)")
        for label, exe in shells:
            with self.subTest(shell=label):
                self.check_sends([exe, "-NoProfile", "-NonInteractive", "-Command", HOOK_COMMAND], label)

    def test_without_python_every_shell_is_silent(self):
        empty = os.path.join(self.tmp, "empty-path")
        os.makedirs(empty)
        ran = 0
        for label, prefix in find_posix_shells():
            # keep sh itself reachable, but no python
            link_dir = os.path.join(self.tmp, "only-sh-" + label.replace(":", "_"))
            os.makedirs(link_dir, exist_ok=True)
            if not IS_WINDOWS:
                os.symlink(shutil.which("sh"), os.path.join(link_dir, "sh"))
                path = link_dir
            else:
                path = os.path.dirname(prefix[0])  # Git's bin folder has sh but no python
            with self.subTest(shell=label):
                p = subprocess.run(prefix + [HOOK_COMMAND], input=self.payload().encode(), env=self.hook_env(path=path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
                self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""), label)
                ran += 1
        for label, exe in find_powershells():
            with self.subTest(shell=label):
                p = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", HOOK_COMMAND], input=self.payload().encode(), env=self.hook_env(path=empty), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
                self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""), label)
                ran += 1
        if not ran:
            self.skipTest("no shell here")
        self.assertEqual(self.server.requests, [])

    def test_non_ascii_paths(self):
        """Hook input with a non-ASCII transcript path, through each available shell."""
        odd = os.path.join(self.tmp, "projets-é-日本")
        shutil.copytree(self.project, odd)
        payload = self.payload(transcript=os.path.join(odd, SESSION + ".jsonl"))
        shells = [(l, p + [HOOK_COMMAND]) for l, p in find_posix_shells()] + [(l, [e, "-NoProfile", "-NonInteractive", "-Command", HOOK_COMMAND]) for l, e in find_powershells()]
        for label, argv in shells:
            with self.subTest(shell=label):
                self.server.requests.clear()
                shutil.rmtree(self.home, ignore_errors=True)
                p = subprocess.run(argv, input=payload.encode("utf-8"), env=self.hook_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
                self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""), label)
                self.assertEqual(len(self.by_type("generation-create")), 7, label)


@unittest.skipUnless(find_powershells(), "PowerShell is not installed here (runs in CI)")
class PsLauncherFileTests(Base):
    """agentguard.ps1 run with -File, as the skills run it."""

    def test_status_and_setup_with_real_python(self):
        for label, exe in find_powershells():
            with self.subTest(shell=label):
                p = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", PS_LAUNCHER, "--status"], env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertIn(b"AgentGuard plugin", p.stdout)
                p = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", PS_LAUNCHER, "--setup", "--host", self.server.url, "--public-key", self.server.public_key, "--secret-stdin"], input=(self.server.secret_key + "\n").encode(), env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertIn(b"OK: keys work", p.stdout)


if __name__ == "__main__":
    unittest.main()
