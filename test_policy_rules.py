"""Focused unit tests for individual Bash policy modules."""

import tempfile
import unittest
import unittest.mock
from pathlib import Path

from bash_policy.development import check_all_commands_safe, check_pip_command
from bash_policy.hook import adapt_decision
from bash_policy.remote_jobs import (
    check_remote_jobs_absolute_directory,
    check_remote_jobs_unquoted_tilde,
    check_remote_jobs_wait_flag,
)
from bash_policy.shell import extract_commands, find_command
from bash_policy.transfers import parse_rsync_command, parse_scp_command


class ShellParsingTest(unittest.TestCase):
    def test_extracts_compound_commands(self) -> None:
        self.assertEqual(
            extract_commands("echo one && ruff check ."),
            [["echo", "one"], ["ruff", "check", "."]],
        )

    def test_finds_nested_command(self) -> None:
        self.assertEqual(find_command("echo one; jj status", "jj"), ["jj", "status"])

    def test_mvdan_parser_supports_bash_constructs(self) -> None:
        commands = (
            (
                'case ":$PATH:" in *":$HOME/.local/bin:"*) echo present;; esac',
                [["echo", "present"]],
            ),
            ("echo $((1 + 2))", [["echo", "$((1 + 2))"]]),
            ("time sleep 0.01", [["sleep", "0.01"]]),
        )

        for command, expected in commands:
            with self.subTest(command=command):
                self.assertEqual(extract_commands(command), expected)

    def test_invalid_syntax_fails_open(self) -> None:
        self.assertEqual(extract_commands("echo 'unterminated"), [])

    def test_missing_parser_fails_open(self) -> None:
        with unittest.mock.patch.dict(
            "os.environ", {"BASH_POLICY_PARSER": "/missing/bash-policy-parser"}
        ):
            self.assertEqual(extract_commands("echo unique-missing-parser-test"), [])


class RemoteJobsPolicyTest(unittest.TestCase):
    def test_absolute_working_directory_is_denied(self) -> None:
        decision, _ = check_remote_jobs_absolute_directory(
            "weft run -C /tmp/project beta 'python experiment.py'"
        )
        self.assertEqual(decision, "deny")

    def test_relative_working_directory_is_allowed_to_continue(self) -> None:
        decision, _ = check_remote_jobs_absolute_directory(
            "weft run -C experiments beta 'python experiment.py'"
        )
        self.assertEqual(decision, "")

    def test_unquoted_tilde_is_denied(self) -> None:
        decision, _ = check_remote_jobs_unquoted_tilde(
            "remote-jobs run -C ~/code/project beta 'python experiment.py'"
        )
        self.assertEqual(decision, "deny")

    def test_explicit_wait_flag_needs_no_advice(self) -> None:
        decision, _ = check_remote_jobs_wait_flag(
            "weft run --wait beta 'python experiment.py'"
        )
        self.assertEqual(decision, "")


class DevelopmentPolicyTest(unittest.TestCase):
    def test_safe_compound_command_is_allowed(self) -> None:
        decision, _ = check_all_commands_safe("jj status && tail -40 output.log")
        self.assertEqual(decision, "allow")

    def test_uv_pip_add_is_denied(self) -> None:
        decision, _ = check_pip_command("uv pip add requests", None)
        self.assertEqual(decision, "deny")

    def test_pip_is_denied_in_uv_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "pyproject.toml").touch()
            decision, _ = check_pip_command("pip install requests", tmp)
        self.assertEqual(decision, "deny")


class HookProtocolAdapterTest(unittest.TestCase):
    def test_codex_rewrite_uses_allow_with_updated_input(self) -> None:
        output = adapt_decision(
            "allow",
            codex=True,
            updated_input={"command": "jj status"},
        )
        self.assertEqual(
            output,
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": {"command": "jj status"},
                }
            },
        )

    def test_claude_bare_allow_is_preserved(self) -> None:
        output = adapt_decision("allow", codex=False)
        self.assertEqual(
            output,
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                }
            },
        )


class TransferParsingTest(unittest.TestCase):
    def test_parses_rsync_sources_and_destination(self) -> None:
        parsed = parse_rsync_command(["-av", "host:runs/", "runs/"])
        self.assertEqual(parsed["sources"], ["host:runs/"])
        self.assertEqual(parsed["dest"], "runs/")

    def test_parses_scp_sources_and_destination(self) -> None:
        parsed = parse_scp_command(["host:file.txt", "data/"])
        self.assertEqual(parsed["sources"], ["host:file.txt"])
        self.assertEqual(parsed["dest"], "data/")


if __name__ == "__main__":
    unittest.main(verbosity=2)
