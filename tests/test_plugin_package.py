"""Release archives and relocated installations; all Claude workers are fakes."""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import yaml

from test_runner import ROOT, RelayHarness

spec = importlib.util.spec_from_file_location("crew_package", ROOT / "scripts/package.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class PackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.plugin = self.tmp / "plugin source"
        shutil.copytree(package.PLUGIN, self.plugin)

    def test_all_skills_and_onboarding_references_are_self_contained(self):
        manifest, _ = package.validate(self.plugin)
        package.validate_marketplace(manifest)
        for skill in (self.plugin / "skills").glob("*/SKILL.md"):
            text = skill.read_text()
            metadata = yaml.safe_load(text.split("---", 2)[1])
            self.assertEqual(metadata["name"], skill.parent.name)
            self.assertTrue(1 <= len(metadata["description"]) <= 1024)
            self.assertTrue(1 <= len(metadata["compatibility"]) <= 500)
            for target in re.findall(r"\]\(([^)]+)\)", text):
                if "://" in target:
                    continue
                path = (skill.parent / target).resolve()
                self.assertIn(self.plugin.resolve(), path.parents)
                self.assertTrue(path.is_file(), target)
                self.assertIn(path.relative_to(self.plugin.resolve()).as_posix(), package.FILES)

    def test_archive_is_reproducible_and_excludes_unlisted_files(self):
        (self.plugin / ".env").write_text("PRIVATE=must-not-ship\n")
        (self.plugin / ".DS_Store").write_bytes(b"finder metadata")
        first = package.build(self.plugin, self.tmp / "first")
        for path in self.plugin.rglob("*"):
            if path.is_file():
                os.utime(path, (1800000000, 1800000000))
        second = package.build(self.plugin, self.tmp / "second")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        digest = hashlib.sha256(first.read_bytes()).hexdigest()
        self.assertEqual(first.with_suffix(".zip.sha256").read_text(), f"{digest}  {first.name}\n")
        with zipfile.ZipFile(first) as archive:
            self.assertEqual(set(archive.namelist()), set(package.FILES))
            self.assertIsNone(archive.testzip())
            self.assertIn("plugin.json", archive.namelist())
            for name in archive.namelist():
                self.assertEqual(archive.read(name), (self.plugin / name).read_bytes())
                self.assertFalse(name.startswith("/"))
                self.assertNotIn("..", Path(name).parts)

    def test_symlinked_runtime_cannot_enter_release(self):
        relay = self.plugin / "skills/codex-claude-crew/scripts/relay.py"
        external = self.tmp / "external.py"
        relay.rename(external)
        relay.symlink_to(external)
        with self.assertRaisesRegex(ValueError, "Symlinks"):
            package.build(self.plugin, self.tmp / "output")
        self.assertFalse((self.tmp / "output").exists())

    def test_changed_upstream_runtime_requires_new_provenance(self):
        relay = self.plugin / "skills/codex-claude-crew/scripts/relay.py"
        relay.write_text(relay.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "Upstream content changed"):
            package.build(self.plugin, self.tmp / "output")

    def test_missing_asset_and_escaping_setup_path_are_rejected(self):
        icon = self.plugin / "assets/composer-icon.png"
        icon.unlink()
        with self.assertRaisesRegex(ValueError, "Missing package file"):
            package.validate(self.plugin)
        shutil.copyfile(package.PLUGIN / "assets/composer-icon.png", icon)
        path = self.plugin / "plugin.json"
        manifest = json.loads(path.read_text())
        manifest["extensions"]["com.openai"]["onboardingSkill"] = "./../setup/SKILL.md"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "Invalid package path"):
            package.validate(self.plugin)


class InstalledPackageTests(RelayHarness):
    def setUp(self):
        super().setUp()
        archive = package.build(package.PLUGIN, self.tmp / "releases")
        self.installed = self.tmp / "plugin cache with spaces" / "codex-claude-crew" / "0.1.0"
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(self.installed)

    def installed_command(self, *args, **env):
        entry = self.installed / "skills/codex-claude-crew/scripts/relay.py"
        result = subprocess.run(
            [sys.executable, str(entry), *args], input="Review README.md for typos.",
            cwd=self.repo, env=dict(self.env, **env), capture_output=True, text=True, timeout=30,
        )
        try:
            return result.returncode, json.loads(result.stdout)
        except ValueError:
            self.fail(f"No JSON from installed relay: {result.stdout!r} {result.stderr!r}")

    def test_setup_preflight_from_archive_never_calls_a_model(self):
        code, report = self.installed_command("preflight")
        self.assertEqual(code, 0, report)
        self.assertTrue(report["ok"])
        self.assertFalse(self.log.exists())
        for env in (
            {"CLAUDE_CREW_BIN": str(self.tmp / "missing-claude")},
            {"FAKE_CLAUDE_VERSION": "2.1.201"},
            {"FAKE_CLAUDE_HELP_FAIL": "1"},
            {"FAKE_CLAUDE_LOGGED_OUT": "1"},
        ):
            code, report = self.installed_command("preflight", **env)
            self.assertNotEqual(code, 0)
            self.assertFalse(report["ok"])
            self.assertTrue(report["notes"])
        self.assertFalse(self.log.exists())

    def test_background_run_and_resume_after_cache_relocation(self):
        code, first = self.installed_command("run", "--mode", "review", "--background", "--brief", "-")
        self.assertEqual(code, 0, first)
        self.addCleanup(self.installed_command, "cancel", first["run_id"])
        code, done = self.installed_command("status", first["run_id"], "--wait", "15")
        self.assertEqual(code, 0, done)
        self.assertEqual(done["status"], "completed")
        self.assertNotIn(self.installed, Path(done["files"]["run_dir"]).parents)
        previous = self.installed
        self.installed = self.tmp / "refreshed plugin cache" / "codex-claude-crew" / "local"
        shutil.copytree(previous, self.installed)
        shutil.rmtree(previous)
        code, resumed = self.installed_command("run", "--resume", first["run_id"], "--brief", "-")
        self.assertEqual(code, 0, resumed)
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["claude"]["session_id"], done["claude"]["session_id"])
        self.assertEqual(resumed["verification"]["verdict"], "pending")
        self.assertEqual(self.git("status", "--porcelain"), "")
        code, verified = self.installed_command(
            "verify", resumed["run_id"], "--verdict", "confirmed", "--check", "passed=git status --porcelain",
        )
        self.assertEqual(code, 0, verified)
        self.assertEqual(verified["verification"]["verdict"], "confirmed")
        self.assertEqual(len(self.calls()), 2)
