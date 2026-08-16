"""Deterministic command rewrites shared by Claude and Codex hooks."""

from __future__ import annotations

import os
import re
import shlex
import shutil
from pathlib import Path

from .models import Request

RAM_GUARD = (
    Path(__file__).resolve().parent.parent.parent
    / "agent-command-guards"
    / "shadows"
    / "ram-guard"
)
BYPASS_PATTERN = re.compile(
    r"(?:^|\s)LLM_RAM_GUARD\s*=\s*(?:0|false|no|off)(?:\s|$)", re.IGNORECASE
)


def command_contains_uv_run(words: tuple[str, ...]) -> bool:
    """Recognize uv run, including command/mise prefixes and absolute uv paths."""

    def starts_uv_run(index: int) -> bool:
        return (
            len(words) > index + 1
            and os.path.basename(words[index]) == "uv"
            and words[index + 1] == "run"
        )

    if starts_uv_run(0):
        return True
    launcher = os.path.basename(words[0]) if words else ""
    if launcher == "mise" and "--" in words:
        return starts_uv_run(words.index("--") + 1)
    if launcher in {"command", "env", "exec", "time"}:
        return any(starts_uv_run(index) for index in range(1, len(words) - 1))
    return False


def needs_ram_guard(request: Request) -> bool:
    """Return whether a local shell request should receive the RAM guard."""
    if BYPASS_PATTERN.search(request.command):
        return False
    if "LLM_RAM_GUARD_ACTIVE=1" in request.command or "ram-guard" in request.command:
        return False
    return any(command_contains_uv_run(command.words) for command in request.commands)


def rewrite_for_ram_guard(request: Request) -> str | None:
    """Wrap the complete shell request so compound uv runs share one ceiling."""
    if not needs_ram_guard(request):
        return None
    found_guard = shutil.which("ram-guard")
    if found_guard and Path(found_guard).resolve() == RAM_GUARD.resolve():
        guard = "ram-guard"
    else:
        guard = shlex.quote(str(RAM_GUARD))
    command = shlex.quote(request.command)
    return f"{guard} -- /bin/zsh -c {command}"
