"""Shell rules for the Bash policy hook."""

import os
from typing import Protocol, cast

import bashlex
import bashlex.errors


class _BashNode(Protocol):
    kind: str
    word: str
    parts: list["_BashNode"]
    list: list["_BashNode"]


def extract_commands(command: str) -> list[list[str]]:
    """
    Parse a shell command string using bashlex and return a list of simple
    commands, each as a list of word strings. Shell syntax (redirects, pipes,
    &, &&, ||, ;) is handled by the parser and stripped out.

    Returns an empty list if parsing fails.
    """
    try:
        parts = bashlex.parse(command)
    except bashlex.errors.ParsingError:
        return []

    commands: list[list[str]] = []

    def visit(node: _BashNode) -> None:
        if node.kind == "command":
            words = [p.word for p in node.parts if p.kind == "word"]
            if words:
                commands.append(words)
        elif node.kind == "list":
            for child in node.parts:
                if hasattr(child, "kind"):
                    visit(child)
        elif node.kind == "compound":
            for child in node.list:
                visit(child)
        elif hasattr(node, "parts"):
            for child in node.parts:
                if hasattr(child, "kind"):
                    visit(child)

    for part in parts:
        visit(cast(_BashNode, part))

    return commands


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
