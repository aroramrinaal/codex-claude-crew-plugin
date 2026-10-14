# Codex Claude Crew

Plugin packaging of the [Codex Claude Crew standalone skill](https://github.com/aroramrinaal/codex-claude-crew). Delegate reviews and implementation tasks to local Claude Code, with Codex verifying the results. Skills-only; no MCP server.

## Install

Requires macOS/Linux, Python 3.9+, Git, `ps`, and authenticated Claude Code 2.1.259+ supporting restricted mode.

1. From this checkout, run `codex plugin marketplace add .`.
2. Restart Codex desktop and install **Codex Claude Crew** from its marketplace.
3. Run setup, then select the plugin in your project chat.

Try: “Have Claude review my current changes, then verify its findings.”

Task context uses your Claude provider; records stay in `~/.codex-claude-crew`. Stop active runs before updating or uninstalling.

MIT licensed. Independent of OpenAI and Anthropic.
