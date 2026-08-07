"""Hook rules for the Bash policy hook."""

import json
import select
import sys
from collections.abc import Callable

from .development import (
    check_all_commands_safe,
    check_git_in_jj_repo,
    check_jj_split,
    check_jj_squash,
    check_pdflatex_with_justfile,
    check_pip_command,
    check_ruff_commands,
)
from .remote_jobs import (
    check_remote_jobs_absolute_directory,
    check_remote_jobs_script_instrumentation,
    check_remote_jobs_unquoted_tilde,
    check_remote_jobs_wait_flag,
    check_ssh_remote_jobs_access,
)
from .research import check_research_script_audit, check_weft_preflight
from .transfers import check_mutagen_flush_with_rsync_fallback, evaluate_transfer


def emit_decision(decision: str, reason: str | None = None) -> None:
    """Print a hook decision as JSON and exit."""
    output: dict = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
    }
    if reason:
        output["hookSpecificOutput"]["permissionDecisionReason"] = reason
    print(json.dumps(output))
    sys.exit(0)


def main():
    # Use select to avoid blocking indefinitely on stdin (helps with signal handling)
    ready, _, _ = select.select([sys.stdin], [], [], 2.0)
    if not ready:
        sys.exit(0)  # No input available, nothing to check

    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})
    command = tool_input.get("command", "")
    cwd = input_data.get("cwd")

    if tool_name != "Bash":
        sys.exit(0)

    # Deny checks run first so they aren't short-circuited by allow checks
    # (e.g., check_git_in_jj_repo must deny `git status` before
    # check_all_commands_safe can auto-approve it).
    checks: list[Callable[[], tuple[str, str | None]]] = [
        lambda: check_weft_preflight(command, cwd),
        lambda: check_research_script_audit(command, cwd),
        lambda: check_git_in_jj_repo(command, cwd),
        lambda: check_jj_split(command),
        lambda: check_jj_squash(command, cwd),
        lambda: check_pdflatex_with_justfile(command, cwd),
        lambda: check_pip_command(command, cwd),
        lambda: check_remote_jobs_absolute_directory(command),
        lambda: check_remote_jobs_unquoted_tilde(command),
        lambda: check_ssh_remote_jobs_access(command, cwd),
        lambda: check_mutagen_flush_with_rsync_fallback(command),
        lambda: check_all_commands_safe(command),
        lambda: check_ruff_commands(command),
        lambda: check_remote_jobs_script_instrumentation(command, cwd),
        lambda: check_remote_jobs_wait_flag(command),
        lambda: evaluate_transfer(command, cwd),
    ]

    for check in checks:
        decision, reason = check()
        if decision:
            emit_decision(decision, reason)

    sys.exit(0)
