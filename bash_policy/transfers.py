"""Transfers rules for the Bash policy hook."""

import os
import subprocess
from pathlib import Path

from .shell import extract_commands, find_command

EXCLUDED_DIRS = {
    ".venv",
    ".git",
    ".jj",
    ".conda",
    "__pycache__",
    ".ruff_cache",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
}


APPROVED_DIRS = {"runs", "data", "reports", "output", "outputs"}


MUTAGEN_EXCLUDED_DIRS = {"data", "out", "output", "outputs", "report", "reports"}


def get_mutagen_sessions() -> list[dict]:
    """Get all mutagen sync sessions with their status."""
    try:
        result = subprocess.run(
            ["mutagen", "sync", "list"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []

        sessions = []
        current_session: dict = {}
        current_side: str | None = None

        for line in result.stdout.splitlines():
            stripped_line = line.strip()
            if stripped_line.startswith("Name:"):
                if current_session:
                    sessions.append(current_session)
                current_session = {"name": stripped_line.split(":", 1)[1].strip()}
                current_side = None
            elif stripped_line.startswith("Alpha:"):
                current_side = "alpha"
            elif stripped_line.startswith("Beta:"):
                current_side = "beta"
            elif stripped_line.startswith("URL:") and current_side:
                current_session[f"{current_side}_url"] = stripped_line.split(":", 1)[
                    1
                ].strip()
            elif stripped_line.startswith("Status:"):
                current_session["status"] = stripped_line.split(":", 1)[1].strip()

        if current_session:
            sessions.append(current_session)

        return sessions
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def is_mutagen_handling_sync(
    local_path: str, remote_host: str
) -> tuple[bool, str | None]:
    """
    Check if mutagen is actively syncing the given path to the remote host.

    Returns:
        (True, session_name) if mutagen is handling this sync and not conflicted
        (False, None) otherwise
    """
    local_path = os.path.expanduser(local_path).rstrip("/")
    sessions = get_mutagen_sessions()

    for session in sessions:
        alpha_url = session.get("alpha_url", "")
        beta_url = session.get("beta_url", "")
        status = session.get("status", "")

        # Alpha is usually local, beta is usually remote
        # Check if local path is under the alpha path
        alpha_expanded = os.path.expanduser(alpha_url).rstrip("/")
        if not local_path.startswith(alpha_expanded):
            continue

        # Check if beta points to the same remote host
        beta_host = parse_remote_host(beta_url)
        if beta_host != remote_host:
            continue

        # Check if session is actively syncing (not connecting, paused, or conflicted)
        status_lower = status.lower()
        if "conflict" in status_lower:
            # Conflicted - allow manual rsync
            return False, None
        if (
            "watching" in status_lower
            or "staging" in status_lower
            or "reconciling" in status_lower
            or "scanning" in status_lower
        ):
            # Actively syncing
            return True, session.get("name")

    return False, None


def parse_remote_host(path: str) -> str | None:
    """Extract hostname from user@host:path or host:path format."""
    if ":" in path and not path.startswith("/"):
        # Could be remote:path or user@remote:path
        host_part = path.split(":", maxsplit=1)[0]
        if "@" in host_part:
            return host_part.split("@")[1]
        return host_part
    return None


def is_remote_path(path: str) -> bool:
    """Check if path is a remote path (contains host:)."""
    return parse_remote_host(path) is not None


def get_local_path(path: str, cwd: str | None = None) -> str:
    """Get the local path, expanding ~ and resolving relative paths against cwd."""
    if is_remote_path(path):
        return ""
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded) and cwd:
        expanded = os.path.abspath(os.path.join(os.path.expanduser(cwd), expanded))
    return expanded


def path_ends_with_approved_dir(path: str) -> bool:
    """Check if path ends with an approved directory name."""
    path = path.rstrip("/")
    return any(path.endswith(d) for d in APPROVED_DIRS)


def path_contains_approved_dir(path: str) -> bool:
    """Check if path contains an approved directory as a path component."""
    normalized = path.rstrip("/") + "/"
    return any(f"/{d}/" in normalized for d in APPROVED_DIRS)


def path_contains_mutagen_excluded_dir(path: str) -> bool:
    """Check if path contains a directory commonly excluded from mutagen sync."""
    normalized = path.rstrip("/") + "/"
    return any(f"/{d}/" in normalized for d in MUTAGEN_EXCLUDED_DIRS)


def is_research_data_hostname_exception(local_path: str, remote_host: str) -> bool:
    """
    Check if transfer matches ~/.research/data/*/$hostname pattern
    where $hostname matches the remote host.
    """
    expanded = os.path.expanduser(local_path).rstrip("/")
    research_data = os.path.expanduser("~/.research/data")

    if not expanded.startswith(research_data + "/"):
        return False

    # Get the path after ~/.research/data/
    relative = expanded[len(research_data) + 1 :]
    parts = relative.split("/")

    # Should be something/*/$hostname where last part matches remote
    if len(parts) >= 2:
        last_part = parts[-1]
        if last_part == remote_host:
            return True

    return False


def check_mutagen_flush_with_rsync_fallback(command: str) -> tuple[str, str | None]:
    """
    Check if command is a mutagen flush with rsync fallback pattern.
    If mutagen is actively syncing (not conflicted), deny the rsync fallback.

    Pattern: mutagen sync flush <session> ... || rsync ...

    Returns:
        ("deny", reason) if mutagen is handling this sync
        ("", None) otherwise
    """
    cmds = extract_commands(command)
    # Look for pattern: mutagen command followed by rsync command
    mutagen_cmd = None
    has_rsync = False
    for words in cmds:
        if (
            words[0] == "mutagen"
            and len(words) >= 4
            and words[1] == "sync"
            and words[2] == "flush"
        ):
            mutagen_cmd = words
        if os.path.basename(words[0]) == "rsync":
            has_rsync = True

    if not mutagen_cmd or not has_rsync:
        return "", None

    session_name = mutagen_cmd[3]

    # Check if this session exists and is actively syncing
    sessions = get_mutagen_sessions()
    for session in sessions:
        if session.get("name") == session_name:
            status = session.get("status", "").lower()
            # If conflicted, allow the rsync fallback
            if "conflict" in status:
                return "", None
            # If actively syncing, deny - mutagen will handle it
            if any(
                s in status for s in ["watching", "staging", "reconciling", "scanning"]
            ):
                return (
                    "deny",
                    f"These directories are being continuously synced by mutagen ({session_name}). Manual rsync is not needed.",
                )

    return "", None


def parse_rsync_command(args: list[str]) -> dict:
    """Parse rsync command args to extract source, dest, exclusions, and flags."""
    result: dict = {
        "sources": [],
        "dest": None,
        "excludes": set(),
        "ignore_existing": False,
        "update": False,
    }

    i = 0
    positional = []

    while i < len(args):
        arg = args[i]

        # Handle --exclude=PATTERN or --exclude PATTERN
        if arg.startswith("--exclude="):
            result["excludes"].add(arg.split("=", 1)[1])
        elif arg == "--exclude" and i + 1 < len(args):
            i += 1
            result["excludes"].add(args[i])
        # Check for --ignore-existing flag
        elif arg == "--ignore-existing":
            result["ignore_existing"] = True
        # Check for --update / -u flag
        elif arg == "--update":
            result["update"] = True
        elif arg.startswith("-") and "u" in arg and not arg.startswith("--"):
            # Short flags like -u, -au, -avu, etc.
            result["update"] = True
        # Handle other options that take arguments
        elif arg in [
            "-e",
            "--rsh",
            "--filter",
            "-f",
            "--include",
            "--files-from",
            "--chmod",
            "--chown",
            "--groupmap",
            "--usermap",
        ]:
            i += 1  # Skip the argument
        elif not arg.startswith("-"):
            positional.append(arg)

        i += 1

    # Last positional is dest, others are sources
    if len(positional) >= 2:
        result["sources"] = positional[:-1]
        result["dest"] = positional[-1]
    elif len(positional) == 1:
        result["dest"] = positional[0]

    return result


def parse_scp_command(args: list[str]) -> dict:
    """Parse scp command args to extract source(s) and dest."""
    result: dict = {
        "sources": [],
        "dest": None,
    }

    i = 0
    positional = []

    while i < len(args):
        arg = args[i]

        # Handle options that take arguments
        if arg in ["-i", "-F", "-J", "-o", "-P", "-S", "-c", "-l"]:
            i += 1  # Skip the argument
        elif not arg.startswith("-"):
            positional.append(arg)

        i += 1

    if len(positional) >= 2:
        result["sources"] = positional[:-1]
        result["dest"] = positional[-1]
    elif len(positional) == 1:
        result["dest"] = positional[0]

    return result


def is_excluded(dirname: str, excludes: set[str]) -> bool:
    """Check if a directory name is covered by any exclude pattern."""
    if dirname in excludes:
        return True
    # Check for patterns like .venv/ or .venv/** or .venv/*
    for excl in excludes:
        base = excl.rstrip("/").rstrip("*").rstrip("/")
        if base == dirname:
            return True
    return False


def check_source_for_excluded_dirs(
    parsed: dict, cwd: str | None = None
) -> tuple[str, str | None]:
    """
    Check if source directories contain dirs that should be excluded.
    Returns ("deny", reason) if problematic dirs exist without exclusions.
    Returns ("", None) if okay.
    """
    code_dir = Path(os.path.expanduser("~/code")).resolve()

    for source in parsed["sources"]:
        if is_remote_path(source):
            continue

        local_source = get_local_path(source.rstrip("/"), cwd)
        source_path = Path(local_source).resolve()
        if not source_path.is_relative_to(code_dir):
            continue

        if not source_path.exists():
            continue

        missing_excludes = []
        for exclude_dir in EXCLUDED_DIRS:
            exclude_path = source_path / exclude_dir
            if exclude_path.exists() and not is_excluded(
                exclude_dir, parsed.get("excludes", set())
            ):
                missing_excludes.append(exclude_dir)

        if missing_excludes:
            return (
                "deny",
                f"Source contains {', '.join(missing_excludes)} without --exclude. Add: "
                + " ".join(f"--exclude={d}" for d in missing_excludes),
            )

    return "", None


def evaluate_transfer(command: str, cwd: str | None = None) -> tuple[str, str | None]:
    """
    Evaluate a transfer command.

    Returns:
        ("allow", None) - auto-approve
        ("deny", reason) - block with explanation
        ("ask", reason) - require confirmation
        ("", None) - no decision (not a transfer command)
    """
    # Find rsync or scp command in the shell pipeline
    rsync_words = find_command(command, "rsync")
    scp_words = find_command(command, "scp")

    if rsync_words:
        cmd = "rsync"
        parsed = parse_rsync_command(rsync_words[1:])
    elif scp_words:
        cmd = "scp"
        parsed = parse_scp_command(scp_words[1:])
    else:
        return "", None

    if not parsed["dest"]:
        return "", None

    sources = parsed.get("sources", [])
    dest = parsed["dest"]

    # Determine transfer direction
    remote_sources = [s for s in sources if is_remote_path(s)]
    local_sources = [s for s in sources if not is_remote_path(s)]
    dest_is_remote = is_remote_path(dest)
    dest_is_local = not dest_is_remote

    code_dir = os.path.expanduser("~/code")
    hf_cache = ".cache/huggingface/"

    # === HuggingFace cache sync (any direction) ===
    all_paths = [get_local_path(s, cwd) or s.split(":", 1)[-1] for s in sources] + [
        get_local_path(dest, cwd) or dest.split(":", 1)[-1]
    ]
    if all(hf_cache in p for p in all_paths):
        return "allow", None

    # === OUTBOUND: Local to Remote ===
    if local_sources and dest_is_remote:
        # Check for excluded dirs that should be filtered
        decision, reason = check_source_for_excluded_dirs(parsed, cwd)
        if decision == "deny":
            return decision, reason

        remote_host = parse_remote_host(dest)
        remote_path = dest.split(":", 1)[1] if ":" in dest else ""

        # Rule: dest is remote /tmp
        if remote_path.startswith("/tmp/") or remote_path == "/tmp":
            return "allow", None

        # Check if mutagen is handling this sync
        for source in local_sources:
            local_source = get_local_path(source.rstrip("/"), cwd)
            if local_source.startswith(code_dir) and remote_host:
                is_mutagen, _ = is_mutagen_handling_sync(local_source, remote_host)
                if is_mutagen:
                    return "allow", None
                # Mutagen not handling or conflicted - allow without confirmation
                return "allow", None

    # === INBOUND: Remote to Local ===
    if remote_sources and dest_is_local:
        local_dest = get_local_path(dest, cwd)
        remote_host = parse_remote_host(remote_sources[0])

        # Rule: ~/.research/data/*/$hostname exception
        if remote_host and is_research_data_hostname_exception(local_dest, remote_host):
            return "allow", None

        # Rule: dest ends with runs/data/reports/output/outputs
        if path_ends_with_approved_dir(local_dest):
            return "allow", None

        # Rule: dest is /tmp
        if local_dest.startswith("/tmp/") or local_dest == "/tmp":
            return "allow", None

        # Rule: scp/rsync to ~/code/.../{approved_dir}/ - auto-approve inbound transfers
        if local_dest.startswith(code_dir) and path_contains_approved_dir(local_dest):
            return "allow", None

    # Default: require confirmation for any other scp/rsync
    return "ask", f"{cmd} operation requires confirmation"
