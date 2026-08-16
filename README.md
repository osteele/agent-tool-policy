# Agent Tool Policy

A `PreToolUse` hook for Claude Code and Codex that decides Bash tool requests
against local development and research conventions. It is independent of
`claude-wrapper` and its Anthropic proxy.

## Positioning

Claude Code Auto mode and Codex approval modes provide the general safety boundary
for tool use. They classify risk, constrain filesystem and network access, and
decide when an action needs review. These hooks leave that broad responsibility to
the host agent.

The policies here encode conventions a general permission system cannot infer.
They read repository state, parsed command structure, local tools, and saved
attestations to enforce rules such as:

- use Jujutsu instead of mutating Git state in a Jujutsu repository
- keep Jujutsu commands out of editors and interactive UIs that nobody can close
- use a project's configured build command instead of bypassing it
- validate `remote-jobs` and Weft invocations against local workflow rules
- coordinate transfers with Mutagen and protected destination conventions
- require first-run review of new or modified research scripts
- guard local `uv run` process trees against workstation-wide memory exhaustion

Most commands receive no decision and continue to the native permission system. A
small set of exact, deterministic rules approves familiar read-oriented commands
or supplies advice. The hook does not use ML to classify arbitrary commands, and
its policies do not replace the sandbox or the agent's approval review.

## Bash policy

`bash-policy.py` is the entry point. It applies policy for:

- `scp`, `rsync`, and Mutagen-managed transfers
- SSH, `remote-jobs`, and Weft usage
- Git, Jujutsu, Ruff, pip, and LaTeX commands
- first-run audits for research scripts

The executable stays small. Policy implementations live in the `bash_policy`
package, grouped by responsibility.

A compiled helper built on [`mvdan/sh`](https://github.com/mvdan/sh) parses each
command. It runs once per hook invocation and returns the command words and the
file-writing redirect metadata the Python rules need.

### Policy architecture

The hook parses each Bash request once into immutable `Request`, `Command`, and
`Redirect` values. A command owns its redirect metadata, so rules can distinguish
read-only input redirection from output that writes a file.

Each policy domain (`development`, `remote_jobs`, `research`, and `transfers`)
exports a `POLICIES` tuple. The central registry combines those tuples and the
engine evaluates every applicable policy. Policies return explicit `Decision`
objects with a `deny`, `ask`, `allow`, or `advice` disposition.

Resolution uses fixed safety precedence: `deny` before `ask` before `allow`.
Numeric policy priority only breaks ties within one disposition; it cannot make an
allow override a denial. Advice is deduplicated and aggregated separately from the
actionable result, then translated to the Claude or Codex hook protocol at the
outer boundary. Dedicated protocol adapters preserve Claude's native `ask`
decision, while the Codex adapter converts asks to denials because Codex
`PreToolUse` does not currently support asking.

### Jujutsu rules

In a jj repository, Git mutations are denied by default. Read-oriented Git
commands produce a warning when they do not resolve to the Git shadow in
[agent-command-guards](../agent-command-guards), because an unguarded Git view can
lag jj's operation and revision state.

jj commands that would block on an editor or an interactive UI are denied:
`describe`, `commit`, and `squash` without a message; `split` without both a
message and explicit paths; `resolve` other than `--list` or `--tool
:ours`/`:theirs`; `diffedit`, `arrange`, `config edit`, and `sparse edit`; and any
subcommand invoked with `-i`/`--interactive`, `--editor`, or a `--tool` that
selects changes. `--tool` on `diff`, `log`, and `show` names a diff formatter
rather than an editor, and is left alone. Each denial names the non-interactive
alternative. The rules were checked against jj 0.44 by running every form with a
recording editor.

Both escapes are comments on the command itself, so they survive the hook without
changing what runs:

```bash
git commit -m 'fix'  # intentionally ignoring jj
jj describe          # intentionally interactive
```

### Memory guard

Local `uv run` requests are rewritten to run under `ram-guard` in
[agent-command-guards](../agent-command-guards). Immediately before launch the
guard grants the command 70% of the memory macOS reports as available, leaving a
30% reserve. It monitors aggregate process-tree RSS where process inspection is
permitted, and falls back to the corresponding available-memory floor where it is
not. It also sets PyTorch MPS allocator watermarks at 0.7 hard and 0.6 soft.

Tune the policy with `LLM_RAM_GUARD_AVAILABLE_FRACTION`, replace the dynamic
budget with `LLM_RAM_GUARD_LIMIT`, and adjust `LLM_MPS_HIGH_WATERMARK_RATIO` and
`LLM_MPS_LOW_WATERMARK_RATIO`. Set `LLM_RAM_GUARD=off` on an individual command to
bypass the rewrite.

The `uv` shadow in agent-command-guards wraps `uv run` when it wins the `PATH`
lookup. This hook covers what `PATH` cannot see: absolute uv paths, and `mise` or
`command` prefixes. Both entry points check `LLM_RAM_GUARD_ACTIVE`, so a command
is never wrapped twice.

### Research review

Research audit attestations are owned by [agent-review](../agent-review). This
hook detects relevant commands, runs deterministic lint, and presents
host-specific guidance; it delegates content keys and memo state to the fast local
`agent-review` command. Review Workbench remains the separate viewer and future
control surface for review rounds.

## Output protocol

The hook adapts its output to the caller. Claude receives native
`permissionDecision` values. For Codex `PreToolUse`, bare allows emit no output,
advisory allows become `additionalContext`, asks fail closed as denials, and
rewrites use `allow` together with `updatedInput`.

## Installation

Install the hook symlink and build the parser helper, which needs Go 1.25 or
later:

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

The launcher uses the project's virtual environment when available. Otherwise it
resolves `uv` from `~/.local/bin` before falling back to `PATH`, so the hook also
works in agents that start hooks with a minimal environment.

## Development

Dependencies are managed by uv. Run the formatting, linting, type, and test checks
with:

```bash
uv sync
just check
```

`just test` runs the Go parser tests and the Python suite.
