"""Research rules for the Bash policy hook."""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

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


def _lint(scripts: list[Path], profile: str) -> str:
    """Deterministic checks. Returns the error lines, or '' if none/unavailable."""
    if not LINTER.is_file():
        return ""
    try:
        r = subprocess.run(
            [str(LINTER), "--profile", profile, *[str(s) for s in scripts]],
            capture_output=True,
            check=False,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return "\n".join(ln for ln in r.stdout.splitlines() if ": error: " in ln)


def _preflight_scripts(command: str, cwd: str) -> list[Path]:
    """Script paths referenced in the command that actually exist on disk."""
    out = []
    for tok in re.findall(r"[\w./-]+\.py", command):
        cand = Path(cwd) / tok
        if cand.is_file():
            out.append(cand)
    return out


def _preflight_key(path: Path) -> str:
    try:
        return f"{path.name}:{hashlib.sha256(path.read_bytes()).hexdigest()[:16]}"
    except OSError:
        return ""


def _preflight_memo() -> dict:
    try:
        return json.loads(PREFLIGHT_MEMO.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _preflight_remember(keys: list[str]) -> None:
    memo = _preflight_memo()
    for k in keys:
        memo[k] = True
    try:
        PREFLIGHT_MEMO.parent.mkdir(parents=True, exist_ok=True)
        PREFLIGHT_MEMO.write_text(json.dumps(memo, indent=0, sort_keys=True))
    except OSError:
        pass


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
        keys = [k for k in (_preflight_key(s) for s in scripts) if k]
        if not keys:
            return "", None

        if re.search(rf"#.*{PREFLIGHT_MARKER}", command):
            _preflight_remember(keys)
            return "", None

        memo = _preflight_memo()
        unseen = [s for s, k in zip(scripts, keys, strict=False) if k not in memo]
        if not unseen:
            return "", None

        lint_errors = _lint(unseen, "remote")
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
            "of its own code). Give it the diff since the last audited version and:\n\n"
            "    ~/.claude/skills/research-ops/writing-research-scripts.md   (correctness)\n"
            "    ~/.claude/skills/weft-submit/writing-remote-scripts.md      (remote-only)\n\n"
            "Run the mechanical checks yourself:\n\n"
            f"    {LINTER} --profile remote <script>\n\n"
            f"Then resubmit with an affirming comment:\n\n"
            f"    weft run ... '...'  # {PREFLIGHT_MARKER}\n\n"
            "The marker affirms the audit happened and its findings were addressed; it "
            "is not proof. Scripts are keyed by content, so this version is gated once "
            "and any later edit is gated again.\n\n"
            "A fresh subagent can only check gates decidable from the script and its output; "
            "it cannot know this project's history or hold independent measurements. "
            "**Give it only the artifact-tier gates** — a `CHECKS.md` marks these as a "
            "separate section or lists their IDs at the top. Do not ask it to judge which "
            "gates it can reach, or to name ones it could not evaluate: self-reporting a "
            "vacuous pass is the same failure as the vacuous pass.\n\n"
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
        keys = [k for k in (_preflight_key(s) for s in scripts) if k]
        if not keys:
            return "", None

        if any(re.search(rf"#.*{m}", command) for m in LOCAL_MARKERS):
            _preflight_remember(keys)
            return "", None

        memo = _preflight_memo()
        unseen = [s for s, k in zip(scripts, keys, strict=False) if k not in memo]
        if not unseen:
            return "", None

        lint_errors = _lint(unseen, "research")
        names = ", ".join(s.name for s in unseen)
        head = (
            f"Deterministic checks FAILED for {names}:\n\n{lint_errors}\n\n"
            "Fix these first — they are mechanical, not judgment calls.\n\n"
            if lint_errors
            else f"First local run of this version of {names}.\n\n"
        )
        return (
            "deny",
            head + "Audit the judgment items with a FRESH SUBAGENT against\n\n"
            "    ~/.claude/skills/research-ops/writing-research-scripts.md\n\n"
            "giving it the diff since the last audited version. Run the mechanical\n"
            "checks yourself:\n\n"
            f"    {LINTER} <script>\n\n"
            f"Then re-run with an affirming comment:\n\n"
            f"    uv run ... # {RESEARCH_MARKER}\n\n"
            f"If this script is also destined for weft, audit against the remote skill "
            f"too and use `# {PREFLIGHT_MARKER}` instead — it implies the research "
            f"audit, so one audit covers the pilot and the submission.\n\n"
            f"A fresh subagent can only check gates decidable from the script and its output; "
            "it cannot know this project's history or hold independent measurements. "
            "**Give it only the artifact-tier gates** — a `CHECKS.md` marks these as a "
            "separate section or lists their IDs at the top. Do not ask it to judge which "
            "gates it can reach, or to name ones it could not evaluate: self-reporting a "
            "vacuous pass is the same failure as the vacuous pass.\n\n"
            f"Record the audit in `lab-notebook/CHECKS.md` (mechanism: "
            f"~/.claude/skills/peer-vet/SKILL.md). **Log it even when the audit found "
            f"nothing** — clean audits are the denominator.",
        )
    except (OSError, json.JSONDecodeError, re.error):
        return "", None
