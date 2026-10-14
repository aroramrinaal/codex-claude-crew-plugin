"""Failure-oriented relay tests. All workers are local fakes; no model calls."""

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from test_runner import RELAY, RelayHarness

spec = importlib.util.spec_from_file_location('crew_relay', RELAY)
relay_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay_module)


class RegressionTests(RelayHarness):
    def wait_for(self, condition):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = condition()
            if result:
                return result
            time.sleep(.05)
        self.fail('condition did not become true within 10s')

    def background(self, mode='review', **env):
        args = ['run', '--mode', mode, '--background', '--brief', '-']
        if mode == 'implement':
            args += ['--worktree', 'busy']
        code, record = self.relay(*args, FAKE_CLAUDE_SCENARIO='sleep', **env)
        self.assertEqual(code, 0, record)
        self.addCleanup(self.stop_test_run, record)
        self.wait_for(lambda: self.log.exists() and self.log.stat().st_size)
        return record

    def stop_test_run(self, record):
        # Even a failing assertion must not leave a fake process behind.
        path = Path(record['files']['run_dir']) / 'run.json'
        if path.exists():
            record = json.loads(path.read_text())
            for identity in (record.get('child'), record.get('worker')):
                if identity and relay_module.same_process(identity):
                    try:
                        os.killpg(identity['pid'], signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_old_cli_is_rejected_before_creating_worktree(self):
        code, result = self.relay('run', '--mode', 'implement', '--worktree', 'unsupported', '--brief', '-',
                                  FAKE_CLAUDE_VERSION='2.1.201')
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'claude_incompatible')
        self.assertFalse(self.log.exists())
        self.assertNotIn('crew/unsupported', self.git('branch', '--list'))
        code, result = self.relay('preflight', FAKE_CLAUDE_VERSION='2.1.201')
        self.assertEqual(code, 1)
        self.assertFalse(result['ok'])
        self.assertFalse(result['capabilities']['restricted_profile'])

    def test_new_version_with_missing_boundary_flag_is_rejected(self):
        code, result = self.relay('run', '--mode', 'review', '--brief', '-', FAKE_CLAUDE_HELP_FAIL='1')
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'claude_incompatible')
        self.assertFalse(self.log.exists())

    def test_restricted_profile_on_new_and_resumed_runs(self):
        _, first = self.relay('run', '--mode', 'review', '--brief', '-')
        self.relay('run', '--resume', first['run_id'], '--brief', '-')
        for call in self.calls():
            argv = call['argv']
            self.assertIn('--restricted', argv)
            self.assertIn('--disable-slash-commands', argv)
            self.assertIn('--no-chrome', argv)
            self.assertIn('--strict-mcp-config', argv)
            self.assertEqual(json.loads(self.flag(argv, '--mcp-config')), {'mcpServers': {}})
            self.assertEqual(self.flag(argv, '--permission-prompts'), 'none')
            self.assertNotIn('--bare', argv)  # retain supported login, not ambient configuration
            self.assertNotIn('--dangerously-skip-permissions', argv)

    def test_wildcards_and_rule_injection_are_rejected(self):
        for rule in ('Bash', 'Bash(*)', 'Bash( ** )', 'Bash( )', 'Bash(:*)', 'Bash(* *)',
                     'Bash(git status),Bash(*)', 'WebFetch(domain:example.com),Bash(*)', 'Bash(git\nstatus)'):
            with self.subTest(rule=rule):
                code, _ = self.relay('run', '--mode', 'review', '--allow', rule, '--brief', '-')
                self.assertEqual(code, 2)
        self.assertFalse(self.log.exists())

    def test_specific_rules_still_work(self):
        rules = ['Bash(pytest *)', 'Bash(npm test *)', 'Bash(/usr/bin/git status)', 'WebSearch']
        args = [part for rule in rules for part in ('--allow', rule)]
        code, _ = self.relay('run', '--mode', 'review', *args, '--brief', '-')
        self.assertEqual(code, 0)
        for rule in rules:
            self.assertIn(rule, self.calls()[0]['argv'])

    def test_clean_commit_is_detected_in_review(self):
        before = self.git('rev-parse', 'HEAD').strip()
        code, record = self.relay('run', '--mode', 'review', '--brief', '-', FAKE_CLAUDE_SCENARIO='commit')
        self.assertEqual(code, 0)
        observed = record['observed']
        self.assertTrue(observed['read_only_violation'])
        self.assertTrue(observed['head_changed'])
        self.assertEqual(observed['head_before'], before)
        self.assertNotEqual(observed['head_after'], before)
        self.assertEqual(observed['changed_files'], ['README.md'])
        self.assertEqual(len(observed['commits']), 1)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_already_dirty_content_change_is_named(self):
        (self.repo / 'README.md').write_text('dirty before\n')
        code, record = self.relay('run', '--mode', 'review', '--brief', '-', FAKE_CLAUDE_SCENARIO='tracked_edit')
        self.assertEqual(code, 0)
        self.assertTrue(record['observed']['read_only_violation'])
        self.assertEqual(record['observed']['changed_files'], ['README.md'])

    def test_index_changes_and_preserved_mtime_untracked_edits_are_seen(self):
        target = self.repo / 'extra.txt'
        target.write_text('before')
        before = relay_module.fingerprint(self.repo)
        old_stat = target.stat()
        target.write_text('after!')
        os.utime(target, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        self.git('add', 'README.md')
        record = {'mode': 'review', 'observed': {}}
        relay_module.observe(record, self.repo, before)
        self.assertTrue(record['observed']['read_only_violation'])
        self.assertIn('extra.txt', record['observed']['changed_files'])
        (self.repo / 'README.md').write_text('stage me\n')
        before = relay_module.fingerprint(self.repo)
        self.git('add', 'README.md')
        relay_module.observe(record, self.repo, before)
        self.assertIn('README.md', record['observed']['changed_files'])

    def test_unreadable_fingerprint_cannot_report_unchanged(self):
        with patch.object(Path, 'open', side_effect=PermissionError('unreadable')):
            before = relay_module.fingerprint(self.repo)
            record = {'mode': 'review', 'observed': {}}
            relay_module.observe(record, self.repo, before)
        self.assertFalse(record['observed']['coverage_complete'])
        self.assertIsNone(record['observed']['read_only_violation'])

    def test_resume_inherits_budget_rules_and_limits_with_explicit_overrides(self):
        _, first = self.relay('run', '--mode', 'review', '--model', 'sonnet', '--effort', 'high',
                              '--max-turns', '7', '--timeout', '60', '--max-budget-usd', '1',
                              '--allow', 'Bash(pytest *)', '--brief', '-')
        _, second = self.relay('run', '--resume', first['run_id'], '--brief', '-')
        for key in relay_module.DEFAULT_REQUEST:
            self.assertEqual(first['request'][key], second['request'][key])
        self.assertEqual(second['request']['overrides'], {})
        argv = self.calls()[1]['argv']
        self.assertEqual(self.flag(argv, '--max-budget-usd'), '1.0')
        self.assertEqual(self.flag(argv, '--model'), 'sonnet')
        _, third = self.relay('run', '--resume', second['run_id'], '--max-turns', '9',
                              '--allow', 'Bash(npm test *)', '--brief', '-')
        self.assertEqual(third['request']['max_turns'], 9)
        self.assertEqual(third['request']['allow'], ['Bash(pytest *)', 'Bash(npm test *)'])
        _, fourth = self.relay('run', '--resume', third['run_id'], '--clear-allow', '--brief', '-')
        self.assertEqual(fourth['request']['allow'], [])
        self.assertEqual(fourth['request']['max_budget_usd'], 1)

    def test_active_implementation_rejects_resume_fork_and_shared_review(self):
        first = self.background(mode='implement')
        for flags in ([], ['--fork']):
            code, result = self.relay('run', '--resume', first['run_id'], *flags, '--brief', '-')
            self.assertEqual(code, 2, result)
        code, _ = self.relay('run', '--mode', 'review', '--cwd', first['cwd'], '--brief', '-')
        self.assertEqual(code, 2)
        nested = Path(first['cwd']) / 'nested'
        nested.mkdir()
        code, _ = self.relay('run', '--mode', 'review', '--cwd', str(nested), '--brief', '-')
        self.assertEqual(code, 2)
        code, other = self.relay('run', '--mode', 'implement', '--worktree', 'independent', '--brief', '-')
        self.assertEqual(code, 0, other)
        self.assertNotEqual(first['cwd'], other['cwd'])
        code, stopped = self.relay('cancel', first['run_id'])
        self.assertEqual(code, 0, stopped)
        self.assertEqual(stopped['status'], 'interrupted')
        code, resumed = self.relay('run', '--resume', first['run_id'], '--fork', '--brief', '-')
        self.assertEqual(code, 0, resumed)

    def test_two_simultaneous_resumes_only_one_can_reserve_session(self):
        _, first = self.relay('run', '--mode', 'review', '--brief', '-')
        procs = [subprocess.Popen([sys.executable, str(RELAY), 'run', '--resume', first['run_id'],
                                  '--background', '--brief', '-'], cwd=self.repo,
                                 env=dict(self.env, FAKE_CLAUDE_SCENARIO='sleep'),
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(2)]
        for proc in procs:
            proc.stdin.write('Continue reviewing.'); proc.stdin.close(); proc.stdin = None
        results = [(proc, json.loads(proc.communicate(timeout=10)[0])) for proc in procs]
        for proc, result in results:
            if proc.returncode == 0:
                self.addCleanup(self.stop_test_run, result)
        self.assertEqual(sorted(proc.returncode for proc in procs), [0, 2])
        for proc, result in results:
            if proc.returncode == 0:
                self.relay('cancel', result['run_id'])

    def test_descendant_stops_even_when_leader_has_exited(self):
        code, record = self.relay('run', '--mode', 'review', '--brief', '-', FAKE_CLAUDE_SCENARIO='descendant')
        self.assertEqual(code, 0, record)
        self.assertFalse(relay_module.group_members(record['child']['pid']))
        self.assertIsNotNone(record['finished_at'])

    def test_worker_crash_reaps_live_claude_before_releasing_reservation(self):
        first = self.background(mode='implement')
        os.kill(first['worker_pid'], signal.SIGKILL)
        self.wait_for(lambda: not relay_module.same_process(first['worker']))
        _, record = self.relay('status', first['run_id'])
        self.assertEqual(record['status'], 'error', record)
        self.assertFalse(relay_module.group_members(record['child']['pid']))
        self.assertIn('execution has stopped', record['error'])
        code, _ = self.relay('run', '--resume', first['run_id'], '--brief', '-')
        self.assertEqual(code, 0)

    def test_unidentifiable_group_stays_reserved(self):
        first = self.background(mode='implement')
        os.kill(first['worker_pid'], signal.SIGKILL)
        self.wait_for(lambda: not relay_module.same_process(first['worker']))
        with patch.dict(os.environ, self.env):
            with patch.object(relay_module, 'stop_group', return_value=False):
                result = relay_module.refresh(first)
        self.assertEqual(result['status'], 'orphaned')
        self.assertIsNone(result['finished_at'])
        code, _ = self.relay('run', '--resume', first['run_id'], '--brief', '-')
        self.assertEqual(code, 2)
        # Restore real cleanup after asserting that an uncertain stop is not success.
        self.relay('status', first['run_id'])

    def test_reused_process_identity_is_never_signalled(self):
        current = relay_module.process_identity(os.getpid())
        wrong = dict(current, started='not this process')
        with patch.object(relay_module, 'group_members', return_value={os.getpid(): current}):
            with patch.object(relay_module.os, 'killpg') as kill:
                self.assertFalse(relay_module.stop_group(wrong))
                kill.assert_not_called()

    def test_startup_record_persists_worker_and_recovers_dead_queue(self):
        # Hold the CLI version probe only in execute(), after background dispatch.
        # The fake counts probes so the dispatcher's initial capability check passes.
        shim = self.tmp / 'slow-version'
        counter = self.tmp / 'version-count'
        marker = self.tmp / 'version-worker'
        original = self.env['CLAUDE_CREW_BIN']
        shim.write_text('#!' + sys.executable + '\n' +
                        'import os,sys,time\nfrom pathlib import Path\n' +
                        f'counter=Path({str(counter)!r})\n' +
                        'if sys.argv[1:]==["--version"]:\n' +
                        ' n=int(counter.read_text())+1 if counter.exists() else 1\n counter.write_text(str(n))\n' +
                        f' if n==2:\n  Path({str(marker)!r}).write_text(str(os.getpid()))\n  time.sleep(60)\n' +
                        f'os.execv({original!r},[{original!r},*sys.argv[1:]])\n')
        shim.chmod(0o755)
        code, first = self.relay('run', '--mode', 'review', '--background', '--brief', '-', CLAUDE_CREW_BIN=str(shim))
        self.assertEqual(code, 0)
        self.addCleanup(self.stop_test_run, first)
        self.wait_for(marker.exists)
        probe_pid = int(marker.read_text())
        try:
            stored = json.loads((Path(first['files']['run_dir']) / 'run.json').read_text())
            self.assertEqual(stored['worker_pid'], first['worker_pid'])
            self.assertIsNotNone(stored['worker'])
            os.kill(first['worker_pid'], signal.SIGKILL)
            self.wait_for(lambda: not relay_module.same_process(first['worker']))
            _, final = self.relay('status', first['run_id'])
            self.assertEqual(final['status'], 'error')
            self.assertIsNotNone(final['finished_at'])
            self.assertFalse(self.log.exists())
        finally:
            try:
                os.kill(probe_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_gated_child_without_release_never_invokes_claude(self):
        _, finished = self.relay('run', '--mode', 'review', '--brief', '-')
        calls = len(self.calls())
        proc = subprocess.run([sys.executable, str(RELAY), '_exec', finished['run_id']],
                              input='', capture_output=True, text=True, env=self.env, timeout=5)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(len(self.calls()), calls)

    def test_success_without_valid_report_is_a_report_failure(self):
        for scenario in ('missing_report', 'invalid_report', 'invalid_nested_report'):
            with self.subTest(scenario=scenario):
                code, record = self.relay('run', '--mode', 'review', '--brief', '-', FAKE_CLAUDE_SCENARIO=scenario)
                self.assertEqual(code, 1)
                self.assertEqual(record['status'], 'error')
                self.assertEqual(record['claude']['exit_code'], 0)
                self.assertIsNone(record['claimed'])
                self.assertIsNotNone(record['report_error'])
                self.assertIn('structured report', record['error'])

    def test_session_cost_and_delta_are_distinct(self):
        _, first = self.relay('run', '--mode', 'review', '--brief', '-', FAKE_CLAUDE_COST='1')
        _, second = self.relay('run', '--resume', first['run_id'], '--brief', '-', FAKE_CLAUDE_COST='1.25')
        self.assertEqual(first['claude']['session_cost_usd'], 1)
        self.assertEqual(first['claude']['cost_delta_usd'], 1)
        self.assertEqual(second['claude']['session_cost_usd'], 1.25)
        self.assertEqual(second['claude']['cost_delta_usd'], .25)
        _, forked = self.relay('run', '--resume', second['run_id'], '--fork', '--brief', '-')
        self.assertIsNone(forked['claude']['cost_delta_usd'])
        _, stale = self.relay('run', '--resume', first['run_id'], '--brief', '-', FAKE_CLAUDE_COST='2')
        self.assertIsNone(stale['claude']['cost_delta_usd'])

    def test_preflight_reports_storage_auth_and_capabilities(self):
        code, report = self.relay('preflight')
        self.assertEqual(code, 0)
        self.assertTrue(report['state_writable'])
        self.assertTrue(report['process_inspection'])
        self.assertTrue(report['capabilities']['restricted_profile'])
        code, report = self.relay('preflight', FAKE_CLAUDE_LOGGED_OUT='1')
        self.assertEqual(code, 1)
        self.assertFalse(report['ok'])
        self.assertNotIn('someone@example.com', json.dumps(report))
        not_a_dir = self.tmp / 'file'
        not_a_dir.write_text('not a directory')
        code, report = self.relay('preflight', CLAUDE_CREW_HOME=str(not_a_dir))
        self.assertEqual(code, 1)
        self.assertFalse(report['state_writable'])

    def test_nonfinite_budget_is_rejected(self):
        for amount in ('nan', 'inf', '-inf', '0'):
            code, _ = self.relay('run', '--mode', 'review', '--max-budget-usd=' + amount, '--brief', '-')
            self.assertEqual(code, 2)
