"""Development rules for the Bash policy hook."""

import os
import re
import shutil
import subprocess
from pathlib import Path

from .models import FunctionPolicy, Request, decision_from_check
from .shell import build_request, extract_commands, find_command

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


def _check_ruff_request(request: Request) -> tuple[str, str | None]:
    if any(command.writes_files for command in request.commands):
        return "", None
    if not request.commands:
        return "", None

    for command in request.commands:
        words = command.words
        if not words:
            continue
        if os.path.basename(words[0]) != "ruff":
            return "", None
        if len(words) < 2 or words[1] not in ("format", "check"):
            return "", None

    return "allow", None


def check_ruff_commands(command: str) -> tuple[str, str | None]:
    """
    Auto-approve commands that only contain ruff format and/or ruff check.

    Returns:
        ("allow", None) if command only contains ruff format/check
        ("", None) otherwise
    """
    return _check_ruff_request(build_request(command, None))


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


def _check_all_commands_safe_request(request: Request) -> tuple[str, str | None]:
    if any(command.writes_files for command in request.commands):
        return "", None
    if not request.commands:
        return "", None
    if all(_is_safe_command(list(command.words)) for command in request.commands):
        return "allow", None
    return "", None


def check_all_commands_safe(command: str) -> tuple[str, str | None]:
    """
    Auto-approve when every command in a compound shell expression is safe/read-only.

    Returns:
        ("allow", None) if all commands are safe
        ("", None) otherwise
    """
    return _check_all_commands_safe_request(build_request(command, None))


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


JJ_GLOBAL_OPTIONS_WITH_VALUES = {
    "-R",
    "--repository",
    "--at-operation",
    "--at-op",
    "--color",
    "--config",
    "--config-file",
    "--config-toml",
}

JJ_SPLIT_OPTIONS_WITH_VALUES = {
    "-m",
    "--message",
    "-r",
    "--revision",
    "-o",
    "--onto",
    "-d",
    "--destination",
    "-A",
    "--insert-after",
    "--after",
    "-B",
    "--insert-before",
    "--before",
    "--tool",
}

# Short options that consume a value across the jj subcommands inspected here.
# A clustered token stops at the first of these: -fmain is --from=main, not -m.
JJ_SHORT_OPTIONS_WITH_VALUES = {
    "-A",
    "-B",
    "-R",
    "-c",
    "-d",
    "-f",
    "-m",
    "-o",
    "-r",
    "-t",
}

SHELL_OPERATORS = {"&&", "||", ";", "|", "&"}


def _short_flags(token: str) -> tuple[set[str], bool]:
    """
    Return the short options a clustered token sets, and whether its value is a
    separate token. Follows clap: the first letter that takes a value consumes
    the rest of the token as that value.
    """
    if not token.startswith("-") or token.startswith("--") or len(token) < 2:
        return set(), False
    flags: set[str] = set()
    for index, letter in enumerate(token[1:], start=1):
        if not letter.isalpha():
            return flags, False
        flag = f"-{letter}"
        flags.add(flag)
        if flag in JJ_SHORT_OPTIONS_WITH_VALUES:
            return flags, index == len(token) - 1
    return flags, False


def _jj_subcommand_index(words: list[str]) -> int | None:
    """Return the index of jj's subcommand after its global options."""
    index = 1
    while index < len(words):
        word = words[index]
        if word in JJ_GLOBAL_OPTIONS_WITH_VALUES:
            index += 2
            continue
        if word.startswith("-"):
            index += 1
            continue
        return index
    return None


def _has_flag(args: list[str], short: str | None, long: str) -> bool:
    """Check whether a jj option appears among a subcommand's arguments."""
    for arg in args:
        if arg == long or (short is not None and arg == short):
            return True
        if arg.startswith(f"{long}="):
            return True
        if short is not None and short in _short_flags(arg)[0]:
            return True
    return False


def _jj_positional_args(args: list[str], options_with_values: set[str]) -> list[str]:
    """Return a subcommand's positional arguments, skipping options and values."""
    positionals: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            positionals.extend(args[index + 1 :])
            break
        if not arg.startswith("-") or arg == "-":
            positionals.append(arg)
            index += 1
            continue
        if arg.startswith("--"):
            index += 2 if arg in options_with_values else 1
            continue
        index += 2 if _short_flags(arg)[1] else 1
    return positionals


def _jj_diff_editor_requested(args: list[str]) -> bool:
    """Check for options that start a diff editor whatever the description says."""
    return _has_flag(args, "-i", "--interactive") or _has_flag(args, None, "--tool")


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


def _jj_squash_revisions(args: list[str]) -> tuple[str, str]:
    """Return the source and destination revisions a squash would combine."""
    source = "@"
    destination = "@-"
    for index, arg in enumerate(args):
        name, _, attached = arg.partition("=")
        value = (
            attached if attached else (args[index + 1] if index + 1 < len(args) else "")
        )
        if not value:
            continue
        if name in ("-r", "--revision", "-f", "--from"):
            source = value
        elif name in ("-t", "--into"):
            destination = value
    return source, destination


def _jj_squash_reason(args: list[str], cwd: str | None) -> str | None:
    if _jj_diff_editor_requested(args):
        return "`jj squash -i`/`--tool` selects changes in a diff editor"
    if _has_flag(args, "-m", "--message") or _has_flag(
        args, "-u", "--use-destination-message"
    ):
        return None

    source, destination = _jj_squash_revisions(args)
    if cwd and (
        _jj_rev_has_blank_description(source, cwd)
        or _jj_rev_has_blank_description(destination, cwd)
    ):
        return None

    return (
        "`jj squash` without -m/--message opens an editor on the combined "
        "description. Use `jj squash -m '...'`, or `jj squash -u` to keep the "
        "destination's message"
    )


def _jj_split_reason(args: list[str]) -> str | None:
    if _jj_diff_editor_requested(args):
        return "`jj split -i`/`--tool` selects changes in a diff editor"
    if not _jj_positional_args(
        args, JJ_SPLIT_OPTIONS_WITH_VALUES | JJ_GLOBAL_OPTIONS_WITH_VALUES
    ):
        return (
            "`jj split` without filesets selects changes in a diff editor. Split "
            "explicit paths with `jj split -m '...' <paths>`, or use the "
            "`/jj-split` skill"
        )
    if not _has_flag(args, "-m", "--message"):
        return (
            "`jj split` without -m/--message opens an editor for each new "
            "description. Use `jj split -m '...' <paths>`"
        )
    return None


def _jj_resolve_reason(args: list[str]) -> str | None:
    if _has_flag(args, "-l", "--list"):
        return None
    for index, arg in enumerate(args):
        if arg == "--tool" and index + 1 < len(args):
            tool = args[index + 1]
        elif arg.startswith("--tool="):
            tool = arg.split("=", 1)[1]
        else:
            continue
        if tool in (":ours", ":theirs"):
            return None
    return (
        "`jj resolve` opens a merge tool. Use `jj resolve --list` to see the "
        "conflicts, edit the conflict markers directly, or pick a side with "
        "`jj resolve --tool :ours`/`:theirs`"
    )


def _jj_editor_reason(subcmd: str, args: list[str], cwd: str | None) -> str | None:
    """Return why a jj invocation would wait on an editor, or None if it would not."""
    if _has_flag(args, "-h", "--help"):
        return None

    if _has_flag(args, None, "--editor"):
        return f"`jj {subcmd} --editor` forces an editor open"

    if subcmd in ("describe", "desc"):
        if _has_flag(args, "-m", "--message") or _has_flag(args, None, "--stdin"):
            return None
        return (
            "`jj describe` without -m/--message opens an editor. "
            "Use `jj describe -m '...'`"
        )

    if subcmd in ("commit", "ci"):
        if _jj_diff_editor_requested(args):
            return "`jj commit -i`/`--tool` selects changes in a diff editor"
        if _has_flag(args, "-m", "--message"):
            return None
        return (
            "`jj commit` without -m/--message opens an editor. Use `jj commit -m '...'`"
        )

    if subcmd == "squash":
        return _jj_squash_reason(args, cwd)

    if subcmd == "split":
        return _jj_split_reason(args)

    if subcmd == "diffedit":
        return (
            "`jj diffedit` opens a diff editor. Use `jj squash`/`jj restore` with "
            "explicit paths"
        )

    if subcmd == "arrange":
        return (
            "`jj arrange` opens an interactive TUI. Use `jj rebase` or `jj parallelize`"
        )

    if subcmd == "resolve":
        return _jj_resolve_reason(args)

    if subcmd == "restore":
        if _jj_diff_editor_requested(args):
            return "`jj restore -i`/`--tool` selects changes in a diff editor"
        return None

    if subcmd == "config" and args[:1] in (["edit"], ["e"]):
        return "`jj config edit` opens an editor. Use `jj config set`/`jj config unset`"

    if subcmd == "sparse" and args[:1] == ["edit"]:
        return "`jj sparse edit` opens an editor. Use `jj sparse set`"

    if _has_flag(args, "-i", "--interactive"):
        return f"`jj {subcmd} -i`/`--interactive` opens an interactive editor"

    return None


def _jj_invocations(commands: list[list[str]]) -> list[tuple[str, list[str]]]:
    """Return the subcommand and arguments of each jj command."""
    invocations: list[tuple[str, list[str]]] = []
    for words in commands:
        if not words or os.path.basename(words[0]) != "jj":
            continue
        index = _jj_subcommand_index(words)
        if index is not None:
            invocations.append((words[index], words[index + 1 :]))
    return invocations


def _split_on_shell_operators(command: str) -> list[list[str]]:
    """Approximate the simple commands in a string the parser could not parse."""
    commands: list[list[str]] = [[]]
    for token in command.split():
        if token in SHELL_OPERATORS:
            commands.append([])
            continue
        commands[-1].append(token)
    return [words for words in commands if words]


def check_jj_interactive_commands(
    command: str, cwd: str | None
) -> tuple[str, str | None]:
    """
    Deny jj commands that would wait on an editor or an interactive UI, since an
    agent session has nobody at the keyboard to close it.

    An `# intentionally interactive` comment in the command overrides the guard,
    matching the `# intentionally ignoring jj` escape for Git in a jj repository.

    Uses both structured parsing and a token fallback, so the guard still works
    when the parser cannot parse the command.

    Returns:
        ("deny", reason) if a jj command would open an editor
        ("", None) otherwise
    """
    if re.search(r"#.*intentionally interactive", command):
        return "", None

    parsed_cmds = extract_commands(command)
    commands = parsed_cmds if parsed_cmds else _split_on_shell_operators(command)

    for subcmd, args in _jj_invocations(commands):
        reason = _jj_editor_reason(subcmd, args, cwd)
        if reason:
            return "deny", (
                f"{reason}. An editor needs a person at the keyboard, which this "
                f"session does not have. To run it anyway, end the command with "
                f"the comment `# intentionally interactive`."
            )

    return "", None


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


def _cwd(request: Request) -> str | None:
    return str(request.cwd) if request.cwd else None


POLICIES = (
    FunctionPolicy(
        "development.git-in-jj",
        900,
        lambda request: decision_from_check(
            check_git_in_jj_repo(request.command, _cwd(request))
        ),
    ),
    FunctionPolicy(
        "development.jj-interactive",
        890,
        lambda request: decision_from_check(
            check_jj_interactive_commands(request.command, _cwd(request))
        ),
    ),
    FunctionPolicy(
        "development.pdflatex-with-justfile",
        870,
        lambda request: decision_from_check(
            check_pdflatex_with_justfile(request.command, _cwd(request))
        ),
    ),
    FunctionPolicy(
        "development.pip",
        860,
        lambda request: decision_from_check(
            check_pip_command(request.command, _cwd(request))
        ),
    ),
    FunctionPolicy(
        "development.safe-commands",
        300,
        lambda request: decision_from_check(_check_all_commands_safe_request(request)),
    ),
    FunctionPolicy(
        "development.ruff",
        290,
        lambda request: decision_from_check(_check_ruff_request(request)),
    ),
)
