"""Hook rules for the Bash policy hook."""

import json
import select
import sys

from .adapters import ADAPTERS, HookAdapter
from .engine import evaluate_policies
from .registry import POLICIES
from .rewrites import rewrite_for_ram_guard
from .shell import build_request


def select_host(payload: dict, argv: list[str]) -> str:
    """Name the agent this request came from.

    opencode arrives through a plugin shim that says so on the command line.
    Kimi identifies itself in the payload. Codex is recognized by fields only it
    sends. Anything else is Claude, which is the only host whose configuration
    predates this dispatch.
    """
    if "--host" in argv:
        host = argv[argv.index("--host") + 1]
        if host not in ADAPTERS:
            raise SystemExit(
                f"unknown --host {host!r}; expected one of {sorted(ADAPTERS)}"
            )
        return host
    if payload.get("client_type") == "kimi_code_cli":
        return "kimi"
    if "model" in payload or "turn_id" in payload:
        return "codex"
    return "claude"


def select_adapter(payload: dict, argv: list[str]) -> HookAdapter:
    return ADAPTERS[select_host(payload, argv)]


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
    adapter = select_adapter(input_data, sys.argv[1:])

    if tool_name != "Bash":
        sys.exit(0)

    request = build_request(command, cwd)
    resolution = evaluate_policies(request, POLICIES)
    rewritten_command = rewrite_for_ram_guard(request)
    updated_input = (
        {**tool_input, "command": rewritten_command} if rewritten_command else None
    )
    output = adapter.render(resolution, updated_input=updated_input)
    if output is not None:
        print(json.dumps(output))

    sys.exit(0)
