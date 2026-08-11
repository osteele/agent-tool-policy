"""Shell rules for the Bash policy hook."""

import json
import os
import subprocess
from functools import cache
from pathlib import Path
from typing import cast

PARSER = Path(__file__).resolve().parent.parent / ".bin" / "bash-policy-parser"
PARSER_TIMEOUT_SECONDS = 1


@cache
def extract_commands(command: str) -> list[list[str]]:
    """
    Parse a shell command with the mvdan/sh helper and return its simple commands.

    Each command is represented as word strings. Shell syntax such as redirects,
    pipelines, and list operators is omitted. Returns an empty list when the helper
    is missing, times out, or cannot parse the input.
    """
    try:
        result = subprocess.run(
            [os.environ.get("BASH_POLICY_PARSER", str(PARSER))],
            input=command,
            capture_output=True,
            check=False,
            text=True,
            timeout=PARSER_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return []
        return cast(list[list[str]], json.loads(result.stdout))
    except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired):
        return []


def find_command(command: str, name: str) -> list[str] | None:
    """
    Find the first simple command in a shell command string whose
    first word (basename) matches `name`. Returns the word list,
    or None if not found.
    """
    for cmd_words in extract_commands(command):
        if os.path.basename(cmd_words[0]) == name:
            return cmd_words
    return None
