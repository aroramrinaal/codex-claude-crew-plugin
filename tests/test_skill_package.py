"""Validate the distributable skill, including paths after copying it elsewhere."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from test_runner import ROOT, RelayHarness

SKILL = ROOT / 'plugins' / 'codex-claude-crew' / 'skills' / 'codex-claude-crew'


class SkillPackageTests(RelayHarness):
    def test_frontmatter_and_ui_metadata(self):
        text = (SKILL / 'SKILL.md').read_text()
        self.assertTrue(text.startswith('---\n'))
        frontmatter = yaml.safe_load(text.split('---', 2)[1])
        # Agent Skills permits compatibility; older bundled validators omit it.
        self.assertFalse(set(frontmatter) - {'name', 'description', 'license', 'compatibility', 'metadata', 'allowed-tools'})
        name = frontmatter['name']
        self.assertEqual(name, SKILL.name)
        self.assertRegex(name, r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
        self.assertLessEqual(len(name), 64)
        self.assertTrue(1 <= len(frontmatter['description']) <= 1024)
        self.assertTrue(1 <= len(frontmatter['compatibility']) <= 500)
        self.assertTrue(all(isinstance(v, str) for v in frontmatter['metadata'].values()))
        ui = yaml.safe_load((SKILL / 'agents' / 'openai.yaml').read_text())
        self.assertTrue(ui['interface']['display_name'])
        self.assertTrue(25 <= len(ui['interface']['short_description']) <= 64)
        self.assertIn('$' + name, ui['interface']['default_prompt'])
        self.assertIsInstance(ui['policy']['allow_implicit_invocation'], bool)
        for key in ('icon_small', 'icon_large'):
            if key in ui['interface']:
                path = (SKILL / ui['interface'][key]).resolve()
                self.assertIn(SKILL.resolve(), path.parents)
                self.assertTrue(path.is_file())
        for target in re.findall(r'\]\((references/[^)]+)\)', text):
            path = (SKILL / target).resolve()
            self.assertIn(SKILL.resolve(), path.parents)
            self.assertTrue(path.is_file(), target)

    def test_copied_skill_runs_background_without_repository_resources(self):
        copied = self.tmp / 'installed skill with spaces'
        shutil.copytree(SKILL, copied, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        entry = copied / 'scripts' / 'relay.py'
        proc = subprocess.run([sys.executable, str(entry), 'run', '--mode', 'review', '--background', '--brief', '-'],
                              input='Review README.md.', cwd=self.repo, env=self.env,
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        run = json.loads(proc.stdout)
        proc = subprocess.run([sys.executable, str(entry), 'status', run['run_id'], '--wait', '15'],
                              cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['status'], 'completed')
