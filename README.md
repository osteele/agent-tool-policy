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
