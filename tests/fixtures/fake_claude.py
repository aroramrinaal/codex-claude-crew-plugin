"""Stand-in for the `claude` CLI, so relay tests never call a model.

FAKE_CLAUDE_SCENARIO picks the behavior: success (default), edit, error, garbage or sleep.
FAKE_CLAUDE_VERSION sets the reported version. Each run appends its argv, cwd and stdin
to the JSON-lines file named by FAKE_CLAUDE_LOG.
"""

import json
import os
import subprocess
import sys
import time
import uuid


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def main():
    argv = sys.argv[1:]
    if argv[:1] == ["--version"]:
        print(os.environ.get("FAKE_CLAUDE_VERSION", "2.1.300") + " (Claude Code)")
        return 0
    if "--help" in argv:
        if os.environ.get("FAKE_CLAUDE_HELP_FAIL"):
            print("error: unknown option '--restricted'", file=sys.stderr)
            return 1
        print("Fake Claude CLI help")
        return 0
    if argv[:2] == ["auth", "status"]:
        print(json.dumps({
            "loggedIn": not bool(os.environ.get("FAKE_CLAUDE_LOGGED_OUT")),
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "subscriptionType": "max",
            "email": "someone@example.com",
        }))
        return 0

    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "success")
    stdin = sys.stdin.read()
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
        log.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "stdin": stdin,
                              "pid": os.getpid(), "pgid": os.getpgrp()}) + "\n")
    session_id = flag(argv, "--session-id") or flag(argv, "--resume")
    if "--fork-session" in argv:
        session_id = str(uuid.uuid4())

    if scenario == "sleep":
        time.sleep(60)
        return 0
    if scenario == "descendant":
        marker = os.environ["FAKE_CLAUDE_LOG"] + ".descendant"
        code = ("import os,signal,time; "
                "signal.signal(signal.SIGINT,signal.SIG_IGN); "
                "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                f"open({marker!r},'w').write(str(os.getpid())); "
                "time.sleep(60)")
        subprocess.Popen([sys.executable, "-c", code])
        while not os.path.exists(marker):
            time.sleep(.01)
    if scenario == "garbage":
        print("not json")
        print("error: unknown option '--json-schema'", file=sys.stderr)
        return 1
    if scenario == "error":
        print(json.dumps({
            "type": "result",
            "subtype": "error_max_turns",
            "is_error": True,
            "session_id": session_id,
            "num_turns": 50,
            "total_cost_usd": 0.5,
            "modelUsage": {},
            "permission_denials": [],
            "errors": ["Reached max turns (50)"],
        }))
        return 1
    if scenario == "edit":
        with open("crew-was-here.txt", "w") as fh:
            fh.write("hello from the fake claude\n")
    if scenario in ("tracked_edit", "commit"):
        with open("README.md", "w") as fh:
            fh.write("modified by the fake worker\n")
        if scenario == "commit":
            subprocess.run(["git", "add", "README.md"], check=True)
            subprocess.run(["git", "-c", "user.name=Fake", "-c", "user.email=fake@example.com",
                            "-c", "commit.gpgsign=false", "commit", "-qm", "unexpected commit"], check=True)

    report = {
        "status": "complete",
        "summary": "Did the thing.",
        "findings": [],
        "changed_files": ["crew-was-here.txt"] if scenario == "edit" else [],
        "checks": [{"command": "pytest -q", "status": "passed"}],
        "open_issues": [],
    }
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": session_id,
        "num_turns": 3,
        "duration_ms": 1200,
        "total_cost_usd": float(os.environ.get("FAKE_CLAUDE_COST", "0.12")),
        "result": "Did the thing.",
        "modelUsage": {"claude-sonnet-5-5": {}},
        "permission_denials": [{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "npm install"}}],
        "structured_output": report,
    }
    if scenario == "missing_report":
        envelope.pop("structured_output")
    if scenario == "invalid_report":
        envelope["structured_output"] = {"status": "complete"}
    if scenario == "invalid_nested_report":
        envelope["structured_output"]["findings"] = [{"severity": "high", "title": "bad", "detail": "bad", "line": True}]
    print(json.dumps(envelope))
    return 0


if __name__ == "__main__":
    sys.exit(main())
