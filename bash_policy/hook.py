"""Hook rules for the Bash policy hook."""

import json
import select
import sys

from .engine import evaluate_policies
from .models import Resolution
from .registry import POLICIES
from .shell import build_request


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


def adapt_resolution(
    resolution: Resolution, *, codex: bool
) -> dict[str, object] | None:
    """Convert an internal resolution to the active hook protocol."""
    context = tuple(
        dict.fromkeys(
            value for value in (resolution.reason, *resolution.advice) if value
        )
    )
    message = "\n\n".join(context) or None
    if resolution.disposition in {"deny", "ask"}:
        return adapt_decision(
            resolution.disposition,
            message,
            codex=codex,
        )
    if resolution.disposition == "allow":
        return adapt_decision("allow", message, codex=codex)
    if message:
        # Claude requires a permission decision to carry advisory text. Codex can
        # represent the same internal advice as additional context without allowing.
        return adapt_decision("allow", message, codex=codex)
    return None


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

    request = build_request(command, cwd)
    resolution = evaluate_policies(request, POLICIES)
    output = adapt_resolution(resolution, codex=is_codex)
    if output is not None:
        print(json.dumps(output))

    sys.exit(0)
