"""Protocol adapters for Claude and Codex PreToolUse hooks."""

from typing import Protocol

from .models import Resolution

HookOutput = dict[str, object]


class HookAdapter(Protocol):
    """Translate an internal resolution to one hook protocol."""

    def render(
        self,
        resolution: Resolution,
        *,
        updated_input: dict[str, object] | None = None,
    ) -> HookOutput | None: ...


def _message(resolution: Resolution) -> str | None:
    context = tuple(
        dict.fromkeys(
            value for value in (resolution.reason, *resolution.advice) if value
        )
    )
    return "\n\n".join(context) or None


class ClaudeAdapter:
    """Render native Claude decisions, including its supported ask state."""

    def render(
        self,
        resolution: Resolution,
        *,
        updated_input: dict[str, object] | None = None,
    ) -> HookOutput | None:
        message = _message(resolution)
        disposition = resolution.disposition
        if disposition is None:
            if message is None and updated_input is None:
                return None
            disposition = "allow"

        hook_output: HookOutput = {
            "hookEventName": "PreToolUse",
            "permissionDecision": disposition,
        }
        if message:
            hook_output["permissionDecisionReason"] = message
        if updated_input is not None:
            hook_output["updatedInput"] = updated_input
        return {"hookSpecificOutput": hook_output}


class CodexAdapter:
    """Render Codex decisions without emitting its unsupported ask state."""

    def render(
        self,
        resolution: Resolution,
        *,
        updated_input: dict[str, object] | None = None,
    ) -> HookOutput | None:
        message = _message(resolution)
        hook_output: HookOutput = {"hookEventName": "PreToolUse"}

        if resolution.disposition in {"deny", "ask"}:
            hook_output["permissionDecision"] = "deny"
            if message:
                hook_output["permissionDecisionReason"] = message
        elif updated_input is not None:
            hook_output["permissionDecision"] = "allow"
            hook_output["updatedInput"] = updated_input
            if message:
                hook_output["additionalContext"] = message
        elif message:
            hook_output["additionalContext"] = message
        else:
            return None

        return {"hookSpecificOutput": hook_output}


CLAUDE_ADAPTER = ClaudeAdapter()
CODEX_ADAPTER = CodexAdapter()
