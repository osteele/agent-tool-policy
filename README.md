# Claude Hooks

Personal Claude Code hooks that are independent of `claude-wrapper` and its
Anthropic proxy.

## Bash policy

`bash-policy.py` is a `PreToolUse` hook for Bash commands. It applies policy for:

- `scp`, `rsync`, and Mutagen-managed transfers
- SSH, `remote-jobs`, and Weft usage
- Git/Jujutsu, Ruff, pip, and LaTeX commands
- first-run audits for research scripts

The executable is intentionally small. Policy implementations live in the
`bash_policy` package, grouped by responsibility.

Research audit attestations are owned by
`~/code/research/cross-agent-review`. This hook detects relevant commands, runs
deterministic lint, and presents host-specific guidance; it delegates content
keys and memo state to the fast local `agent-review` command. Review Workbench
remains the separate viewer and future control surface for review rounds.

In jj repositories, Git mutations are denied by default. Read-oriented Git
commands produce a warning when they do not resolve to
`~/code/llm-shadow-commands/git`, because an unshadowed Git view can lag jj's
operation and revision state.

The hook adapts its output protocol to the caller. Claude receives native
`permissionDecision` values. For Codex PreToolUse, bare allows emit no output,
advisory allows become `additionalContext`, asks fail closed as denials, and
rewrites use `allow` together with `updatedInput`.

Install the hook symlink:

```bash
./setup
```

Configure Claude Code to run it for Bash `PreToolUse` events:

```json
{
  "type": "command",
  "command": "~/.claude/hooks/bash-policy.py",
  "timeout": 5
}
```

Run the tests with:

```bash
just test
```

Development dependencies are managed by uv. Run all formatting, linting, type,
and test checks with:

```bash
uv sync
just check
```
