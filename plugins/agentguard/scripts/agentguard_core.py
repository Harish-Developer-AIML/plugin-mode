"""Shared core for the AgentGuard adapters (Claude Code, and the adapters for other agents).

Everything here is agent-independent: configuration, redaction, the error log, the state
file and its lock, HTTP sending to the Langfuse ingestion API within a time budget,
batching, the detached worker spawn, and helpers for status output.

Functions take what differs between agents (paths, platform, logger, User-Agent) as
arguments, so each adapter keeps its own files and behaviour. Python 3.8+ standard library
only. Importing this module has no side effects and prints nothing.
"""

import base64
import datetime as _dt
import getpass
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:  # Windows
    import msvcrt
except ImportError:
    msvcrt = None

DEFAULT_HOST = "https://ag-dev.actaclad.com"
# Total time for all HTTP requests in one run (AGENTGUARD_TIMEOUT_SECONDS). Sending happens in
# a detached background process, so a long budget never slows the agent down; AgentGuard's
# ingestion endpoint sometimes needs more than a few seconds to answer.
DEFAULT_TIMEOUT_SECONDS = 15.0
MIN_TIMEOUT_SECONDS = 1.0
MAX_TIMEOUT_SECONDS = 60.0
MAX_BATCH_EVENTS = 500
MAX_BATCH_BYTES = 3 * 1024 * 1024  # the server accepts 4.5 MB
STATE_RETENTION_DAYS = 7
ERROR_LOG_MAX_BYTES = 1024 * 1024
DEFAULT_MAX_CONTENT_CHARS = 2000
LOCK_WAIT_EXTRA_SECONDS = 20.0  # a waiting run allows the current one its whole budget plus this
# AgentGuard API keys: "sk-ag-<uuid>" / "pk-ag-<uuid>" (packages/shared/src/server/auth/apiKeys.ts).
# "sk-lf-" / "pk-lf-" are the Langfuse-style keys AgentGuard also accepts. Both secret and
# public keys are redacted from anything the adapters log or send.
KEY_PATTERN = re.compile(r"\b(?:sk|pk)-(?:ag|lf)-[A-Za-z0-9_-]{8,}")
TRUE_VALUES = ("1", "true", "yes", "on")
IS_WINDOWS = os.name == "nt"
# Windows process creation flags (subprocess has names for some of them only from 3.7 on).
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000
# The old setup ran an OpenTelemetry collector in Docker, listening for OTLP over HTTP on
# localhost:4318. While it runs, sessions reach AgentGuard twice.
OTEL_COLLECTOR_PORT = 4318
COMPOSE_DIR_LABEL = "com.docker.compose.project.working_dir"


# ---------------------------------------------------------------------------
# Configuration and redaction
# ---------------------------------------------------------------------------

def parse_timeout(raw):
    """AGENTGUARD_TIMEOUT_SECONDS -> (seconds, warning or None). Unset means the default;
    text that is not a number, or a number outside 1 to 60, also gives the default, with a
    warning that the worker logs once."""
    if raw is None or not str(raw).strip():
        return DEFAULT_TIMEOUT_SECONDS, None
    try:
        value = float(str(raw).strip())
    except ValueError:
        value = None
    if value is None or value != value or not (MIN_TIMEOUT_SECONDS <= value <= MAX_TIMEOUT_SECONDS):
        return DEFAULT_TIMEOUT_SECONDS, "AGENTGUARD_TIMEOUT_SECONDS=%r is not a number from %g to %g; using %g" % (
            str(raw)[:40], MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS)
    return value, None


class Config(object):
    """AgentGuard settings from an environment-like mapping (os.environ by default)."""

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.host = (env.get("AGENTGUARD_HOST") or DEFAULT_HOST).strip().rstrip("/")
        self.public_key = (env.get("AGENTGUARD_PUBLIC_KEY") or "").strip()
        self.secret_key = (env.get("AGENTGUARD_SECRET_KEY") or "").strip()
        self.log_content = (env.get("AGENTGUARD_LOG_CONTENT") or "").strip().lower() in TRUE_VALUES
        self.debug = (env.get("AGENTGUARD_DEBUG") or "").strip().lower() in TRUE_VALUES
        self.user = (env.get("AGENTGUARD_USER") or "").strip()
        try:
            self.max_content_chars = max(100, int(env.get("AGENTGUARD_MAX_CONTENT_CHARS") or DEFAULT_MAX_CONTENT_CHARS))
        except ValueError:
            self.max_content_chars = DEFAULT_MAX_CONTENT_CHARS
        self.timeout, self.timeout_warning = parse_timeout(env.get("AGENTGUARD_TIMEOUT_SECONDS"))

    @property
    def has_keys(self):
        return bool(self.public_key and self.secret_key)


# Settings in ~/.agentguard/config.json (written by `agentguard setup`) and the environment
# variable each one stands for. An environment variable that is set wins over the file.
CONFIG_FILE_KEYS = {
    "host": "AGENTGUARD_HOST",
    "public_key": "AGENTGUARD_PUBLIC_KEY",
    "secret_key": "AGENTGUARD_SECRET_KEY",
    "user": "AGENTGUARD_USER",
    "log_content": "AGENTGUARD_LOG_CONTENT",
    "max_content_chars": "AGENTGUARD_MAX_CONTENT_CHARS",
    "timeout_seconds": "AGENTGUARD_TIMEOUT_SECONDS",
    "debug": "AGENTGUARD_DEBUG",
}


def agentguard_home(environ=None):
    """~/.agentguard (or AGENTGUARD_HOME): the shared config, and state and logs for every
    adapter except Claude Code, which keeps its own files under ~/.claude/agentguard."""
    environ = os.environ if environ is None else environ
    return environ.get("AGENTGUARD_HOME") or os.path.join(os.path.expanduser("~"), ".agentguard")


def config_file_path(environ=None):
    return os.path.join(agentguard_home(environ), "config.json")


def read_config_file(path):
    """The config file as {ENV_NAME: text}; {} when missing. Raises ValueError when broken."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("%s is not a JSON object" % path)
    out = {}
    for key, env_name in CONFIG_FILE_KEYS.items():
        value = data.get(key)
        if isinstance(value, bool):
            out[env_name] = "true" if value else "false"
        elif isinstance(value, (str, int, float)) and str(value).strip():
            out[env_name] = str(value).strip()
    return out


def read_config_json(path):
    """The raw config file as a dict ({} when missing). Raises ValueError when it is broken,
    so a broken file is never overwritten."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return {}
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("%s is not a JSON object" % path)
    return data


def update_config_file(changes, path=None, is_windows=IS_WINDOWS):
    """Merge `changes` into ~/.agentguard/config.json and write it atomically, readable by the
    owner only (on Windows the file lives in the user's profile, which only they can read).
    A value of None removes that key; a dict value is merged one level deep."""
    path = path or config_file_path()
    data = read_config_json(path)
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        elif isinstance(value, dict) and isinstance(data.get(key), dict):
            merged = dict(data[key])
            for k, v in value.items():
                if v is None:
                    merged.pop(k, None)
                else:
                    merged[k] = v
            data[key] = merged
        else:
            data[key] = value
    ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    replace_file(tmp, path, is_windows=is_windows)
    return data


def load_config(environ=None, path=None):
    """(Config, sources): each setting from the environment when set there, else from
    ~/.agentguard/config.json. sources maps ENV_NAME to "environment" or "config file". A
    broken config file is ignored here (status reports it)."""
    environ = os.environ if environ is None else environ
    try:
        from_file = read_config_file(path or config_file_path(environ))
    except ValueError:
        from_file = {}
    merged, sources = {}, {}
    for env_name in CONFIG_FILE_KEYS.values():
        if (environ.get(env_name) or "").strip():
            merged[env_name], sources[env_name] = environ[env_name], "environment"
        elif env_name in from_file:
            merged[env_name], sources[env_name] = from_file[env_name], "config file"
    return Config(merged), sources


def redact(text, cfg=None):
    """Replace the configured secret and anything shaped like an AgentGuard key."""
    if not isinstance(text, str):
        return text
    if cfg is not None and cfg.secret_key and cfg.secret_key in text:
        text = text.replace(cfg.secret_key, "[REDACTED]")
    return KEY_PATTERN.sub("[REDACTED]", text)


def mask(value, keep=4):
    if not value:
        return "(not set)"
    if len(value) <= keep * 2 + 2:
        return "*" * len(value)
    return "%s...%s" % (value[: keep + 2], value[-keep:])


def mask_secret(value):
    if not value:
        return "(not set)"
    return "set (ends ...%s)" % value[-4:] if len(value) > 8 else "set"


# ---------------------------------------------------------------------------
# Files: directories, atomic replace, error log
# ---------------------------------------------------------------------------

def ensure_dir(path):
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def replace_file(src, dst, attempts=10, is_windows=IS_WINDOWS):
    """os.replace that copes with Windows, where it fails while another process has dst
    open (an editor, a virus scanner, or the agent reading its settings). Retries for
    about a second, then raises the last error."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not is_windows or attempt == attempts - 1:
                raise
            time.sleep(0.1)


def debug(msg, cfg=None):
    """Print to stderr, only when AGENTGUARD_DEBUG is on."""
    if cfg is not None and cfg.debug:
        try:
            sys.stderr.write("[agentguard] %s\n" % redact(msg, cfg))
        except Exception:
            pass


def log_error(msg, path, max_bytes=ERROR_LOG_MAX_BYTES, replace=None, cfg=None):
    """Append one redacted line to the error log at `path`, rotated to `path`.1 past
    `max_bytes`. Never raises."""
    replace = replace or replace_file
    try:
        debug("error: " + msg, cfg)
        ensure_dir(os.path.dirname(path))
        try:
            if os.path.getsize(path) > max_bytes:
                replace(path, path + ".1", attempts=3)
        except OSError:
            pass
        line = "%s %s\n" % (now_iso(), redact(msg, cfg).replace("\n", " | "))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_ts(value):
    """ISO 8601 string -> epoch seconds, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        v = value.replace("Z", "+00:00")
        return _dt.datetime.fromisoformat(v).timestamp()
    except ValueError:
        # fromisoformat before 3.11 accepts only 0, 3 or 6 fraction digits
        m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?$", value)
        if not m:
            return None
        frac = (m.group(2) or ".0")[1:7].ljust(6, "0")
        tz = m.group(3) or "Z"
        tz = "+00:00" if tz == "Z" else tz
        try:
            return _dt.datetime.fromisoformat("%s.%s%s" % (m.group(1), frac, tz)).timestamp()
        except ValueError:
            return None


def det_id(*parts):
    """Deterministic 32-hex id from the given parts."""
    return hashlib.sha256(":".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:32]


def truncate(text, limit):
    if not isinstance(text, str):
        return text
    if len(text) <= limit:
        return text
    return text[:limit] + "... [truncated %d chars]" % (len(text) - limit)


def text_size(content):
    try:
        return len(content) if isinstance(content, str) else len(json.dumps(content, ensure_ascii=False))
    except Exception:
        return 0


def envelope(event_type, body):
    """Ingestion event. The envelope id is a hash of type+body, so identical resends are identical."""
    digest = det_id(event_type, json.dumps(body, sort_keys=True, separators=(",", ":"), default=str))
    return {"id": digest, "timestamp": now_iso(), "type": event_type, "body": body}


# ---------------------------------------------------------------------------
# State file and lock
# ---------------------------------------------------------------------------

def empty_state():
    return {"version": 1, "sessions": {}, "last_send": None}


def load_state(path, log=None):
    """The state at `path`, or an empty state when it is missing or unreadable."""
    log = log or (lambda msg: None)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            st = json.load(fh)
        if isinstance(st, dict) and isinstance(st.get("sessions"), dict):
            st.setdefault("last_send", None)
            return st
        log("state.json has an unexpected shape; starting fresh")
    except (OSError, ValueError) as exc:
        if os.path.exists(path):
            log("could not read state.json (%s); starting fresh" % exc)
    return empty_state()


def save_state(st, path, replace=None):
    """Write the state atomically, readable by the owner only."""
    replace = replace or replace_file
    ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, separators=(",", ":"))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    replace(tmp, path)


def cleanup_state(st, now=None, retention_days=STATE_RETENTION_DAYS):
    now = time.time() if now is None else now
    cutoff = now - retention_days * 86400
    for sid in [s for s, v in st["sessions"].items() if not isinstance(v, dict) or v.get("updated", 0) < cutoff]:
        del st["sessions"][sid]


@contextmanager
def file_lock(path, wait=DEFAULT_TIMEOUT_SECONDS + LOCK_WAIT_EXTRA_SECONDS):
    """Exclusive lock on path. Yields True when acquired, False after waiting `wait` seconds."""
    ensure_dir(os.path.dirname(path))
    fh = open(path, "a+")
    acquired = False
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:  # pragma: no cover
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
                break
            except (OSError, IOError):
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield acquired
    finally:
        if acquired:
            try:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except (OSError, IOError):
                pass
        fh.close()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def auth_header(cfg):
    token = base64.b64encode(("%s:%s" % (cfg.public_key, cfg.secret_key)).encode("utf-8")).decode("ascii")
    return "Basic " + token


def http_request(cfg, method, path, body=None, timeout=DEFAULT_TIMEOUT_SECONDS, user_agent="agentguard"):
    """Returns (status, parsed_json_or_text). Raises on network errors and timeouts."""
    data = None
    headers = {
        "Authorization": auth_header(cfg),
        "User-Agent": user_agent,
        "Accept": "application/json",
    }
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(cfg.host + path, data=data, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=max(0.05, timeout))
        try:
            status, raw = resp.getcode(), resp.read()
        finally:
            resp.close()
    except urllib.error.HTTPError as exc:
        try:
            status, raw = exc.code, exc.read()
        except Exception:
            status, raw = exc.code, b""
        finally:
            exc.close()
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else None
    except (ValueError, UnicodeDecodeError):
        parsed = raw[:500].decode("utf-8", "replace")
    return status, parsed


def split_batches(events, max_events=MAX_BATCH_EVENTS, max_bytes=MAX_BATCH_BYTES):
    batches, cur, cur_bytes = [], [], 2
    for ev in events:
        size = len(json.dumps(ev, separators=(",", ":"))) + 1
        if cur and (len(cur) >= max_events or cur_bytes + size > max_bytes):
            batches.append(cur)
            cur, cur_bytes = [], 2
        cur.append(ev)
        cur_bytes += size
    if cur:
        batches.append(cur)
    return batches


class Sender(object):
    """Sends ingestion batches within one shared time budget.

    `log`, `debug_log`, `split` and `request` are the adapter's own functions (its error
    log, its batching limits, its User-Agent); the defaults only log nowhere."""

    def __init__(self, cfg, budget=None, log=None, debug_log=None, split=None, request=None):
        self.cfg = cfg
        self.budget = cfg.timeout if budget is None else budget
        self.deadline = time.monotonic() + self.budget
        self.last = None  # dict recorded as state["last_send"]
        self.sent_events = 0
        self._log = log or (lambda msg: None)
        self._debug = debug_log or (lambda msg: debug(msg, cfg))
        self._split = split or split_batches
        self._request = request or http_request

    def _record(self, ok, status, detail, count):
        self.last = {"time": now_iso(), "ok": ok, "status": status, "detail": redact(detail, self.cfg)[:300], "events": count, "host": self.cfg.host}

    def send(self, events):
        if not events:
            return True
        for batch in self._split(events):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0.05:
                self._record(False, None, "time budget (%gs, AGENTGUARD_TIMEOUT_SECONDS) used up; the rest is retried next run" % self.budget, len(batch))
                self._log(self.last["detail"])
                return False
            try:
                status, body = self._request(self.cfg, "POST", "/api/public/ingestion", {"batch": batch}, timeout=remaining)
            except Exception as exc:  # network error, timeout, DNS, TLS
                self._record(False, None, "%s: %s" % (type(exc).__name__, exc), len(batch))
                self._log("send failed: " + self.last["detail"])
                return False
            if status < 200 or status >= 300:
                msg = body.get("message") if isinstance(body, dict) else body
                # 401: the keys are wrong. 403: the keys work but AgentGuard refuses the data
                # (for example the organization's plan ended); its message says why.
                hint = {401: " (check AGENTGUARD_PUBLIC_KEY / AGENTGUARD_SECRET_KEY)", 403: " (AgentGuard refused the data; see its message)"}.get(status, "")
                self._record(False, status, "HTTP %s: %s%s" % (status, str(msg)[:200], hint), len(batch))
                self._log("send failed: " + self.last["detail"])
                return False
            errors = body.get("errors") if isinstance(body, dict) else None
            errors = errors if isinstance(errors, list) else []
            server_errors = [e for e in errors if not isinstance(e, dict) or not isinstance(e.get("status"), int) or e["status"] >= 500]
            if server_errors:
                self._record(False, status, "server error on %d event(s): %s" % (len(server_errors), str(server_errors[0].get("message"))[:200]), len(batch))
                self._log("send failed: " + self.last["detail"])
                return False
            if errors:  # 4xx per event: would fail again, so log and move on
                self._log("AgentGuard rejected %d event(s), first: %s" % (len(errors), json.dumps(errors[0])[:400]))
            self.sent_events += len(batch)
            self._record(True, status, "sent %d event(s)%s" % (len(batch), ", %d rejected" % len(errors) if errors else ""), len(batch))
            self._debug(self.last["detail"])
        return True


def test_keys(cfg, request=None):
    """Check the keys against AgentGuard. Returns (ok, message)."""
    request = request or http_request
    try:
        status, body = request(cfg, "GET", "/api/public/projects", timeout=cfg.timeout)
    except Exception as exc:
        return False, "could not reach %s: %s: %s" % (cfg.host, type(exc).__name__, exc)
    if status == 200:
        names = []
        if isinstance(body, dict) and isinstance(body.get("data"), list):
            names = [p.get("name") for p in body["data"] if isinstance(p, dict) and p.get("name")]
        return True, "keys work" + (" (project: %s)" % ", ".join(names) if names else "")
    if status in (401, 403):
        return False, "AgentGuard refused the keys (HTTP %s). Check the public and secret key and the host." % status
    return False, "unexpected answer from %s: HTTP %s" % (cfg.host, status)




# ---------------------------------------------------------------------------
# Processes: detached worker, git identity
# ---------------------------------------------------------------------------

def spawn_detached(argv, env, is_windows=IS_WINDOWS):
    """Start argv in the background with no inherited stdio, so the caller can exit at once
    and nothing waits on it."""
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True, "env": env}
    if not is_windows:
        subprocess.Popen(argv, start_new_session=True, **kwargs)
        return
    # No console, own process group, and out of the agent's job object if the job allows
    # it, so the worker is not killed with the hook or with the agent. A job that forbids
    # breakaway makes CreateProcess fail with "access denied": then start it inside the job.
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(argv, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except OSError:
        subprocess.Popen(argv, creationflags=flags, **kwargs)


def resolve_user(cfg, cwd, is_windows=IS_WINDOWS):
    """AGENTGUARD_USER, else `git config user.email` in cwd, else the OS user name."""
    if cfg.user:
        return cfg.user
    try:
        extra = {"creationflags": CREATE_NO_WINDOW} if is_windows else {}  # the worker has no console: don't open one
        out = subprocess.run(["git", "config", "user.email"], cwd=cwd if cwd and os.path.isdir(cwd) else None, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2, **extra)
        email = out.stdout.decode("utf-8", "replace").strip()
        if email:
            return email
    except Exception:
        pass
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Old OpenTelemetry collector (status and setup output)
# ---------------------------------------------------------------------------

def port_open(port, host="127.0.0.1", timeout=0.3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def publishes_port(ports, port):
    """True if a `docker ps` Ports column publishes `port` on the host, for example
    "0.0.0.0:4318->4318/tcp" or "0.0.0.0:4317-4318->4317-4318/tcp"."""
    for match in re.finditer(r":(\d+)(?:-(\d+))?->", ports or ""):
        low = int(match.group(1))
        high = int(match.group(2) or low)
        if low <= port <= high:
            return True
    return False


def docker_containers_on_port(port, is_windows=IS_WINDOWS):
    """[(container name, compose working dir or "")] for running containers that publish
    `port`. Empty when docker is missing, fails, or is slow: this check must never get in
    the way."""
    docker = shutil.which("docker")
    if not docker:
        return []
    fmt = "{{.Names}}\t{{.Ports}}\t{{.Label \"%s\"}}" % COMPOSE_DIR_LABEL
    extra = {"creationflags": CREATE_NO_WINDOW} if is_windows else {}
    try:
        proc = subprocess.run([docker, "ps", "--format", fmt], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, **extra)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    found = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = line.strip("\r").split("\t")
        if len(parts) >= 2 and parts[0].strip() and publishes_port(parts[1], port):
            found.append((parts[0].strip(), parts[2].strip() if len(parts) > 2 else ""))
    return found


def quote_path(path, is_windows=IS_WINDOWS):
    if is_windows:  # PowerShell single quotes: a quote inside is doubled
        return "'%s'" % path.replace("'", "''")
    return "'%s'" % path.replace("'", "'\\''")


def collector_advice(port=OTEL_COLLECTOR_PORT, is_windows=IS_WINDOWS):
    """Lines telling the user how to stop a still-running old collector; [] if nothing
    listens on its port. Only prints instructions: it never stops anything itself."""
    if not port_open(port):
        return []
    containers = docker_containers_on_port(port, is_windows)
    if not containers:
        return [
            "WARNING: something is listening on 127.0.0.1:%d, the port of the old OpenTelemetry collector. "
            "If it is that collector, it makes every session appear twice: stop it from the folder that has "
            "its docker-compose.yml, and nowhere else." % port
        ]
    lines = []
    projects = []  # compose working dirs, in order, each with its container names
    for name, workdir in containers:
        if workdir:
            for entry in projects:
                if entry[0] == workdir:
                    entry[1].append(name)
                    break
            else:
                projects.append((workdir, [name]))
        else:
            lines.append("WARNING: an OpenTelemetry collector is still running (container %s). Stop it with:" % name)
            lines.append("  docker stop %s" % name)
            lines.append("  docker update --restart=no %s" % name)
    join = "; " if is_windows else " && "
    for workdir, names in projects:
        lines.append("WARNING: an OpenTelemetry collector is still running (container %s). Stop it with:" % ", ".join(names))
        lines.append("  cd %s%sdocker compose down" % (quote_path(workdir, is_windows), join))
    lines.append("It makes every session appear twice. This plugin never stops containers itself; "
                 "run \"docker compose down\" only in the folder shown, because in any other folder it stops a different project.")
    return lines
