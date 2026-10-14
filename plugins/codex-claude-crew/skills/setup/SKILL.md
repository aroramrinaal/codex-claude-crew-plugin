---
name: setup
description: Set up Codex Claude Crew after plugin installation, or diagnose its local Python, Git, Claude Code, authentication, and PATH prerequisites. Use for Crew setup or readiness problems, not for general Claude configuration or delegated coding tasks.
license: MIT
compatibility: Requires a local shell on macOS or Linux. Checks Python 3.9+, Git, ps, and authenticated Claude Code 2.1.259+ with restricted-profile flags.
---

# Set up Codex Claude Crew

Check readiness using the plugin's bundled relay. Setup does not make a model request or start a delegated task. Claude Code must already be installed and authenticated; the plugin uses that local CLI and its configured provider.

1. Resolve [the relay](../codex-claude-crew/scripts/relay.py) relative to this installed `SKILL.md`. Use its absolute path, quoted in shell commands. Do not use a standalone skill, a sibling source checkout, or a hard-coded plugin-cache version. If the packaged relay is missing, report an incomplete installation.
2. Check that the host is macOS or Linux and `python3` is version 3.9 or newer before invoking the relay. Report an unsupported platform or missing Python directly.
3. Run `python3 "<absolute-relay-path>" preflight` from the user's project. This probes Claude's version, flags and sign-in, Git, process inspection, and state-directory access. It may create the state directory and a temporary write probe; it does not call a model. Read the JSON even when the command exits nonzero.
4. Explain any failed checks using the returned fields. Do not weaken the execution profile to make setup pass.

| Report | Remediation |
|---|---|
| `claude` is null | Install Claude Code using its official instructions, or locate an existing executable. For a GUI PATH mismatch, prefix relay commands with `CLAUDE_CREW_BIN="<absolute-claude-path>"`. Carry that override into later delegation commands. |
| `capabilities.restricted_profile` is false | The CLI must satisfy both the minimum version and required flags. Explain the reported version and suggest `claude update`; retry preflight after the update. |
| `auth.loggedIn` is not true | Have the user complete Claude Code sign-in in their terminal. If already signed in, check the host's network/keychain access and any existing `CLAUDE_CONFIG_DIR`. Do not read, copy, or print credentials. |
| `git` is null | Git must be available to the same shell that starts the relay. |
| `process_inspection` is false | `ps` and process identity inspection must work under the host's execution policy. |
| `state_writable` is false | Use a writable directory outside the installed plugin. If needed, prefix relay commands with `CLAUDE_CREW_HOME="<absolute-state-directory>"` and preserve that override for subsequent commands. |

Suggest installation, updates, or configuration changes when needed; perform them only within the user's requested scope. Do not change global Claude permissions, install an MCP server, or write preferences into the plugin cache.

If the standalone `codex-claude-crew` skill is also installed, leave it intact. For plugin use, select **Codex Claude Crew** in the plugin picker and load [the bundled delegation skill](../codex-claude-crew/SKILL.md) from this same installation. Both distributions share `~/.codex-claude-crew` by default; keep any custom state directory consistent so existing runs remain visible.

Report readiness only when preflight returns `ok: true`. Include the CLI version and any executable/state override the user needs. Explain that task briefs and source context are sent through the user's Claude provider and that local records retain task material. Finish with one suggested first request, such as “Have Claude review my current changes, then verify its findings.” Start it only if the user requested a review as part of setup; otherwise setup ends here.
