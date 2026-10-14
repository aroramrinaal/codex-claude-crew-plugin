"""Tests for scripts/relay.py. A fake `claude` stands in for Claude Code, so no model is called.

Run with: python3 -m unittest discover -s tests
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELAY = ROOT / "plugins" / "codex-claude-crew" / "skills" / "codex-claude-crew" / "scripts" / "relay.py"
FAKE_CLAUDE = ROOT / "tests" / "fixtures" / "fake_claude.py"


class RelayHarness(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        shim = self.tmp / "claude"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLAUDE}" "$@"\n')
        shim.chmod(0o755)
        self.log = self.tmp / "claude-calls.jsonl"
        self.env = dict(
            os.environ,
            CLAUDE_CREW_HOME=str(self.tmp / "crew"),
            CLAUDE_CREW_BIN=str(shim),
            FAKE_CLAUDE_LOG=str(self.log),
        )
        self.env.pop("CODEX_THREAD_ID", None)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / "README.md").write_text("hello\n")
        self.git("add", "README.md")
        self.git("-c", "user.name=Crew Test", "-c", "user.email=crew@example.com",
                 "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True).stdout

    def relay(self, *args, brief="Review README.md for typos.", **env):
        proc = subprocess.run(
            [sys.executable, str(RELAY), *args],
            input=brief, capture_output=True, text=True, cwd=self.repo, env=dict(self.env, **env), timeout=120,
        )
        try:
            return proc.returncode, json.loads(proc.stdout)
        except ValueError:
            self.fail(f"relay printed no JSON (exit {proc.returncode}): {proc.stdout!r} {proc.stderr!r}")

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    @staticmethod
    def flag(argv, name):
        return argv[argv.index(name) + 1]


class RelayTests(RelayHarness):
    def test_preflight_reports_version_and_hides_account_details(self):
        code, report = self.relay("preflight")
        self.assertEqual(code, 0, report)
        self.assertTrue(report["ok"])
        self.assertEqual(report["version"], "2.1.300")
        self.assertIs(report["auth"]["loggedIn"], True)
        self.assertNotIn("someone@example.com", json.dumps(report))

    def test_review_run_is_read_only_and_keeps_the_session_id(self):
        code, record = self.relay("run", "--mode", "review", "--brief", "-")
        self.assertEqual(code, 0, record)
        self.assertEqual(record["status"], "completed")
        call = self.calls()[0]
        self.assertEqual(call["stdin"], "Review README.md for typos.")
        self.assertEqual(self.flag(call["argv"], "--permission-mode"), "dontAsk")
        self.assertNotIn("Edit", self.flag(call["argv"], "--tools").split(","))
        self.assertEqual(self.flag(call["argv"], "--permission-prompts"), "none")
        self.assertEqual(record["claude"]["session_id"], self.flag(call["argv"], "--session-id"))
        self.assertEqual(record["claude"]["models"], ["claude-sonnet-5-5"])
        self.assertEqual(record["claude"]["permission_denials"], [{"tool": "Bash", "input": "npm install"}])
        self.assertEqual(record["claimed"]["status"], "complete")
        self.assertIs(record["observed"]["read_only_violation"], False)
        self.assertEqual(record["verification"]["verdict"], "pending")

    def test_review_flags_files_changed_during_the_run(self):
        code, record = self.relay("run", "--mode", "review", "--brief", "-", FAKE_CLAUDE_SCENARIO="edit")
        self.assertEqual(code, 0, record)
        self.assertIs(record["observed"]["read_only_violation"], True)
        self.assertEqual(record["observed"]["changed_files"], ["crew-was-here.txt"])

    def test_implement_runs_in_a_new_worktree(self):
        code, record = self.relay(
            "run", "--mode", "implement", "--worktree", "fix-typo", "--allow", "Bash(pytest *)", "--brief", "-",
            FAKE_CLAUDE_SCENARIO="edit",
        )
        self.assertEqual(code, 0, record)
        worktree = record["worktree"]
        self.assertEqual(worktree["branch"], "crew/fix-typo")
        self.assertEqual(worktree["base"], self.git("rev-parse", "HEAD").strip())
        call = self.calls()[0]
        self.assertEqual(Path(call["cwd"]).resolve(), Path(worktree["path"]).resolve())
        self.assertEqual(self.flag(call["argv"], "--permission-mode"), "acceptEdits")
        self.assertIn("Bash(pytest *)", call["argv"])
        self.assertEqual(record["observed"]["changed_files"], ["crew-was-here.txt"])
        self.assertEqual(record["observed"]["commits"], [])
        self.assertFalse((self.repo / "crew-was-here.txt").exists())

    def test_requests_that_break_the_contract_are_refused_before_claude_runs(self):
        cases = [
            ["--mode", "implement"],
            ["--mode", "review", "--worktree", "x"],
            ["--mode", "review", "--allow", "Bash"],
            ["--mode", "review", "--allow", "Edit"],
            ["--mode", "review", "--model", "sonnet; rm -rf ~"],
            ["--mode", "review", "--fork"],
        ]
        for args in cases:
            code, record = self.relay("run", "--brief", "-", *args)
            self.assertEqual(code, 2, args)
            self.assertEqual(record["status"], "usage_error", args)
        self.assertFalse(self.log.exists())

    def test_resume_sends_a_follow_up_to_the_same_session(self):
        _, first = self.relay("run", "--mode", "review", "--brief", "-")
        code, second = self.relay("run", "--resume", first["run_id"], "--brief", "-", brief="Now check docs/ too.")
        self.assertEqual(code, 0, second)
        argv = self.calls()[1]["argv"]
        self.assertEqual(self.flag(argv, "--resume"), first["claude"]["session_id"])
        self.assertNotIn("--session-id", argv)
        self.assertEqual(second["claude"]["session_id"], first["claude"]["session_id"])
        self.assertEqual(second["request"]["resume_from"]["run_id"], first["run_id"])
        self.assertEqual(second["cwd"], first["cwd"])
        # A session id finds the newest run of that session.
        _, third = self.relay("run", "--resume", first["claude"]["session_id"], "--brief", "-", brief="Summarize.")
        self.assertEqual(third["request"]["resume_from"]["run_id"], second["run_id"])

    def test_fork_starts_a_new_session(self):
        _, first = self.relay("run", "--mode", "review", "--brief", "-")
        code, forked = self.relay("run", "--resume", first["run_id"], "--fork", "--brief", "-", brief="Try another angle.")
        self.assertEqual(code, 0, forked)
        self.assertIn("--fork-session", self.calls()[1]["argv"])
        self.assertNotEqual(forked["claude"]["session_id"], first["claude"]["session_id"])

    def test_error_result_is_reported(self):
        code, record = self.relay("run", "--mode", "review", "--brief", "-", FAKE_CLAUDE_SCENARIO="error")
        self.assertEqual(code, 1)
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["claude"]["subtype"], "error_max_turns")
        self.assertIn("Reached max turns", record["error"])

    def test_output_that_isnt_json_is_an_error(self):
        code, record = self.relay("run", "--mode", "review", "--brief", "-", FAKE_CLAUDE_SCENARIO="garbage")
        self.assertEqual(code, 1)
        self.assertEqual(record["status"], "error")
        self.assertIn("claude update", record["error"])

    def test_timeout_stops_claude_and_keeps_the_session_id(self):
        started = time.monotonic()
        code, record = self.relay(
            "run", "--mode", "review", "--timeout", "1", "--brief", "-", FAKE_CLAUDE_SCENARIO="sleep",
        )
        self.assertEqual(code, 1)
        self.assertEqual(record["status"], "timed_out")
        self.assertLess(time.monotonic() - started, 30)
        self.assertTrue(record["claude"]["session_id"])

    def test_missing_claude(self):
        code, record = self.relay("run", "--mode", "review", "--brief", "-", CLAUDE_CREW_BIN=str(self.tmp / "nope"))
        self.assertEqual(code, 127)
        self.assertEqual(record["status"], "claude_unavailable")

    def test_verification_is_recorded_apart_from_claudes_claim(self):
        _, record = self.relay("run", "--mode", "review", "--brief", "-")
        code, verified = self.relay(
            "verify", record["run_id"], "--verdict", "rejected", "--check", "failed=pytest -q", "--note", "Finding 1 is wrong.",
        )
        self.assertEqual(code, 0, verified)
        self.assertEqual(verified["verification"]["verdict"], "rejected")
        self.assertEqual(verified["verification"]["checks"], [{"command": "pytest -q", "status": "failed"}])
        self.assertEqual(verified["claimed"]["status"], "complete")

    def test_background_run_can_be_polled_and_listed(self):
        code, record = self.relay("run", "--mode", "review", "--background", "--brief", "-")
        self.assertEqual(code, 0, record)
        self.assertIn(record["status"], ("queued", "running"))
        _, done = self.relay("status", record["run_id"], "--wait", "60")
        self.assertEqual(done["status"], "completed", done)
        _, runs = self.relay("list")
        self.assertEqual(runs[0]["run_id"], record["run_id"])
        self.assertEqual(runs[0]["session_id"], done["claude"]["session_id"])

    def test_permission_prompts_are_disabled_on_supported_claude(self):
        self.relay("run", "--mode", "review", "--brief", "-", FAKE_CLAUDE_VERSION="2.1.300")
        self.assertEqual(self.flag(self.calls()[0]["argv"], "--permission-prompts"), "none")


if __name__ == "__main__":
    unittest.main()
