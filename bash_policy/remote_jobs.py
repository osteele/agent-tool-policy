"""Remote Jobs rules for the Bash policy hook."""

import os
import re
import subprocess
from pathlib import Path

from .shell import find_command


def check_ssh_remote_jobs_access(
    command: str, cwd: str | None
) -> tuple[str, str | None]:
    """
    Check if an SSH command is trying to access ~/.remote-jobs/ directly.
    Only applies when working directory is under ~/code/research.

    Returns:
        ("deny", reason) if trying to access remote-jobs directories
        ("", None) otherwise
    """
    # Only check if cwd is under ~/code/research
    research_dir = os.path.expanduser("~/code/research")
    if cwd:
        cwd_expanded = os.path.expanduser(cwd)
        if not cwd_expanded.startswith(research_dir):
            return "", None
    else:
        return "", None

    ssh_words = find_command(command, "ssh")
    if not ssh_words:
        return "", None

    args = ssh_words[1:]
    # Look for the remote command (after hostname)
    # SSH format: ssh [options] hostname [command]
    i = 0
    while i < len(args):
        arg = args[i]
        # Skip options that take arguments
        if arg in [
            "-i",
            "-F",
            "-J",
            "-o",
            "-p",
            "-l",
            "-L",
            "-R",
            "-D",
            "-W",
            "-b",
            "-c",
            "-m",
            "-O",
            "-S",
            "-w",
            "-E",
        ]:
            i += 2
            continue
        # Skip flag-only options
        if arg.startswith("-"):
            i += 1
            continue
        # First non-option is hostname, rest is command
        if i + 1 < len(args):
            remote_command = " ".join(args[i + 1 :])
            if (
                ".remote-jobs/logs" in remote_command
                or "~/.remote-jobs/logs" in remote_command
            ):
                return (
                    "deny",
                    "Use `remote-jobs log <job-id>` instead of accessing logs directly via SSH",
                )
            if (
                ".remote-jobs/queue" in remote_command
                or "~/.remote-jobs/queue" in remote_command
            ):
                return (
                    "deny",
                    "Use `remote-jobs list` or `remote-jobs status` instead of accessing queue directly via SSH",
                )
            if ".remote-jobs" in remote_command or "~/.remote-jobs" in remote_command:
                return (
                    "deny",
                    "Use `remote-jobs list` or `remote-jobs status` instead of accessing ~/.remote-jobs directly via SSH",
                )
        break

    return "", None


def check_remote_jobs_absolute_directory(command: str) -> tuple[str, str | None]:
    """
    Check if a remote-jobs/weft run or queue add command uses an absolute path
    (starting with / or ~) for the -C directory argument.
    The directory must be relative to the working (project) directory.

    Returns:
        ("deny", reason) if absolute path detected
        ("", None) otherwise
    """
    rj_words = find_command(command, "remote-jobs") or find_command(command, "weft")
    if not rj_words:
        return "", None

    args = rj_words[1:]
    if not args:
        return "", None

    # Check if subcommand is "run" or "queue add"
    subcmd = args[0]
    if (subcmd == "queue" and len(args) > 1 and args[1] == "add") or subcmd == "run":
        pass
    else:
        return "", None

    # Find -C argument value (the parser strips quotes, so '~/...' becomes ~/...)
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "-C" and i + 1 < len(args):
            dir_arg = args[i + 1]
            if dir_arg.startswith(("/", "~")):
                return "deny", (
                    f"The -C directory '{dir_arg}' is an absolute path. "
                    "Specify a path relative to the working (project) directory instead."
                )
        i += 1

    return "", None


def check_remote_jobs_unquoted_tilde(command: str) -> tuple[str, str | None]:
    """
    Check if a remote-jobs run command uses an unquoted ~ in the -C directory argument.
    The shell expands ~ to the LOCAL home directory before remote-jobs sees it,
    so it must be quoted to be interpreted on the remote host.

    Only applies to remote-jobs, not weft (weft uses local paths for -C).

    Returns:
        ("deny", reason) if unquoted ~ detected
        ("", None) otherwise
    """
    rj_words = find_command(command, "remote-jobs")
    if not rj_words:
        return "", None

    args = rj_words[1:]
    if not args:
        return "", None

    # Check if subcommand is "run" or "queue add"
    subcmd = args[0]
    if (subcmd == "queue" and len(args) > 1 and args[1] == "add") or subcmd == "run":
        pass
    else:
        return "", None

    # Check the raw command for unquoted -C ~/... pattern
    if (
        re.search(r"-C\s+~/", command)
        or re.search(r"-C\s+~\s", command)
        or command.rstrip().endswith("-C ~")
    ):
        return "deny", (
            "The ~ in the -C path will be expanded locally by the shell. "
            "Quote it so it's interpreted on the remote host, e.g.: "
            "-C '~/code/research/...'"
        )

    return "", None


def check_remote_jobs_wait_flag(command: str) -> tuple[str, str | None]:
    """
    Check if a remote-jobs/weft run or queue add command specifies --wait or --no-wait.
    If neither is specified, auto-approve with advice to watch the job.

    Returns:
        ("allow", advice) if neither --wait nor --no-wait is specified
        ("", None) otherwise
    """
    rj_words = find_command(command, "remote-jobs") or find_command(command, "weft")
    if not rj_words:
        return "", None

    args = rj_words[1:]
    if not args:
        return "", None

    # Check if subcommand is "run" or "queue add"
    subcmd = args[0]
    if (subcmd == "queue" and len(args) > 1 and args[1] == "add") or subcmd == "run":
        pass
    else:
        return "", None

    # Check for --wait or --no-wait in the arguments
    has_wait_flag = any(arg in ("--wait", "--no-wait") for arg in args)

    if not has_wait_flag:
        return "allow", (
            "Job submitted. Watch it with `weft status --wait $JOB_ID` "
            "so you can repair it if it fails and process results when ready."
        )

    return "", None


def _is_recently_modified(filepath: Path, cwd: str | None) -> bool:
    """Check if a file appears in jj/git status (uncommitted or recently changed)."""
    # Find the repo root
    check_dir = filepath.parent
    if cwd:
        check_dir = Path(os.path.expanduser(cwd))

    # Try jj first, then git
    for vcs_cmd in [
        ["jj", "diff", "--summary"],
        ["git", "diff", "--name-only", "HEAD"],
    ]:
        try:
            result = subprocess.run(
                vcs_cmd,
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
                cwd=str(check_dir),
            )
            if result.returncode == 0 and filepath.name in result.stdout:
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue

    return False


def _check_script_instrumentation(script_path: Path) -> list[str]:
    """Check a Python script for remote-jobs instrumentation patterns.

    Returns a list of missing pattern descriptions.
    """
    try:
        content = script_path.read_text()
    except OSError:
        return []

    missing = []
    if "flush=True" not in content and "python -u" not in content:
        missing.append("flush=True (or python -u)")
    if "Progress:" not in content and "progress:" not in content:
        missing.append("Progress: N/M lines")
    if "SIGTERM" not in content and "sigterm" not in content:
        missing.append("SIGTERM handler")
    return missing


def check_remote_jobs_script_instrumentation(
    command: str, cwd: str | None
) -> tuple[str, str | None]:
    """
    When queuing a recently-modified Python script via remote-jobs,
    check if it follows the writing-remote-scripts guidelines.

    Emits a soft "allow" with advice if patterns are missing.
    Does not block — legacy and lightweight scripts pass through.

    Returns:
        ("allow", advice) if script is missing instrumentation
        ("", None) otherwise
    """
    rj_words = find_command(command, "remote-jobs") or find_command(command, "weft")
    if not rj_words:
        return "", None

    args = rj_words[1:]
    if not args:
        return "", None

    # Only check "run" and "queue add" subcommands
    subcmd = args[0]
    if (subcmd == "queue" and len(args) > 1 and args[1] == "add") or subcmd == "run":
        pass
    else:
        return "", None

    # Extract the remote command (everything after host, skipping flags)
    # remote-jobs/weft run [-C dir] [--flags] <host> '<command>'
    # We need to find the .py file in the command string
    py_match = re.search(r"[\w/._-]+\.py", command)
    if not py_match:
        return "", None

    script_name = py_match.group(0)

    # Extract -C directory if present
    work_dir = cwd
    c_match = (
        re.search(r"-C\s+'([^']+)'", command)
        or re.search(r'-C\s+"([^"]+)"', command)
        or re.search(r"-C\s+(\S+)", command)
    )
    if c_match:
        work_dir = c_match.group(1)

    # Resolve script path locally
    # The -C path may use ~ (intended for remote), so try local equivalents
    search_dirs = []
    if work_dir:
        # Replace remote ~ with local ~
        local_work = os.path.expanduser(
            work_dir.replace("~", os.path.expanduser("~"), 1)
            if work_dir.startswith("~")
            else work_dir
        )
        search_dirs.append(Path(local_work))
    if cwd:
        search_dirs.append(Path(os.path.expanduser(cwd)))

    script_path = None
    for base in search_dirs:
        candidate = base / script_name
        if candidate.exists():
            script_path = candidate
            break

    if not script_path:
        return "", None

    # Only check recently-modified scripts
    if not _is_recently_modified(script_path, cwd):
        return "", None

    missing = _check_script_instrumentation(script_path)
    if not missing:
        return "", None

    advice = (
        f"Script {script_name} was recently modified and is missing "
        f"remote-jobs instrumentation: {', '.join(missing)}. "
        f"See writing-remote-scripts skill for guidelines."
    )
    return "allow", advice
