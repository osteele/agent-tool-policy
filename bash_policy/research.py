"""Research rules for the Bash policy hook."""

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import FunctionPolicy, Request, decision_from_check

PREFLIGHT_MARKER = "preflight-checked"


RESEARCH_MARKER = "research-audited"


LOCAL_MARKERS = (RESEARCH_MARKER, PREFLIGHT_MARKER)


LINTER = Path.home() / ".claude" / "skills" / "research-ops" / "lint_research_script.py"


PREFLIGHT_MEMO = Path(
    os.environ.get(
        "WEFT_PREFLIGHT_MEMO",
        Path.home() / ".claude" / "state" / "weft-preflight-affirmed.json",
    )
)


def _review_cli() -> Path | None:
    """Find the fast local protocol CLI installed by agent-review."""
    configured = os.environ.get("AGENT_REVIEW_CLI") or os.environ.get(
        "CROSS_AGENT_REVIEW_CLI"
    )
    candidates = [
        Path(configured) if configured else None,
        Path.home() / "bin" / "agent-review",
        Path.home() / "code" / "agent-tools" / "agent-review" / "agent-review",
    ]
    return next(
        (path for path in candidates if path is not None and path.is_file()), None
    )


@dataclass(frozen=True)
class CheckResult:
    """Structured deterministic-check provenance for review packets."""

    name: str
    status: str
    output: str = ""
    tool: str = ""
    tool_version: str = ""
    exit_status: int | None = None

    @property
    def error_lines(self) -> str:
        return "\n".join(
            line for line in self.output.splitlines() if ": error: " in line
        )


def _lint(scripts: list[Path], profile: str) -> CheckResult:
    """Run deterministic checks without conflating failure and unavailability."""
    if not LINTER.is_file():
        return CheckResult(
            name="research-script-lint",
            status="unavailable",
            tool=str(LINTER),
        )
    try:
        r = subprocess.run(
            [str(LINTER), "--profile", profile, *[str(s) for s in scripts]],
            capture_output=True,
            check=False,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="research-script-lint",
            status="timeout",
            tool=str(LINTER),
        )
    except (OSError, subprocess.SubprocessError) as error:
        return CheckResult(
            name="research-script-lint",
            status="exception",
            output=str(error),
            tool=str(LINTER),
        )
    output = "\n".join(value for value in (r.stdout, r.stderr) if value).strip()
    return CheckResult(
        name="research-script-lint",
        status="pass" if r.returncode == 0 else "fail",
        output=output,
        tool=str(LINTER),
        exit_status=r.returncode,
    )


def _record_opportunities(
    scripts: list[Path], cwd: str, review_kind: str, check: CheckResult
) -> bool:
    """Best-effort local recording; this boundary must never affect a decision."""
    cli = _review_cli()
    if cli is None:
        return False
    try:
        recorder_env = dict(os.environ)
        if not (
            recorder_env.get("AGENT_REVIEW_STATE")
            or recorder_env.get("CROSS_AGENT_REVIEW_STATE")
        ):
            isolated_memo = recorder_env.get("WEFT_PREFLIGHT_MEMO")
            if isolated_memo:
                recorder_env["AGENT_REVIEW_STATE"] = str(
                    Path(isolated_memo).parent / "review-state"
                )
        result = subprocess.run(
            [
                str(cli),
                "opportunity",
                "record",
                "--project",
                cwd,
                "--kind",
                review_kind,
                "--source-label",
                "claude-codex-hook",
                "--checks-json",
                json.dumps([asdict(check)], sort_keys=True),
                *[str(script) for script in scripts],
            ],
            capture_output=True,
            check=False,
            env=recorder_env,
            text=True,
            timeout=2,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _preflight_scripts(command: str, cwd: str) -> list[Path]:
    """Script paths referenced in the command that actually exist on disk."""
    out = []
    for tok in re.findall(r"[\w./-]+\.py", command):
        cand = Path(cwd) / tok
        if cand.is_file():
            out.append(cand)
    return out


def _attestation_unseen(scripts: list[Path]) -> list[Path] | None:
    """Ask the protocol owner which script versions have not been affirmed.

    None means the boundary was unavailable. The caller fails open, matching the
    hook's established behavior for unavailable guards.
    """
    cli = _review_cli()
    if cli is None:
        return None
    try:
        result = subprocess.run(
            [
                str(cli),
                "attestation",
                "check",
                "--memo",
                str(PREFLIGHT_MEMO),
                *[str(script) for script in scripts],
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return None
        payload = json.loads(result.stdout)
        return [Path(value) for value in payload["unseen"]]
    except (
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
    ):
        return None


def _attestation_affirm(scripts: list[Path]) -> bool:
    """Record affirmations through the protocol owner."""
    cli = _review_cli()
    if cli is None:
        return False
    try:
        result = subprocess.run(
            [
                str(cli),
                "attestation",
                "affirm",
                "--memo",
                str(PREFLIGHT_MEMO),
                *[str(script) for script in scripts],
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def check_weft_preflight(command: str, cwd: str | None) -> tuple[str, str | None]:
    """Gate the FIRST `weft run` of a given script version, in research projects.

    Scoped to projects with a `lab-notebook/` directory: outside a research project weft
    is often just a remote task runner and there is nothing to check against.

    This targets forgetfulness, not forgery. The marker is an affirmation, not proof of
    work. Scripts are keyed by content hash, so an unchanged script is gated once and a
    modified one is gated again -- which matches the checklist's own scope, "new or
    modified experiments".

    Fails open on any unexpected condition: a guard that blocks work it cannot reason
    about is worse than no guard.

    Returns:
        ("deny", reason) if an unaffirmed script version is being submitted
        ("", None) otherwise
    """
    try:
        if not cwd or "weft run" not in command:
            return "", None
        if not (Path(cwd) / "lab-notebook").is_dir():
            return "", None

        scripts = _preflight_scripts(command, cwd)
        if not scripts:
            return "", None
        if re.search(rf"#.*{PREFLIGHT_MARKER}", command):
            _attestation_affirm(scripts)
            return "", None

        unseen = _attestation_unseen(scripts)
        if unseen is None or not unseen:
            return "", None

        lint_result = _lint(unseen, "remote")
        _record_opportunities(unseen, cwd, "remote-preflight", lint_result)
        lint_errors = lint_result.error_lines
        names = ", ".join(s.name for s in unseen)
        head = (
            f"Deterministic checks FAILED for {names}:\n\n{lint_errors}\n\n"
            "Fix these first — they are mechanical, not judgment calls.\n\n"
            if lint_errors
            else f"First `weft run` of this version of {names}.\n\n"
        )
        return (
            "deny",
            head
            + "Audit the judgment items with a FRESH SUBAGENT (the agent that wrote the\n"
            "script is context-saturated and motivated to proceed — the worst reviewer\n"
            "of its own code). Start it without inherited conversation and give it only\n"
            "the exact script plus its matching experiment record. Do not give it the\n"
            "diff, other source files, notebook history, gates, or the author's framing.\n\n"
            "Run the mechanical checks yourself:\n\n"
            f"    {LINTER} --profile remote <script>\n\n"
            f"Then resubmit with an affirming comment:\n\n"
            f"    weft run ... '...'  # {PREFLIGHT_MARKER}\n\n"
            "The marker affirms the audit happened and its findings were addressed; it "
            "is not proof. Scripts are keyed by content, so this version is gated once "
            "and any later edit is gated again.\n\n"
            "This is the `cold-script+experiment/v1` context profile. It cannot check project "
            "history or independent measurements; do not ask it to guess about either.\n\n"
            "Record the audit in `lab-notebook/CHECKS.md` (mechanism: "
            "~/.claude/skills/peer-vet/SKILL.md). **Log it even when the audit found "
            "nothing** — clean audits are the denominator that makes the rest legible, "
            "and without them the log becomes a highlight reel.",
        )
    except (OSError, json.JSONDecodeError, re.error):
        return "", None


def _is_experiment_script(path: Path) -> bool:
    """Experiment scripts: named exp_* or living under experiments/."""
    return path.name.startswith("exp_") or "experiments" in path.parts


def check_research_script_audit(
    command: str, cwd: str | None
) -> tuple[str, str | None]:
    """Gate the FIRST local `uv run` of an experiment-script version.

    Closes the coverage gap left by rule 12: a script submitted to weft is audited,
    but the same script run locally is not -- and analysis bugs, which are the ones
    that produce wrong numbers rather than dead jobs, mostly surface locally first.

    Only matches an actual FILE argument, so ad-hoc `uv run python - <<'PY'` and
    `uv run pytest` are untouched. Accepts either marker, so a script audited for
    remote submission does not get gated again when piloted locally.

    Fails open on any unexpected condition.
    """
    try:
        if not cwd or not re.search(r"\buv run\b", command):
            return "", None
        if "weft run" in command:  # rule 12 owns that path
            return "", None
        if not (Path(cwd) / "lab-notebook").is_dir():
            return "", None

        scripts = [
            s for s in _preflight_scripts(command, cwd) if _is_experiment_script(s)
        ]
        if not scripts:
            return "", None
        if any(re.search(rf"#.*{m}", command) for m in LOCAL_MARKERS):
            _attestation_affirm(scripts)
            return "", None

        unseen = _attestation_unseen(scripts)
        if unseen is None or not unseen:
            return "", None

        lint_result = _lint(unseen, "research")
        _record_opportunities(unseen, cwd, "local-research", lint_result)
        lint_errors = lint_result.error_lines
        names = ", ".join(s.name for s in unseen)
        head = (
            f"Deterministic checks FAILED for {names}:\n\n{lint_errors}\n\n"
            "Fix these first — they are mechanical, not judgment calls.\n\n"
            if lint_errors
            else f"First local run of this version of {names}.\n\n"
        )
        return (
            "deny",
            head + "Audit the judgment items with a FRESH SUBAGENT. Start it without\n"
            "inherited conversation and give it only the exact script plus its matching\n"
            "experiment record. Do not give it the diff, other source files, notebook\n"
            "history, gates, or the author's framing. Run the mechanical checks yourself:\n\n"
            f"    {LINTER} <script>\n\n"
            f"Then re-run with an affirming comment:\n\n"
            f"    uv run ... # {RESEARCH_MARKER}\n\n"
            f"If this script is also destined for weft, audit against the remote skill "
            f"too and use `# {PREFLIGHT_MARKER}` instead — it implies the research "
            f"audit, so one audit covers the pilot and the submission.\n\n"
            f"This is the `cold-script+experiment/v1` context profile. It cannot check project "
            "history or independent measurements; do not ask it to guess about either.\n\n"
            f"Record the audit in `lab-notebook/CHECKS.md` (mechanism: "
            f"~/.claude/skills/peer-vet/SKILL.md). **Log it even when the audit found "
            f"nothing** — clean audits are the denominator.",
        )
    except (OSError, json.JSONDecodeError, re.error):
        return "", None


def _cwd(request: Request) -> str | None:
    return str(request.cwd) if request.cwd else None


POLICIES = (
    FunctionPolicy(
        "research.weft-preflight",
        1000,
        lambda request: decision_from_check(
            check_weft_preflight(request.command, _cwd(request))
        ),
    ),
    FunctionPolicy(
        "research.local-script-audit",
        990,
        lambda request: decision_from_check(
            check_research_script_audit(request.command, _cwd(request))
        ),
    ),
)
