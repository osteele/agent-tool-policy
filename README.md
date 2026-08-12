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

Shell commands are parsed by a small compiled helper built on
[`mvdan/sh`](https://github.com/mvdan/sh). The helper runs once per hook invocation
and returns the command words and file-writing redirect metadata needed by the
Python policy rules.

### Policy architecture

The hook parses each Bash request once into immutable `Request`, `Command`, and
`Redirect` values. A command owns its redirect metadata, so rules can distinguish
read-only input redirection from output that writes a file.

Each policy domain (`development`, `remote_jobs`, `research`, and `transfers`)
exports a `POLICIES` tuple. The central registry combines those tuples, and the
engine evaluates every applicable policy. Policies return explicit `Decision`
objects with a `deny`, `ask`, `allow`, or `advice` disposition.

Resolution uses fixed safety precedence: `deny` before `ask` before `allow`.
Numeric policy priority only breaks ties within the same disposition; it cannot
make an allow override a denial. Advice is deduplicated and aggregated separately
from the actionable result, then translated to the Claude or Codex hook protocol
at the outer boundary.

Research audit attestations are owned by
`~/code/agent-tools/cross-agent-review`. This hook detects relevant commands, runs
deterministic lint, and presents host-specific guidance; it delegates content
keys and memo state to the fast local `agent-review` command. Review Workbench
remains the separate viewer and future control surface for review rounds.

In jj repositories, Git mutations are denied by default. Read-oriented Git
commands produce a warning when they do not resolve to
`~/code/agent-tools/llm-shadow-commands/git`, because an unshadowed Git view can lag jj's
operation and revision state.

The hook adapts its output protocol to the caller. Claude receives native
`permissionDecision` values. For Codex PreToolUse, bare allows emit no output,
advisory allows become `additionalContext`, asks fail closed as denials, and
rewrites use `allow` together with `updatedInput`.

Install the hook symlink and build the parser helper (requires Go 1.25 or later):

```bash
./setup
```

Configure Claude Code to run it for Bash `PreToolUse` events:

```json
{
  "type": "command",
  "command": "~/.claude/hooks/bash-policy-hook",
  "timeout": 5
}
```

The launcher uses the project's virtual environment when available. Otherwise,
it resolves `uv` from `~/.local/bin` before falling back to `PATH`, so the hook
also works in agents that start hooks with a minimal environment.

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
