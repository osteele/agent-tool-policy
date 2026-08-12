"""Shell rules for the Bash policy hook."""

import json
import os
import subprocess
from functools import cache
from pathlib import Path
from typing import TypedDict, cast

from .models import Command, Redirect, Request

PARSER = Path(__file__).resolve().parent.parent / ".bin" / "bash-policy-parser"
PARSER_TIMEOUT_SECONDS = 1


class RedirectPayload(TypedDict):
    operator: str
    target: str | None
    writes_file: bool


class CommandPayload(TypedDict):
    words: list[str]
    redirects: list[RedirectPayload]


class ShellAnalysis(TypedDict):
    commands: list[CommandPayload]


def _empty_analysis() -> ShellAnalysis:
    return {"commands": []}


@cache
def _analyze_shell(command: str, parser: str) -> ShellAnalysis:
    """
    Parse a shell command with the mvdan/sh helper.

    Returns an empty analysis when the helper is missing, times out, emits an
    invalid response, or cannot parse the input.
    """
    try:
        result = subprocess.run(
            [parser],
            input=command,
            capture_output=True,
            check=False,
            text=True,
            timeout=PARSER_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return _empty_analysis()
        payload = cast(dict[str, object], json.loads(result.stdout))
        return {"commands": cast(list[CommandPayload], payload["commands"])}
    except (
        json.JSONDecodeError,
        KeyError,
        OSError,
        subprocess.TimeoutExpired,
        TypeError,
    ):
        return _empty_analysis()


def analyze_shell(command: str) -> ShellAnalysis:
    """Return the cached structured analysis for a shell command."""
    parser = os.environ.get("BASH_POLICY_PARSER", str(PARSER))
    return _analyze_shell(command, parser)


def build_request(command: str, cwd: str | Path | None) -> Request:
    """Parse a hook input into the immutable request model."""
    analysis = analyze_shell(command)
    commands = tuple(
        Command(
            tuple(item["words"]),
            tuple(
                Redirect(
                    redirect["operator"],
                    redirect["target"],
                    redirect["writes_file"],
                )
                for redirect in item["redirects"]
            ),
        )
        for item in analysis["commands"]
    )
    return Request(command, Path(cwd).expanduser() if cwd else None, commands)


def extract_commands(command: str) -> list[list[str]]:
    """Return the simple command words from a shell command."""
    return [list(item["words"]) for item in analyze_shell(command)["commands"]]


def shell_writes_files(command: str) -> bool:
    """Return whether shell redirections can write to a filesystem path."""
    return any(
        redirect["writes_file"]
        for item in analyze_shell(command)["commands"]
        for redirect in item["redirects"]
    )


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
