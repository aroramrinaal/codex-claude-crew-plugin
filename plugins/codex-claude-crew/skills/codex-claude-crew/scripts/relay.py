#!/usr/bin/env python3
"""Run one bounded task in a local Claude Code session for Codex.

Codex calls this script instead of building `claude -p` commands by hand. It sets
the permission boundary for the chosen mode, creates the git worktree for
implement runs, captures Claude's JSON output, and writes a run record that keeps
Claude's claims apart from Codex's own verification.

Standard library only. macOS and Linux.

  relay.py preflight                    check the claude CLI and sign-in (no model call)
  relay.py run --mode review ...        start a run (see `relay.py run --help`)
  relay.py status RUN [--wait SECONDS]  print a run record
  relay.py list [--limit N] [--thread]  list recent runs
  relay.py verify RUN --verdict V ...   record Codex's own verification
  relay.py cancel RUN                   stop a running run

Environment: CLAUDE_CREW_HOME (state folder, default ~/.codex-claude-crew) and
CLAUDE_CREW_BIN (path to the claude executable, default: found on PATH).
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

RECORD_SCHEMA = "codex-claude-crew.run/v1"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
VERDICTS = ("confirmed", "partial", "rejected")
ACTIVE = ("queued", "running", "stopping", "orphaned")
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
# Require both restricted mode and unattended permission denial; no weaker fallback.
MIN_CLAUDE_VERSION = (2, 1, 259)
PROFILE_FLAGS = ["--restricted", "--permission-prompts", "none", "--no-chrome",
                 "--disable-slash-commands", "--strict-mcp-config",
                 "--mcp-config", '{"mcpServers":{}}']
DEFAULT_REQUEST = {"model": None, "effort": None, "max_turns": 50,
                   "max_budget_usd": None, "timeout_s": 1800, "allow": []}

READ_TOOLS = ["Read", "Glob", "Grep"]
EXTRA_TOOLS = ("Bash", "WebFetch", "WebSearch")  # what --allow may add
GIT_READ_RULES = [
    "Bash(git diff *)",
    "Bash(git log *)",
    "Bash(git show *)",
    "Bash(git status *)",
    "Bash(git blame *)",
]
DENY_RULES = ["Bash(git commit *)", "Bash(git push *)", "mcp__*"]

NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,47}")
RUN_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
MODEL_RE = re.compile(r"[A-Za-z0-9._:@/\[\]-]{1,100}")
RULE_RE = re.compile(r"([A-Za-z][A-Za-z0-9_]*)(\(.+\))?")

# Passed with --json-schema, so Claude's final answer arrives as structured_output.
REPORT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["complete", "partial", "blocked"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "severity": {"type": "string", "enum": ["high", "medium", "low", "info"]},
                    "title": {"type": "string"},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "detail": {"type": "string"},
                },
                "required": ["severity", "title", "detail"],
            },
        },
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "command": {"type": "string"},
                    "status": {"type": "string", "enum": ["passed", "failed", "not_run", "denied"]},
                    "notes": {"type": "string"},
                },
                "required": ["command", "status"],
            },
        },
        "open_issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary", "findings", "changed_files", "checks", "open_issues"],
}

# Appended to Claude Code's system prompt, so the brief can stay about the task.
WORKER_RULES = """\
You are a worker session started by Codex, another coding agent, through codex-claude-crew. \
Nobody is watching this session: no one can answer questions or approve permission prompts.
- Work only on the task in the brief, inside the working directory.
- Do not commit, push, rebase, reset or switch branches. Codex reviews and lands the work.
- If you are blocked or the brief is ambiguous, stop and say so in the report instead of guessing.
- Report checks honestly. Mark a check passed only if you ran it and saw it pass; mark denied commands as denied."""

MODE_RULES = {
    "review": "Mode: review. This run is read-only: do not create, edit or delete files. "
    "Put each issue in `findings`, with a file path and line where you can. Leave `changed_files` empty.",
    "implement": "Mode: implement. You are in a git worktree made for this task. "
    "Make the change the brief asks for and keep the diff focused on it. "
    "List every file you changed in `changed_files`.",
}


class Fail(Exception):
    """Stops a command: prints {"status", "error"} as JSON and exits with `code`."""

    def __init__(self, message, code=2, status="usage_error"):
        super().__init__(message)
        self.code = code
        self.status = status


class Interrupted(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def emit(payload, code=0):
    print(json.dumps(payload, indent=2))
    return code


def crew_home():
    return Path(os.environ.get("CLAUDE_CREW_HOME") or Path.home() / ".codex-claude-crew").expanduser().resolve()


def runs_dir():
    return crew_home() / "runs"


# --- Claude Code CLI ---------------------------------------------------------


def claude_bin():
    configured = os.environ.get("CLAUDE_CREW_BIN")
    if configured:
        path = Path(configured).expanduser().resolve()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which("claude")


def claude_version(binary):
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"\d+\.\d+\.\d+", proc.stdout)
    return match.group(0) if match else None


def version_tuple(version):
    return tuple(int(part) for part in version.split(".")) if version else ()


def capabilities(binary):
    """Probe parser support without a model call. Help alone can omit valid flags."""
    version = claude_version(binary)
    supported = version_tuple(version) >= MIN_CLAUDE_VERSION
    flags_ok = False
    if supported:
        try:
            proc = subprocess.run(
                [binary, *PROFILE_FLAGS, "--max-turns", "1", "--json-schema", "{}", "--help"],
                capture_output=True, text=True, timeout=30,
            )
            flags_ok = proc.returncode == 0 and not re.search(
                r"unknown option|unexpected option|unrecognized", proc.stderr, re.I)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {"version": version, "minimum_version": ".".join(map(str, MIN_CLAUDE_VERSION)),
            "version_supported": supported, "restricted_profile": bool(flags_ok)}


def claude_args(record, version):
    """The `claude -p` flags for a run. The brief itself goes in on stdin."""
    mode, request = record["mode"], record["request"]
    if mode == "review":
        tools = READ_TOOLS + ["Bash"]
        allowed = READ_TOOLS + GIT_READ_RULES
        permission_mode = "dontAsk"  # anything not pre-approved or read-only is denied
    else:
        tools = READ_TOOLS + ["Edit", "Write", "Bash"]
        allowed = READ_TOOLS + ["Edit", "Write"] + GIT_READ_RULES
        permission_mode = "acceptEdits"  # file edits pass; other commands need an --allow rule
    for rule in request["allow"]:
        tool = rule.split("(", 1)[0]
        if tool not in tools:
            tools.append(tool)
    args = [
        "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(REPORT_SCHEMA, separators=(",", ":")),
        "--append-system-prompt", f"{WORKER_RULES}\n\n{MODE_RULES[mode]}",
        "--permission-mode", permission_mode,
        "--tools", ",".join(tools),
        "--allowedTools", *allowed, *request["allow"],
        "--disallowedTools", ",".join(DENY_RULES),
        *PROFILE_FLAGS,
        "--max-turns", str(request["max_turns"]),
    ]
    if request["max_budget_usd"] is not None:
        args += ["--max-budget-usd", str(request["max_budget_usd"])]
    if request["model"]:
        args += ["--model", request["model"]]
    if request["effort"]:
        args += ["--effort", request["effort"]]
    if request["resume_from"]:
        args += ["--resume", request["resume_from"]["session_id"]]
        if request["fork"]:
            args.append("--fork-session")
    else:
        # Choosing the id up front means a run that times out can still be resumed.
        args += ["--session-id", record["claude"]["session_id"]]
    return args


def transcript_path(cwd, session_id):
    """Claude Code keeps each session at ~/.claude/projects/<cwd with non-alphanumerics as ->/<id>.jsonl."""
    projects = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    for folder in (cwd, os.path.realpath(cwd)):
        path = projects / re.sub(r"[^A-Za-z0-9]", "-", folder) / f"{session_id}.jsonl"
        if path.is_file():
            return str(path)
    return None


# --- git -----------------------------------------------------------------------


def git(cwd, *args, check=True):
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, encoding="utf-8", errors="surrogateescape", timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Fail(f"could not run git: {exc}", code=1, status="error")
    if check and proc.returncode != 0:
        raise Fail(f"git {' '.join(args)} failed: {proc.stderr.strip()}", code=1, status="error")
    return proc.stdout


def git_root(cwd):
    if not shutil.which("git"):
        return None
    root = git(cwd, "rev-parse", "--show-toplevel", check=False).strip()
    return Path(root) if root else None


def git_status(root):
    """Changed paths in the working tree and index, as {path: two-letter status}."""
    out = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries, parts = {}, iter(out.split("\0"))
    for item in parts:
        if len(item) < 4:
            continue
        code, path = item[:2], item[3:]
        if "R" in code or "C" in code:
            next(parts, None)  # renames and copies carry the original path next
        entries[path] = code
    return entries


def fingerprint(root):
    """Snapshot HEAD, index and contents of Git-visible paths, including clean files.

    Ignored files and changes outside this repository are deliberately out of scope.
    A submodule or unreadable path makes coverage incomplete, never 'unchanged'.
    """
    result = {"head": git(root, "rev-parse", "--verify", "HEAD", check=False).strip(),
              "ref": git(root, "symbolic-ref", "-q", "HEAD", check=False).strip(),
              "status": git_status(root), "paths": {}, "index": {}, "complete": True}
    for entry in git(root, "ls-files", "--stage", "-z").split("\0"):
        if entry:
            metadata, path = entry.split("\t", 1)
            result["index"].setdefault(path, []).append(metadata)
    paths = set(result["index"]) | set(result["status"])
    for path in sorted(paths):
        target = root / path
        try:
            if target.is_symlink():
                value = "symlink:" + os.readlink(target)
            elif target.is_file():
                digest = hashlib.sha256()
                with target.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                value = f"{target.stat().st_mode & 0o777}:{digest.hexdigest()}"
            elif not target.exists():
                value = "missing"
            else:
                value = None
                result["complete"] = False
            result["paths"][path] = value
        except OSError:
            result["paths"][path] = None
            result["complete"] = False
    return result


def create_worktree(cwd, name, base):
    if not NAME_RE.fullmatch(name):
        raise Fail("--worktree takes a short name: lowercase letters, digits, '.', '_' or '-'")
    root = git_root(cwd)
    if not root:
        raise Fail(f"implement runs need a git repository; {cwd} isn't in one")
    base_sha = git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False).strip()
    if not base_sha:
        raise Fail(f"--base {base!r} isn't a commit in {root}")
    repo_key = f"{root.name}-{hashlib.sha1(str(root).encode()).hexdigest()[:8]}"
    path = crew_home() / "worktrees" / repo_key / name
    if path.exists():
        raise Fail(f"{path} already exists; continue it with --resume RUN_ID or pick another name")
    path.parent.mkdir(parents=True, exist_ok=True)
    branch = f"crew/{name}"
    git(root, "worktree", "add", "-b", branch, str(path), base_sha)
    notes = []
    if git_status(root):
        notes.append("The main checkout has uncommitted changes. The worktree starts from the base commit without them.")
    return {"repo": str(root), "path": str(path), "branch": branch, "base": base_sha}, notes


# --- run records -----------------------------------------------------------------


@contextmanager
def state_lock():
    """Serialize short state transactions across CLI processes (macOS/Linux)."""
    crew_home().mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(crew_home() / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def record_path(run_id):
    return runs_dir() / run_id / "run.json"


def save(record):
    """Atomic replacement. Callers serialize read/modify/write with state_lock."""
    path = record_path(record["run_id"])
    fd, temporary = tempfile.mkstemp(prefix="run-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load(run_id):
    return json.loads(record_path(run_id).read_text())


def all_records():
    """Every saved record, newest first."""
    records = []
    for path in runs_dir().glob("*/run.json"):
        try:
            records.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return sorted(records, key=lambda record: record["created_at"], reverse=True)


def find_run(ref):
    """Look a run up by run id, or by Claude session id (newest run of that session)."""
    if RUN_ID_RE.fullmatch(ref) and record_path(ref).is_file():
        return load(ref)
    if UUID_RE.fullmatch(ref):
        for record in all_records():
            if record["claude"].get("session_id") == ref:
                return record
    return None


def require_run(ref):
    record = find_run(ref)
    if not record:
        raise Fail(f"no run found for {ref!r}")
    return record


def process_table():
    """Read process identities without command lines or environment values."""
    proc = subprocess.run(["ps", "-axo", "pid=,pgid=,stat=,lstart="],
                          capture_output=True, text=True, timeout=10,
                          env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
    if proc.returncode:
        raise Fail("could not inspect processes; execution state is unconfirmed", 1, "error")
    rows = {}
    for line in proc.stdout.splitlines():
        fields = line.split(None, 3)
        if len(fields) == 4 and "Z" not in fields[2]:
            rows[int(fields[0])] = {"pid": int(fields[0]), "pgid": int(fields[1]),
                                    "started": " ".join(fields[3].split())}
    return rows


def process_identity(pid):
    return process_table().get(pid)


def same_process(identity, table=None):
    if not identity:
        return False
    actual = (process_table() if table is None else table).get(identity["pid"])
    return actual is not None and actual["started"] == identity["started"]


def group_members(pgid):
    return {pid: row for pid, row in process_table().items() if row["pgid"] == pgid}


def stop_group(identity, proc=None, owned=False):
    """Stop the entire group, even after its leader exits.

    During recovery, refuse to signal a reused PID or an unidentifiable orphan
    group. Such jobs retain their reservation and require manual inspection.
    """
    if not identity:
        return False
    pgid = identity["pid"]
    members = group_members(pgid)
    if not members:
        return True
    leader = process_table().get(pgid)
    if leader and not same_process(identity, {pgid: leader}):
        return False
    if not leader and not owned:
        return False
    for sig, grace in ((signal.SIGINT, 5), (signal.SIGTERM, 3), (signal.SIGKILL, 2)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if proc is not None:
                proc.poll()  # reap the leader; zombies cannot do any more work
            if not group_members(pgid):
                return True
            time.sleep(0.1)
    return not group_members(pgid)


def refresh(record):
    """Reconcile lost workers; never release a live or unconfirmed execution."""
    with state_lock():
        record = load(record["run_id"])
        if record["status"] not in ACTIVE:
            return record
        table = process_table()
        if same_process(record.get("worker"), table):
            return record
        if record["status"] == "queued" and same_process(record.get("launcher"), table):
            return record
        if same_process(record.get("recovery_owner"), table):
            return record
        record.update(status="stopping", recovery_owner=process_identity(os.getpid()))
        save(record)
    child = record.get("child")
    # With the gated launch protocol an unrecorded child cannot start Claude.
    stopped = stop_group(child) if child else record.get("gated_launch", False)
    if stopped:
        baseline = record.get("baseline") or {}
        observe(record, Path(record["git_root"]) if record.get("git_root") else None, baseline)
        return finish(record, "error", "the relay worker was lost; execution has stopped")
    return finish(record, "orphaned", "worker lost; execution could not be confirmed stopped; inspect the recorded process group")


def finish(record, status, error):
    session_id = record["claude"]["session_id"]
    if session_id:
        record["claude"]["transcript"] = transcript_path(record["cwd"], session_id)
    record.update(status=status, error=error,
                  finished_at=None if status in ACTIVE else now(), recovery_owner=None)
    with state_lock():
        current = load(record["run_id"])
        # A late writer must not resurrect or overwrite a terminal record.
        if current["status"] not in ACTIVE:
            return current
        save(record)
    return record


# --- running Claude -----------------------------------------------------------------


def _interrupt(signum, frame):
    for sig in STOP_SIGNALS:
        signal.signal(sig, signal.SIG_IGN)
    raise Interrupted()


def parse_result(stdout):
    """The `result` object that `claude -p --output-format json` prints, or None."""
    text = stdout.strip()
    for candidate in [text] + text.splitlines()[::-1] if text else []:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("type") == "result":
            return data
    return None


def denial(item):
    tool_input = item.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    detail = tool_input.get("command") or tool_input.get("file_path") or tool_input.get("url") or json.dumps(tool_input)
    return {"tool": item.get("tool_name"), "input": str(detail)[:300]}


def validate_report(value, schema=REPORT_SCHEMA, path="report"):
    """Validate the JSON Schema subset used by REPORT_SCHEMA, without dependencies."""
    kind = schema["type"]
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "integer": type(value) is int}[kind]
    if not valid:
        return f"{path} must be {kind}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} has an invalid value"
    if kind == "object":
        properties = schema["properties"]
        if set(schema.get("required", [])) - set(value):
            return f"{path} is missing required fields"
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return f"{path} contains unexpected fields"
        for key, item in value.items():
            error = validate_report(item, properties[key], f"{path}.{key}")
            if error:
                return error
    elif kind == "array":
        for index, item in enumerate(value):
            error = validate_report(item, schema["items"], f"{path}[{index}]")
            if error:
                return error
    return None


def read_result(record, stdout, stderr, status, error):
    claude = record["claude"]
    result = parse_result(stdout)
    if result is None:
        if status == "completed":
            status, error = "error", "Claude Code returned no JSON result"
            tail = stderr.strip()[-1500:]
            if tail:
                error += f": {tail}"
            if re.search(r"unknown option|unexpected option|unrecognized", stderr, re.I):
                error += " (the installed Claude Code may be older than a flag the relay uses; try `claude update`)"
        return status, error
    claude.update(
        session_id=result.get("session_id") or claude["session_id"],
        subtype=result.get("subtype"),
        is_error=bool(result.get("is_error")),
        num_turns=result.get("num_turns"),
        duration_ms=result.get("duration_ms"),
        models=sorted(result.get("modelUsage")) if isinstance(result.get("modelUsage"), dict) else [],
        permission_denials=[denial(item) for item in (result.get("permission_denials") or []) if isinstance(item, dict)]
            if isinstance(result.get("permission_denials", []), list) else [],
        errors=[str(item) for item in (result.get("errors") or [])]
            if isinstance(result.get("errors", []), list) else [],
        result_text=result.get("result") if isinstance(result.get("result"), str) else None,
    )
    cost = result.get("total_cost_usd")
    cost = cost if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0 else None
    claude["cost_usd"] = claude["session_cost_usd"] = cost  # old field retained as an alias
    prior = record.get("cost_baseline_usd")
    if type(prior) not in (int, float) or not math.isfinite(prior) or prior < 0:
        prior = None
    claude["cost_delta_usd"] = round(cost - prior, 10) if cost is not None and prior is not None and cost >= prior else None
    claimed = result.get("structured_output")
    report_error = validate_report(claimed)
    record["report_error"] = report_error
    record["claimed"] = claimed if report_error is None else None
    if status == "completed" and (claude["is_error"] or claude["subtype"] != "success" or claude["exit_code"] != 0):
        status = "error"
        error = (
            "; ".join(claude["errors"])
            or (claude["result_text"] or "")[:500]
            or f"Claude Code ended with {claude['subtype']} (exit code {claude['exit_code']})"
        )
    if status == "completed" and report_error:
        status, error = "error", f"Claude returned an invalid or missing structured report: {report_error}"
    return status, error


def observe(record, root, before):
    """Record what git shows, independent of what Claude reports."""
    observed = record["observed"]
    try:
        if record["mode"] == "review" and root and before:
            after = fingerprint(root)
            paths = set(before["paths"]) | set(after["paths"])
            changed = {p for p in paths if
                       (before["paths"].get(p) is not None and after["paths"].get(p) is not None and
                        before["paths"][p] != after["paths"][p]) or
                       before["index"].get(p) != after["index"].get(p) or
                       before["status"].get(p) != after["status"].get(p)}
            head_changed = before["head"] != after["head"] or before["ref"] != after["ref"]
            observed.update(head_before=before["head"], head_after=after["head"],
                            head_changed=head_changed, coverage_complete=before["complete"] and after["complete"])
            observed["changed_files"] = sorted(changed)
            observed["read_only_violation"] = True if changed or head_changed else (
                False if observed["coverage_complete"] else None)
            if before["head"] and after["head"]:
                observed["commits"] = git(root, "log", "--format=%h %s", f"{before['head']}..{after['head']}").splitlines()
            elif after["head"]:
                observed["commits"] = git(root, "log", "--format=%h %s", after["head"]).splitlines()
        elif record["mode"] == "implement" and record["worktree"]:
            path, base = record["worktree"]["path"], record["worktree"]["base"]
            committed = git(path, "diff", "--name-only", "-z", base, "HEAD").split("\0")
            observed["changed_files"] = sorted(set(git_status(Path(path))) | {p for p in committed if p})
            observed["commits"] = git(path, "log", "--format=%h %s", f"{base}..HEAD").splitlines()
    except Fail as exc:
        observed["error"] = str(exc)


def execute(record):
    """Own a queued reservation, run Claude, and retain it until its group stops."""
    with state_lock():
        record = load(record["run_id"])
        if record["status"] != "queued":
            return record
        if record.get("worker") and record["worker"]["pid"] != os.getpid():
            raise Fail("this run already belongs to another worker")
        record.update(worker_pid=os.getpid(), worker=process_identity(os.getpid()))
        if record["worker"] is None:
            raise Fail("could not establish worker process identity", 1, "error")
        save(record)  # before probing the CLI or reading the repository

    files = record["files"]
    proc, child = None, None
    root, before = None, None
    status, error = "completed", None
    handlers = {sig: signal.signal(sig, _interrupt) for sig in STOP_SIGNALS}
    try:
        binary = claude_bin()
        if not binary:
            raise Fail("claude not found; install Claude Code or set CLAUDE_CREW_BIN", 127, "claude_unavailable")
        caps = capabilities(binary)
        record["claude"]["version"] = caps["version"]
        if not caps["restricted_profile"]:
            raise Fail(f"Claude Code {caps['minimum_version']}+ with the restricted profile is required; run claude update", 1, "error")
        argv = [binary, *claude_args(record, caps["version"])]
        Path(files["run_dir"], "command.json").write_text(json.dumps(argv, indent=2))
        if record["mode"] == "review":
            root = git_root(record["cwd"])
            try:
                before = fingerprint(root) if root else None
            except Fail as exc:
                record["notes"].append(f"Read-only snapshot unavailable: {exc}")
            if before is None:
                record["notes"].append("Read-only tripwire coverage is unavailable; inspect files independently.")
        record.update(status="running", started_at=now(), baseline=before,
                      git_root=str(root) if root else None)
        with state_lock():
            save(record)
        with open(files["stdout"], "wb") as out, open(files["stderr"], "wb") as err:
            # The wrapper cannot exec Claude until we save its process identity
            # and release its stdin gate. A SIGKILL before that closes the pipe.
            mask = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
            try:
                proc = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "_exec", record["run_id"]],
                    cwd=record["cwd"], stdin=subprocess.PIPE, stdout=out, stderr=err,
                    start_new_session=True,
                )
                child = process_identity(proc.pid)
                if not child:
                    raise Fail("could not identify the gated Claude process", 1, "error")
                record["child"] = child
                with state_lock():
                    save(record)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)
            try:
                proc.communicate(b"G" + Path(files["brief"]).read_bytes(), timeout=record["request"]["timeout_s"])
            except subprocess.TimeoutExpired:
                status, error = "timed_out", f"Claude didn't finish within {record['request']['timeout_s']}s"
    except Interrupted:
        status, error = "interrupted", "the relay was stopped before Claude finished"
    except (Fail, OSError, ValueError, subprocess.SubprocessError) as exc:
        status, error = getattr(exc, "status", "error"), str(exc)
    finally:
        # Cleanup itself must not be interrupted halfway through the escalation.
        for sig in STOP_SIGNALS:
            signal.signal(sig, signal.SIG_IGN)
        try:
            if proc is not None:
                child = child or process_identity(proc.pid)
                stopped = stop_group(child, proc=proc, owned=True) if child else proc.poll() is not None
                if not stopped:
                    status, error = "orphaned", "execution could not be confirmed stopped; inspect the recorded process group"
                if proc.stdin:
                    proc.stdin.close()
                proc.poll()
                record["claude"]["exit_code"] = proc.returncode
        except (Fail, OSError, subprocess.SubprocessError) as exc:
            status, error = "orphaned", f"could not confirm process cleanup: {exc}"
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)

    stdout = Path(files["stdout"]).read_text(errors="replace") if Path(files["stdout"]).exists() else ""
    stderr = Path(files["stderr"]).read_text(errors="replace") if Path(files["stderr"]).exists() else ""
    status, error = read_result(record, stdout, stderr, status, error)
    observe(record, root, before)
    return finish(record, status, error)


def cmd_exec(args):
    """Internal gated exec: no model starts unless the owner persisted our identity."""
    signal.pthread_sigmask(signal.SIG_UNBLOCK, STOP_SIGNALS)
    if os.read(sys.stdin.fileno(), 1) != b"G":
        return 1
    record = require_run(args.run_id)
    if not same_process(record.get("child")) or record["child"]["pid"] != os.getpid():
        raise Fail("Claude launch ownership was not recorded")
    argv = json.loads(Path(record["files"]["run_dir"], "command.json").read_text())
    os.execv(argv[0], argv)


# --- commands --------------------------------------------------------------------


def cmd_preflight(args):
    binary = claude_bin()
    report = {
        "ok": False, "claude": binary, "version": None, "auth": None,
        "git": shutil.which("git"), "python": platform.python_version(),
        "state_dir": str(crew_home()), "state_writable": False,
        "capabilities": None, "process_inspection": False, "notes": [],
    }
    try:
        crew_home().mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryFile(dir=crew_home()) as probe:
            probe.write(b"crew")
        report["state_writable"] = True
    except OSError:
        report["notes"].append("The state directory is not writable.")
    try:
        report["process_inspection"] = process_identity(os.getpid()) is not None
    except (OSError, Fail, subprocess.SubprocessError):
        report["notes"].append("Process inspection with ps is required for safe cancellation.")
    if not binary:
        report["notes"].append("claude not found. Install Claude Code and sign in, or set CLAUDE_CREW_BIN.")
        return emit(report, 127)
    caps = report["capabilities"] = capabilities(binary)
    report["version"] = caps["version"]
    if not caps["restricted_profile"]:
        report["notes"].append(f"Claude Code {caps['minimum_version']}+ with restricted/profile flags is required. Run claude update; no weaker fallback is used.")
    try:
        proc = subprocess.run([binary, "auth", "status", "--json"], capture_output=True, text=True, timeout=30)
        data = json.loads(proc.stdout)
        report["auth"] = {key: data.get(key) for key in ("loggedIn", "authMethod", "apiProvider", "subscriptionType")}
        if proc.returncode:
            report["auth"]["loggedIn"] = False
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        report["notes"].append("Could not read claude auth status.")
    if not report["git"]:
        report["notes"].append("git is required for worktrees and the read-only tripwire.")
    logged_in = (report["auth"] or {}).get("loggedIn") is True
    if not logged_in:
        report["notes"].append("Claude Code did not confirm authentication. Check credentials and network/keychain access under the host's permission policy.")
    report["ok"] = all((caps["restricted_profile"], logged_in, report["git"],
                        report["state_writable"], report["process_inspection"]))
    return emit(report, 0 if report["ok"] else 1)


def resolve_dir(value):
    path = Path(value or os.getcwd()).expanduser().resolve()
    if not path.is_dir():
        raise Fail(f"{path} is not a folder")
    return path


def resolve_resume(args):
    """Where and how to continue an earlier session: (mode, cwd, worktree, resume_from)."""
    if args.worktree or args.base:
        raise Fail("--worktree and --base can't be combined with --resume; the run continues where the session started")
    previous = find_run(args.resume)
    if previous:
        if previous["status"] in ACTIVE:
            raise Fail(f"run {previous['run_id']} is still active; inspect status or cancel it first")
        if args.mode and args.mode != previous["mode"]:
            raise Fail(f"run {previous['run_id']} is a {previous['mode']} run; start a new run to switch modes")
        session_id = previous["claude"]["session_id"]
        if not session_id:
            raise Fail(f"run {previous['run_id']} has no Claude session id to resume")
        if not Path(previous["cwd"]).is_dir():
            raise Fail(f"{previous['cwd']} no longer exists, so its session can't be resumed there")
        resume_from = {"run_id": previous["run_id"], "session_id": session_id}
        return previous["mode"], Path(previous["cwd"]), previous["worktree"], resume_from
    if not UUID_RE.fullmatch(args.resume):
        raise Fail(f"no run or session found for {args.resume!r}")
    if args.mode != "review":
        raise Fail("to resume a session the relay didn't start, pass --mode review and the --cwd it ran in")
    return "review", resolve_dir(args.cwd), None, {"run_id": None, "session_id": args.resume}


def check_allow_rules(rules):
    for rule in rules:
        match = RULE_RE.fullmatch(rule)
        if not match or match.group(1) not in EXTRA_TOOLS or any(c in rule for c in (",", "\n", "\r")):
            raise Fail(f"--allow takes scoped Bash rules, WebFetch or WebSearch, not {rule!r}")
        if match.group(1) == "Bash":
            command = match.group(2)[1:-1].strip() if match.group(2) else ""
            # Require a literal executable prefix. Do not accept wildcard-only,
            # legacy ':*', comma-separated rule injection, or multiline rules.
            if not re.match(r"[A-Za-z0-9_./-]+(?:\s|$)", command):
                raise Fail(f"--allow {rule!r} needs a literal command prefix, e.g. Bash(pytest *)")
    return list(dict.fromkeys(rules))


def effective_request(args, previous):
    inherited = {**DEFAULT_REQUEST, **((previous or {}).get("request") or {})}
    overrides = {}
    for field, argument in (("model", "model"), ("effort", "effort"),
                            ("max_turns", "max_turns"), ("max_budget_usd", "max_budget_usd"),
                            ("timeout_s", "timeout")):
        value = getattr(args, argument)
        if value is not None:
            inherited[field] = overrides[field] = value
    allow = [] if args.clear_allow else list(inherited["allow"])
    if args.allow:
        allow.extend(args.allow)
    inherited["allow"] = check_allow_rules(allow)
    if args.allow is not None or args.clear_allow:
        overrides["allow"] = inherited["allow"]
    request = {key: inherited[key] for key in DEFAULT_REQUEST}
    if request["model"] and not MODEL_RE.fullmatch(request["model"]):
        raise Fail(f"--model {request['model']!r} doesn't look like a model alias or name")
    if request["max_turns"] < 1 or request["timeout_s"] < 1:
        raise Fail("--max-turns and --timeout must be at least 1")
    budget = request["max_budget_usd"]
    if budget is not None and (not math.isfinite(budget) or budget <= 0):
        raise Fail("--max-budget-usd must be a finite number greater than 0")
    request["overrides"] = overrides
    return request


def assert_available(mode, cwd, session_id):
    """Run under state_lock so checking ownership and reserving it are atomic."""
    workspace = git_root(cwd) or cwd.resolve()
    for active in all_records():
        if active["status"] not in ACTIVE:
            continue
        source_session = (active["request"].get("resume_from") or {}).get("session_id")
        same_session = session_id and session_id in (active["claude"].get("session_id"), source_session)
        active_workspace = (Path(active["workspace"]) if active.get("workspace") else
                            git_root(active["cwd"]) or Path(active["cwd"]).resolve())
        same_folder = active_workspace == workspace
        if same_session or (same_folder and "implement" in (mode, active["mode"])):
            raise Fail(f"run {active['run_id']} owns this session or worktree; inspect status or cancel it before continuing")


def cmd_run(args):
    if args.fork and not args.resume:
        raise Fail("--fork needs --resume")
    try:
        brief = sys.stdin.read() if args.brief == "-" else Path(args.brief).expanduser().read_text()
    except OSError as exc:
        raise Fail(f"could not read the brief: {exc}")
    if not brief.strip():
        raise Fail("the brief is empty")
    previous = find_run(args.resume) if args.resume else None
    request = effective_request(args, previous)
    if args.resume:
        mode, cwd, worktree, resume_from = resolve_resume(args)
        session_id = None if args.fork else resume_from["session_id"]
        if args.cwd and resolve_dir(args.cwd) != cwd:
            raise Fail("--cwd differs from the recorded session directory")
    else:
        if not args.mode:
            raise Fail("--mode is required: review or implement")
        mode, worktree, resume_from, session_id = args.mode, None, None, str(uuid.uuid4())
        if mode == "implement" and not args.worktree:
            raise Fail("implement runs need --worktree NAME")
        if mode == "review" and (args.worktree or args.base):
            raise Fail("--worktree and --base only apply to implement runs")
        cwd = resolve_dir(args.cwd)
    binary = claude_bin()
    if not binary:
        raise Fail("claude not found. Install Claude Code and sign in, or set CLAUDE_CREW_BIN.", 127, "claude_unavailable")
    caps = capabilities(binary)
    if not caps["restricted_profile"]:
        raise Fail(f"Claude Code {caps['minimum_version']}+ with restricted/profile flags is required; run claude update", 1, "claude_incompatible")
    request.update(resume_from=resume_from, fork=args.fork, background=args.background)
    notes = []
    # Also reserve review access against an active implementation in that checkout.
    # Independent new worktrees do not conflict with work in the source checkout.
    with state_lock():
        if args.resume or mode == "review":
            assert_available(mode, cwd, (resume_from or {}).get("session_id"))
        if mode == "implement" and not args.resume:
            worktree, notes = create_worktree(cwd, args.worktree, args.base or "HEAD")
            cwd = Path(worktree["path"])
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        run_dir = runs_dir() / run_id
        run_dir.mkdir(parents=True, mode=0o700)
        (run_dir / "brief.md").write_text(brief)
        prior_cost = 0 if not args.resume else None
        if previous and not args.fork and previous["status"] == "completed":
            latest = find_run(previous["claude"]["session_id"])
            if latest and latest["run_id"] == previous["run_id"]:
                prior_cost = previous["claude"].get("session_cost_usd", previous["claude"].get("cost_usd"))
        record = {
            "schema": RECORD_SCHEMA, "run_id": run_id, "status": "queued",
            "mode": mode, "cwd": str(cwd), "workspace": str(git_root(cwd) or cwd), "worktree": worktree,
            "created_at": now(), "started_at": None, "finished_at": None,
            "codex_thread_id": os.environ.get("CODEX_THREAD_ID"), "request": request,
            "claude": {
                "version": caps["version"], "session_id": session_id, "exit_code": None,
                "subtype": None, "is_error": None, "num_turns": None, "duration_ms": None,
                "cost_usd": None, "session_cost_usd": None, "cost_delta_usd": None,
                "models": [], "permission_denials": [], "errors": [], "result_text": None, "transcript": None,
            },
            "cost_baseline_usd": prior_cost, "claimed": None, "report_error": None,
            "observed": {"changed_files": [], "commits": [], "read_only_violation": None},
            "verification": {"verdict": "pending", "checks": [], "notes": None, "at": None},
            "notes": notes, "error": None, "worker_pid": None, "worker": None,
            "launcher": process_identity(os.getpid()), "child": None, "gated_launch": True,
            "files": {"run_dir": str(run_dir), "brief": str(run_dir / "brief.md"),
                      "stdout": str(run_dir / "claude.stdout.json"), "stderr": str(run_dir / "claude.stderr.log")},
        }
        save(record)
        if args.background:
            try:
                with open(run_dir / "worker.log", "ab") as log:
                    worker = subprocess.Popen(
                        [sys.executable, str(Path(__file__).resolve()), "_worker", run_id],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                    )
                record.update(worker_pid=worker.pid, worker=process_identity(worker.pid))
                save(record)  # worker cannot adopt the record until we release the lock
            except OSError as exc:
                record.update(status="error", error=f"could not start worker: {exc}", finished_at=now())
                save(record)
            return emit(record, 1 if record["status"] == "error" else 0)
    record = execute(record)
    return emit(record, {"completed": 0, "claude_unavailable": 127}.get(record["status"], 1))


def cmd_worker(args):
    record = execute(require_run(args.run_id))
    return 0 if record["status"] == "completed" else 1


def cmd_status(args):
    record = require_run(args.run)
    deadline = time.monotonic() + max(args.wait, 0)
    while True:
        record = refresh(record)
        if record["status"] not in ACTIVE or time.monotonic() >= deadline:
            return emit(record)
        time.sleep(2)


def cmd_list(args):
    thread = os.environ.get("CODEX_THREAD_ID")
    if args.thread and not thread:
        raise Fail("CODEX_THREAD_ID isn't set, so there's no thread to filter by")
    rows = []
    for record in all_records():
        if args.thread and record.get("codex_thread_id") != thread:
            continue
        record = refresh(record)
        rows.append({
            "run_id": record["run_id"],
            "created_at": record["created_at"],
            "mode": record["mode"],
            "status": record["status"],
            "cwd": record["cwd"],
            "session_id": record["claude"]["session_id"],
            "claimed": (record["claimed"] or {}).get("status"),
            "verdict": record["verification"]["verdict"],
        })
        if len(rows) >= args.limit:
            break
    return emit(rows)


def cmd_verify(args):
    record = refresh(require_run(args.run))
    if record["status"] in ACTIVE:
        raise Fail("the run is still in progress")
    checks = []
    for item in args.check:
        result, sep, command = item.partition("=")
        if not sep or result not in ("passed", "failed") or not command.strip():
            raise Fail(f"--check {item!r} should look like 'passed=npm test' or 'failed=npm test'")
        checks.append({"command": command.strip(), "status": result})
    with state_lock():
        record = load(record["run_id"])
        if record["status"] in ACTIVE:
            raise Fail("the run is still in progress")
        assert_available(record["mode"], Path(record["cwd"]), record["claude"]["session_id"])
        record["verification"] = {"verdict": args.verdict, "checks": checks, "notes": args.note, "at": now()}
        save(record)
    return emit(record)


def cmd_cancel(args):
    record = refresh(require_run(args.run))
    if record["status"] not in ACTIVE:
        return emit(record)
    if same_process(record.get("worker")):
        try:
            os.kill(record["worker"]["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        record = refresh(record)
        if record["status"] not in ACTIVE or record["status"] == "orphaned":
            break
        time.sleep(1)
    return emit(record, 1 if record["status"] in ACTIVE else 0)


def build_parser():
    parser = argparse.ArgumentParser(prog="relay.py", description="Run bounded tasks in a local Claude Code session for Codex.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("preflight", help="check the claude CLI, its version and sign-in without a model call")

    run = sub.add_parser("run", help="start a review or implement run")
    run.add_argument("--mode", choices=("review", "implement"))
    run.add_argument("--brief", required=True, help="path to the brief, or - to read it from stdin")
    run.add_argument("--cwd", help="folder to work in (default: the current folder)")
    run.add_argument("--worktree", metavar="NAME", help="implement: create a git worktree on branch crew/NAME")
    run.add_argument("--base", metavar="REF", help="implement: commit the worktree starts from (default: HEAD)")
    run.add_argument("--model", help="model alias or full name, e.g. sonnet, opus, haiku, fable")
    run.add_argument("--effort", choices=EFFORTS)
    run.add_argument("--allow", action="append", default=None, metavar="RULE",
                     help="extra permission rule, e.g. 'Bash(npm test *)' (repeatable)")
    run.add_argument("--clear-allow", action="store_true", help="discard inherited --allow rules before adding new ones")
    run.add_argument("--max-turns", type=int, default=None)
    run.add_argument("--max-budget-usd", type=float)
    run.add_argument("--timeout", type=int, default=None, help="seconds before the relay stops Claude (default: 1800; inherited on resume)")
    run.add_argument("--resume", metavar="RUN_OR_SESSION", help="send the brief as a follow-up in an earlier run's Claude session")
    run.add_argument("--fork", action="store_true", help="with --resume: branch the session instead of continuing it")
    run.add_argument("--background", action="store_true", help="return at once and poll with `status RUN --wait SECONDS`")

    status = sub.add_parser("status", help="print a run record")
    status.add_argument("run", help="run id or Claude session id")
    status.add_argument("--wait", type=int, default=0, metavar="SECONDS", help="wait up to SECONDS for the run to finish")

    listing = sub.add_parser("list", help="list recent runs, newest first")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--thread", action="store_true", help="only runs started from this Codex thread")

    verify = sub.add_parser("verify", help="record Codex's verification, kept apart from Claude's claim")
    verify.add_argument("run", help="run id or Claude session id")
    verify.add_argument("--verdict", required=True, choices=VERDICTS)
    verify.add_argument("--check", action="append", default=[], metavar="RESULT=COMMAND",
                        help="a check Codex ran, e.g. 'passed=npm test' (repeatable)")
    verify.add_argument("--note", help="one line on what was confirmed or what's wrong")

    cancel = sub.add_parser("cancel", help="stop a running run")
    cancel.add_argument("run", help="run id or Claude session id")

    worker = sub.add_parser("_worker")  # internal: runs a --background run
    worker.add_argument("run_id")
    child = sub.add_parser("_exec")  # internal gated exec, before any model can run
    child.add_argument("run_id")
    return parser


COMMANDS = {
    "preflight": cmd_preflight,
    "run": cmd_run,
    "status": cmd_status,
    "list": cmd_list,
    "verify": cmd_verify,
    "cancel": cmd_cancel,
    "_worker": cmd_worker,
    "_exec": cmd_exec,
}


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except Fail as exc:
        return emit({"status": exc.status, "error": str(exc)}, exc.code)
    except (OSError, subprocess.SubprocessError) as exc:
        return emit({"status": "error", "error": str(exc)}, 1)


if __name__ == "__main__":
    sys.exit(main())
