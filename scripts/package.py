#!/usr/bin/env python3
"""Check the plugin or create a reproducible release ZIP without running Claude."""

import argparse
import hashlib
import json
import re
import stat
import struct
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "codex-claude-crew"
FILES = (
    "LICENSE",
    "plugin.json",
    "upstream.json",
    "assets/composer-icon.png",
    "assets/logo.png",
    "skills/codex-claude-crew/SKILL.md",
    "skills/codex-claude-crew/agents/openai.yaml",
    "skills/codex-claude-crew/assets/icon-small.png",
    "skills/codex-claude-crew/assets/icon-large.png",
    "skills/codex-claude-crew/references/task-contract.md",
    "skills/codex-claude-crew/scripts/relay.py",
    "skills/setup/SKILL.md",
)
VENDORED = set(FILES) - {"plugin.json", "upstream.json", "skills/setup/SKILL.md"}
SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def payload(plugin):
    result = {}
    for name in FILES:
        path = plugin
        for part in Path(name).parts:
            path = path / part
            require(not path.is_symlink(), f"Symlinks are not distributable: {name}")
        require(path.is_file(), f"Missing package file: {name}")
        result[name] = path.read_bytes()
    return result


def package_reference(value, data):
    require(isinstance(value, str) and value.startswith("./"), f"Invalid package path: {value}")
    relative = value[2:]
    require(all(part not in ("", ".", "..") for part in relative.split("/")), f"Invalid package path: {value}")
    require(relative in data, f"Reference not included in package: {value}")
    return relative


def validate(plugin=PLUGIN):
    data = payload(plugin)
    manifest = json.loads(data["plugin.json"])
    require(manifest.get("$schema") == SCHEMA, "Expected the portable Agent Plugins manifest")
    require(manifest.get("name") == "codex-claude-crew", "Unexpected plugin identity")
    version = manifest.get("version", "")
    require(re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version), "Use a stable major.minor.patch version")
    require(manifest.get("license") == "MIT", "Keep the upstream MIT license")
    require(manifest.get("author", {}).get("name"), "Publisher name is required")
    require(1 <= len(manifest.get("description", "")) <= 4000, "Invalid plugin description")
    extension = manifest.get("extensions", {}).get("com.openai", {})
    require(extension.get("requires_local_executor") is True, "Local execution must be declared")
    setup = package_reference(extension.get("onboardingSkill"), data)
    require(setup == "skills/setup/SKILL.md", "Onboarding must use the bundled setup skill")
    interface = extension.get("interface", {})
    require(interface.get("displayName") == "Codex Claude Crew", "Unexpected display name")
    require(interface.get("category") == "Developer Tools", "Unexpected category")
    require(1 <= len(interface.get("shortDescription", "")) <= 120, "Invalid short description")
    require(interface.get("defaultPrompt"), "Include a starter prompt")
    require("screenshots" not in interface, "Skills-only submissions omit screenshots")
    for key in ("composerIcon", "logo"):
        package_reference(interface.get(key), data)
    for config in ("mcp.json", ".mcp.json", ".app.json", "hooks", ".codex-plugin", ".claude-plugin"):
        require(not (plugin / config).exists(), f"Unexpected additional plugin component: {config}")
    require(not ({"apps", "hooks", "mcpServers"} & extension.keys()), "This release is skills-only")
    for name, content in data.items():
        if name.endswith(".png"):
            require(content[:8] == b"\x89PNG\r\n\x1a\n" and content[12:16] == b"IHDR", f"Invalid PNG: {name}")
            width, height = struct.unpack(">II", content[16:24])
            require(48 <= width == height <= 4096 and len(content) <= 5 * 1024 * 1024, f"Invalid icon dimensions or size: {name}")
        if name.endswith((".md", ".yaml", ".json", ".py")):
            require(b"/Users/" not in content, f"Developer-specific path in {name}")
    upstream = json.loads(data["upstream.json"])
    require(re.fullmatch(r"[0-9a-f]{40}", upstream.get("commit", "")), "Pin a full upstream revision")
    require(upstream.get("repository") == "https://github.com/aroramrinaal/codex-claude-crew", "Unexpected upstream repository")
    require(set(upstream.get("files", {})) == VENDORED, "Record provenance for every copied file")
    for name, entry in upstream["files"].items():
        require(hashlib.sha256(data[name]).hexdigest() == entry["sha256"], f"Upstream content changed: {name}")
    return manifest, data


def validate_marketplace(manifest, root=ROOT):
    marketplace = json.loads((root / ".agents/plugins/marketplace.json").read_text())
    entries = marketplace.get("plugins", [])
    require(len(entries) == 1, "Expected one marketplace entry")
    entry = entries[0]
    require(entry.get("name") == manifest["name"], "Marketplace identity mismatch")
    require(entry.get("source") == {"source": "local", "path": "./plugins/codex-claude-crew"}, "Marketplace must resolve the packaged plugin")
    require(entry.get("policy") == {"installation": "AVAILABLE", "authentication": "ON_USE"}, "Unexpected marketplace policy")
    require(entry.get("category") == "Developer Tools", "Marketplace category mismatch")


def build(plugin, output):
    manifest, data = validate(plugin)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{manifest['name']}-{manifest['version']}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
        for name in sorted(data):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            mode = 0o755 if name.endswith(".py") else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            bundle.writestr(info, data[name])
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n")
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "build"))
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    try:
        manifest, _ = validate()
        validate_marketplace(manifest)
        if args.command == "build":
            print(build(PLUGIN, args.output))
        else:
            print(f"{manifest['name']} {manifest['version']}: package files, references, provenance and marketplace checked")
    except (OSError, ValueError, KeyError, TypeError, struct.error) as error:
        parser.exit(1, f"Package error: {error}\n")


if __name__ == "__main__":
    main()
