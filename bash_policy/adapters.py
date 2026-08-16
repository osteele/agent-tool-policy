"""Protocol adapters for the PreToolUse hooks of each supported agent.

The hosts differ in two ways that matter here: whether they can ask the user, and
whether they can run a command the hook rewrote. Claude does both. Codex and
opencode can rewrite but not ask. Kimi can do neither, verified against Kimi Code
CLI 0.36.1: a returned `updatedInput` is ignored and the original command runs.
"""

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


class KimiAdapter:
    """Render Kimi decisions, which carry neither an ask state nor a rewrite.

    Kimi ignores `updatedInput`, so a rewrite cannot be delivered through the
    hook at all. Dropping it silently is deliberate: the memory guard still
    applies through the `uv` shadow on PATH, and denying every `uv run` to force
    the rewrite would block the ordinary case that the shadow already covers.
    """

    def render(
        self,
        resolution: Resolution,
        *,
        updated_input: dict[str, object] | None = None,
    ) -> HookOutput | None:
        message = _message(resolution)
        if resolution.disposition in {"deny", "ask"}:
            hook_output: HookOutput = {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
            }
            if message:
                hook_output["permissionDecisionReason"] = message
            return {"hookSpecificOutput": hook_output}
        return None


CLAUDE_ADAPTER = ClaudeAdapter()
CODEX_ADAPTER = CodexAdapter()
KIMI_ADAPTER = KimiAdapter()

# opencode's plugin shim reads the same fields Codex does and has the same
# capabilities: it can block by throwing and rewrite by assigning args.
OPENCODE_ADAPTER = CODEX_ADAPTER

ADAPTERS = {
    "claude": CLAUDE_ADAPTER,
    "codex": CODEX_ADAPTER,
    "kimi": KIMI_ADAPTER,
    "opencode": OPENCODE_ADAPTER,
}
