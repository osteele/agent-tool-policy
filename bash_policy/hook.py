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


def adapt_decision(
    decision: str,
    reason: str | None = None,
    *,
    codex: bool,
    updated_input: dict[str, object] | None = None,
) -> dict[str, object] | None:
    """Convert a policy decision to the active hook protocol."""
    if codex:
        hook_output: dict = {"hookEventName": "PreToolUse"}
        if decision == "allow":
            if updated_input is not None:
                hook_output["permissionDecision"] = "allow"
                hook_output["updatedInput"] = updated_input
            elif not reason:
                return None
            if reason:
                hook_output["additionalContext"] = reason
        elif decision in {"deny", "ask"}:
            hook_output["permissionDecision"] = "deny"
            if reason:
                hook_output["permissionDecisionReason"] = reason
        else:
            return None
        return {"hookSpecificOutput": hook_output}

    hook_output = {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
    }
    if reason:
        hook_output["permissionDecisionReason"] = reason
    if updated_input is not None:
        return {
            "hookSpecificOutput": {
                **hook_output,
                "updatedInput": updated_input,
            }
        }
    return {"hookSpecificOutput": hook_output}


def emit_decision(
    decision: str,
    reason: str | None = None,
    *,
    codex: bool,
    updated_input: dict[str, object] | None = None,
) -> None:
    """Print an adapted hook decision and exit."""
    output = adapt_decision(decision, reason, codex=codex, updated_input=updated_input)
    if output is not None:
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
    is_codex = "model" in input_data or "turn_id" in input_data

    if tool_name != "Bash":
        sys.exit(0)

    # Deny checks run first so they aren't short-circuited by allow checks
    # check_git_in_jj_repo must deny Git mutations or warn about unshadowed Git
    # reads before check_all_commands_safe can auto-approve them.
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
            emit_decision(decision, reason, codex=is_codex)

    sys.exit(0)
