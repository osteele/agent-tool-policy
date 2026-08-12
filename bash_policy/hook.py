"""Hook rules for the Bash policy hook."""

import json
import select
import sys

from .adapters import CLAUDE_ADAPTER, CODEX_ADAPTER
from .engine import evaluate_policies
from .registry import POLICIES
from .shell import build_request


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
    adapter = (
        CODEX_ADAPTER
        if "model" in input_data or "turn_id" in input_data
        else CLAUDE_ADAPTER
    )

    if tool_name != "Bash":
        sys.exit(0)

    request = build_request(command, cwd)
    resolution = evaluate_policies(request, POLICIES)
    output = adapter.render(resolution)
    if output is not None:
        print(json.dumps(output))

    sys.exit(0)
