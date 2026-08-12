"""Immutable types shared by Bash policy domains and the evaluation engine."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

Disposition = Literal["deny", "ask", "allow", "advice"]
ActionDisposition = Literal["deny", "ask", "allow"]


@dataclass(frozen=True)
class Redirect:
    """A shell redirect attached to one simple command."""

    operator: str
    target: str | None
    writes_file: bool


@dataclass(frozen=True)
class Command:
    """One simple command extracted from the Bash syntax tree."""

    words: tuple[str, ...]
    redirects: tuple[Redirect, ...] = ()

    @property
    def writes_files(self) -> bool:
        return any(redirect.writes_file for redirect in self.redirects)


@dataclass(frozen=True)
class Request:
    command: str
    cwd: Path | None
    commands: tuple[Command, ...]


@dataclass(frozen=True)
class Decision:
    disposition: Disposition
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.disposition == "advice" and not self.reason:
            raise ValueError("advice decisions require a reason")


class Policy(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def priority(self) -> int: ...

    def evaluate(self, request: Request) -> Decision | None: ...


@dataclass(frozen=True)
class FunctionPolicy:
    """A named policy implemented by a request evaluator function."""

    name: str
    priority: int
    evaluator: Callable[[Request], Decision | None]

    def evaluate(self, request: Request) -> Decision | None:
        return self.evaluator(request)


@dataclass(frozen=True)
class Resolution:
    """The selected action plus all independent advisory context."""

    disposition: ActionDisposition | None
    reason: str | None = None
    advice: tuple[str, ...] = ()
    policy_name: str | None = None


def decision_from_check(result: tuple[str, str | None]) -> Decision | None:
    """Translate a domain check result into the explicit decision model."""
    disposition, reason = result
    if not disposition:
        return None
    if disposition == "allow" and reason:
        return Decision("advice", reason)
    if disposition not in {"deny", "ask", "allow"}:
        raise ValueError(f"unknown policy disposition: {disposition}")
    return Decision(disposition, reason)
