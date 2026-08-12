"""Development rules for the Bash policy hook."""

import os
import re
import shutil
import subprocess
from pathlib import Path

from .shell import extract_commands, find_command, shell_writes_files

SAFE_COMMANDS: dict[str, set[str] | None] = {
    # None means any subcommand is safe; a set means only those subcommands
    "sleep": None,
    "echo": None,
    "head": None,
    "tail": None,
    "cat": None,
    "wc": None,
    "date": None,
    "true": None,
    "false": None,
    "remote-jobs": {"list", "log", "status", "mark-processed"},
    "weft": {"list", "log", "status", "mark-processed"},
    "ruff": {"check", "format"},
    "jj": {"log", "status", "diff", "show"},
    "git": {"log", "status", "diff", "show"},
    "grep": None,
    "ls": None,
    "obsidian": {
        "backlinks",
        "diff",
        "file",
        "files",
        "folders",
        "links",
        "outline",
        "read",
        "search",
        "tags",
        "tasks",
    },
    "pdfinfo": None,
    "which": None,
}


def check_ruff_commands(command: str) -> tuple[str, str | None]:
    """
    Auto-approve commands that only contain ruff format and/or ruff check.

    Returns:
        ("allow", None) if command only contains ruff format/check
        ("", None) otherwise
    """
    if shell_writes_files(command):
        return "", None

    cmds = extract_commands(command)
    if not cmds:
        return "", None

    for words in cmds:
        if not words:
            continue
        cmd_name = os.path.basename(words[0])
        if cmd_name != "ruff":
            return "", None
        if len(words) < 2:
            return "", None
        subcmd = words[1]
        if subcmd not in ("format", "check"):
            return "", None

    # All commands are ruff format or ruff check
    return "allow", None


def _is_safe_command(words: list[str]) -> bool:
    """Check if a single parsed command is in the safe list."""
    if not words:
        return False
    cmd_name = os.path.basename(words[0])
    if cmd_name == "jj" and words[1:3] == ["bookmark", "list"]:
        return True
    if cmd_name == "git" and words[1:3] == ["worktree", "list"]:
        return True
    if cmd_name not in SAFE_COMMANDS:
        return False
    allowed_subcmds = SAFE_COMMANDS[cmd_name]
    if allowed_subcmds is None:
        return True
    if len(words) < 2:
        return False
    return words[1] in allowed_subcmds


def check_all_commands_safe(command: str) -> tuple[str, str | None]:
    """
    Auto-approve when every command in a compound shell expression is safe/read-only.

    Returns:
        ("allow", None) if all commands are safe
        ("", None) otherwise
    """
    if shell_writes_files(command):
        return "", None

    cmds = extract_commands(command)
    if not cmds:
        return "", None

    if all(_is_safe_command(words) for words in cmds):
        return "allow", None

    return "", None


def is_jj_repo(cwd: str) -> bool:
    """Check if cwd is inside a jj repository by walking up the directory tree."""
    path = Path(os.path.expanduser(cwd))
    while path != path.parent:
        if (path / ".jj").is_dir():
            return True
        path = path.parent
    return False


def _resolve_executable(executable: str, cwd: str) -> Path | None:
    """Resolve a command token the way the shell will for a simple invocation."""
    if "/" in executable:
        path = Path(os.path.expanduser(executable))
        if not path.is_absolute():
            path = Path(cwd) / path
        return path.resolve()

    resolved = shutil.which(executable)
    return Path(resolved).resolve() if resolved else None


def _git_shadow_is_in_scope(executable: str, cwd: str) -> bool:
    """Check whether this Git invocation resolves to llm-shadow-commands/git."""
    shadow_dir = os.environ.get(
        "LLM_SHADOW_COMMANDS_DIR", "~/code/agent-tools/llm-shadow-commands"
    )
    shadow_git = Path(os.path.expanduser(shadow_dir), "git")
    invoked_git = _resolve_executable(executable, cwd)
    if (
        invoked_git is None
        or not shadow_git.is_file()
        or not os.access(shadow_git, os.X_OK)
    ):
        return False
    return invoked_git == shadow_git.resolve()


def _git_subcommand_index(words: list[str]) -> int | None:
    """Return the index of Git's subcommand after its global options."""
    options_with_separate_values = {
        "-C",
        "-c",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--namespace",
        "--super-prefix",
        "--work-tree",
    }
    index = 1
    while index < len(words):
        word = words[index]
        if word in options_with_separate_values:
            index += 2
            continue
        if word.startswith("-"):
            index += 1
            continue
        return index
    return None


def _git_branch_mode(args: list[str]) -> str:
    """Classify Git branch arguments as read-only, mutating, or unknown."""
    mutating_options = {
        "-d",
        "-D",
        "-m",
        "-M",
        "-c",
        "-C",
        "-f",
        "--copy",
        "--create-reflog",
        "--delete",
        "--edit-description",
        "--force",
        "--move",
        "--no-track",
        "--recurse-submodules",
        "--set-upstream-to",
        "--track",
        "--unset-upstream",
    }
    for arg in args:
        option = arg.split("=", 1)[0]
        if option in mutating_options:
            return "write"

    read_flags = {
        "-a",
        "-r",
        "-v",
        "-vv",
        "--all",
        "--ignore-case",
        "--list",
        "--no-abbrev",
        "--no-color",
        "--no-column",
        "--omit-empty",
        "--remotes",
        "--show-current",
        "--verbose",
    }
    read_options = {
        "--abbrev",
        "--color",
        "--column",
        "--contains",
        "--format",
        "--merged",
        "--no-contains",
        "--no-merged",
        "--points-at",
        "--sort",
    }

    list_patterns = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            return "read" if list_patterns else "unknown"
        if arg == "--list":
            list_patterns = True
            index += 1
            continue
        if arg in read_flags:
            index += 1
            continue
        if any(arg.startswith(f"{option}=") for option in read_options):
            index += 1
            continue
        if arg in read_options:
            if index + 1 < len(args) and not args[index + 1].startswith("-"):
                index += 2
            else:
                index += 1
            continue
        if arg.startswith("-") and set(arg[1:]) <= {"a", "r", "v"}:
            index += 1
            continue
        if list_patterns and not arg.startswith("-"):
            index += 1
            continue
        if not arg.startswith("-"):
            return "write"
        return "unknown"
    return "read"


def _git_without_shadow_warning(subcmd: str) -> tuple[str, str]:
    jj_command = "jj bookmark list" if subcmd == "branch" else f"jj {subcmd}"
    return "allow", (
        f"This is a jj repository, but `git {subcmd}` is not using the Git shadow "
        f"wrapper. Use `{jj_command}` instead; direct Git may observe stale or "
        "incomplete jj state."
    )


def _git_denied_reason(subcmd: str) -> str:
    return (
        f"This is a jj repository. Use `jj` instead of `git {subcmd}`. "
        f"To intentionally use git, add a comment: "
        f"`git {subcmd} # intentionally ignoring jj`"
    )


def check_git_in_jj_repo(command: str, cwd: str | None) -> tuple[str, str | None]:
    """
    Check if a git command is being used in a jj repository.
    Deny git commit/add/stash/apply and mutating git branch operations unless the
    command includes an intentional override comment. Warn on read-only Git commands
    when that invocation does not resolve to the llm-shadow-commands Git wrapper.

    The executable-path check catches explicit bypasses such as /usr/bin/git even
    when the shadow-command directory is otherwise on PATH.

    Uses both structured parsing and a regex fallback, so the guard still works
    when the parser cannot parse the command.

    Returns:
        ("deny", reason) if git command used in jj repo without override
        ("allow", warning) if a read command bypasses the shadow wrapper
        ("", None) otherwise
    """
    if not cwd:
        return "", None

    if not is_jj_repo(cwd):
        return "", None

    # Check for an intentional override comment in the raw command
    if re.search(r"#.*intentionally ignoring jj", command):
        return "", None

    denied_subcommands = {"commit", "add", "stash", "apply"}
    shadowed_subcommands = {"log", "diff", "show", "status", "branch"}

    # Try structured parsing first
    parsed_cmds = extract_commands(command)
    git_commands: list[tuple[str, str, list[str]]] = []
    for cmd_words in parsed_cmds:
        if os.path.basename(cmd_words[0]) == "git" and len(cmd_words) >= 2:
            subcmd_index = _git_subcommand_index(cmd_words)
            if subcmd_index is not None:
                git_commands.append(
                    (
                        cmd_words[0],
                        cmd_words[subcmd_index],
                        cmd_words[subcmd_index + 1 :],
                    )
                )

    for _, subcmd, args in git_commands:
        if subcmd in denied_subcommands or (
            subcmd == "branch" and _git_branch_mode(args) == "write"
        ):
            return "deny", _git_denied_reason(subcmd)

    for executable, subcmd, args in git_commands:
        is_shadowed_read = subcmd in shadowed_subcommands and (
            subcmd != "branch" or _git_branch_mode(args) == "read"
        )
        if is_shadowed_read and not _git_shadow_is_in_scope(executable, cwd):
            return _git_without_shadow_warning(subcmd)

    # Regex fallback when structured parsing fails.
    if not parsed_cmds:
        branch_mutation = (
            r"\bgit\s+branch\s+"
            r"(?:-[dDmMcCf]\b|--(?:copy|create-reflog|delete|edit-description|force|move|no-track|recurse-submodules|set-upstream-to|track|unset-upstream)\b)"
        )
        if re.search(branch_mutation, command):
            return "deny", _git_denied_reason("branch")
        git_pattern = r"\bgit\s+(commit|add|stash|apply|log|diff|show|status)\b"
        subcommands = [match.group(1) for match in re.finditer(git_pattern, command)]
        for subcmd in subcommands:
            if subcmd in denied_subcommands:
                return "deny", _git_denied_reason(subcmd)
        for subcmd in subcommands:
            if not _git_shadow_is_in_scope("git", cwd):
                return _git_without_shadow_warning(subcmd)

    return "", None


def check_jj_split(command: str) -> tuple[str, str | None]:
    """
    Deny `jj split` — it is interactive and cannot be used from Claude Code.
    Use the /jj-split skill instead.

    Returns:
        ("deny", reason) if jj split is used
        ("", None) otherwise
    """
    jj_words = find_command(command, "jj")
    if not jj_words:
        return "", None

    args = jj_words[1:]
    if not args or args[0] != "split":
        return "", None

    return "deny", (
        "`jj split` is interactive and cannot be used from Claude Code. "
        "Use the `/jj-split` skill instead."
    )


def _jj_rev_has_blank_description(rev: str, cwd: str | None) -> bool:
    """Check if a jj revision has an empty description."""
    try:
        result = subprocess.run(
            ["jj", "log", "--no-graph", "-r", rev, "-T", "description"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
            cwd=cwd,
        )
        return result.returncode == 0 and result.stdout.strip() == ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def check_jj_squash(command: str, cwd: str | None) -> tuple[str, str | None]:
    """
    Deny `jj squash` unless:
    - -m/--message is provided, or
    - -u/--use-destination-message is provided, or
    - either source or destination revision has a blank description.

    Without these, jj opens an interactive editor.

    Returns:
        ("deny", reason) if jj squash would open an editor
        ("", None) otherwise
    """
    jj_words = find_command(command, "jj")
    if not jj_words:
        return "", None

    args = jj_words[1:]
    if not args or args[0] != "squash":
        return "", None

    source_rev = "@"
    dest_rev = "@-"

    i = 1
    while i < len(args):
        arg = args[i]
        if arg in ("--message", "-m") or arg.startswith(("--message=", "-m")):
            return "", None
        if arg in ("--use-destination-message", "-u"):
            return "", None
        if arg in ("--from", "-r") and i + 1 < len(args):
            source_rev = args[i + 1]
            i += 2
            continue
        if arg == "--into" and i + 1 < len(args):
            dest_rev = args[i + 1]
            i += 2
            continue
        if arg.startswith("--from="):
            source_rev = arg.split("=", 1)[1]
        elif arg.startswith("--into="):
            dest_rev = arg.split("=", 1)[1]
        i += 1

    # Allow if either revision has a blank description (no editor conflict)
    if cwd and (
        _jj_rev_has_blank_description(source_rev, cwd)
        or _jj_rev_has_blank_description(dest_rev, cwd)
    ):
        return "", None

    return "deny", (
        "`jj squash` without -m/--message opens an interactive editor, which cannot be used from Claude Code. "
        "Use `jj squash -m '...'` to provide the message, or `jj squash -u` to use the destination message."
    )


def check_pdflatex_with_justfile(
    command: str, cwd: str | None
) -> tuple[str, str | None]:
    """
    Deny `pdflatex` when a justfile in the project has a `build pdf` recipe.
    Override with `# ignore justfile` comment in the command.

    Returns:
        ("deny", reason) if pdflatex used in project with justfile build-pdf recipe
        ("", None) otherwise
    """
    if not find_command(command, "pdflatex"):
        return "", None

    # Check for override comment
    if "# ignore justfile" in command:
        return "", None

    if not cwd:
        return "", None

    # Walk up to find a justfile
    path = Path(os.path.expanduser(cwd))
    while path != path.parent:
        justfile = path / "justfile"
        if not justfile.exists():
            justfile = path / "Justfile"
        if justfile.exists():
            try:
                content = justfile.read_text()
                # Check if there's a recipe that builds pdf (e.g. "build pdf", "pdf", "build-pdf")
                if re.search(
                    r"^\s*(?:build[-_ ]pdf|pdf)\s*[:(]", content, re.MULTILINE
                ):
                    return "deny", (
                        "This project has a justfile with a `pdf` or `build pdf` recipe. "
                        "Use `just build pdf` (or `just pdf`) instead of invoking pdflatex directly. "
                        "If you really need to invoke pdflatex directly, add `# ignore justfile` to the command."
                    )
            except OSError:
                pass
            break
        path = path.parent

    return "", None


def check_pip_command(command: str, cwd: str | None) -> tuple[str, str | None]:
    """
    Check for pip/pip3/uv-pip-add usage.

    Rules:
    - If cwd has pyproject.toml: deny pip/pip3 with message to use `uv run`
    - If cwd lacks pyproject.toml: ask for confirmation for pip/pip3
    - Always deny `uv pip add` with message to use `uv add`
    """
    # Check for `uv pip add`
    uv_words = find_command(command, "uv")
    if (
        uv_words
        and len(uv_words) >= 3
        and uv_words[1] == "pip"
        and uv_words[2] == "add"
    ):
        return "deny", "Use `uv add` instead of `uv pip add`."

    # Check for pip/pip3
    pip_words = find_command(command, "pip") or find_command(command, "pip3")
    if not pip_words:
        return "", None

    has_pyproject = False
    if cwd:
        has_pyproject = os.path.isfile(
            os.path.join(os.path.expanduser(cwd), "pyproject.toml")
        )

    if has_pyproject:
        return "deny", "Use `uv run` to run in the uv environment instead of pip."
    else:
        return "ask", "No pyproject.toml found. Are you sure you want to use pip?"
