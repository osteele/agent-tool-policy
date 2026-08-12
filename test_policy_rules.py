"""Focused unit tests for individual Bash policy modules."""

import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from bash_policy.adapters import CLAUDE_ADAPTER, CODEX_ADAPTER
from bash_policy.development import (
    check_all_commands_safe,
    check_pip_command,
    check_ruff_commands,
)
from bash_policy.engine import evaluate_policies
from bash_policy.models import Decision, FunctionPolicy, Request, Resolution
from bash_policy.registry import POLICIES
from bash_policy.remote_jobs import (
    check_remote_jobs_absolute_directory,
    check_remote_jobs_unquoted_tilde,
    check_remote_jobs_wait_flag,
)
from bash_policy.shell import (
    build_request,
    extract_commands,
    find_command,
    shell_writes_files,
)
from bash_policy.transfers import (
    evaluate_transfer,
    parse_rsync_command,
    parse_scp_command,
)


class ShellParsingTest(unittest.TestCase):
    def test_extracts_compound_commands(self) -> None:
        self.assertEqual(
            extract_commands("echo one && ruff check ."),
            [["echo", "one"], ["ruff", "check", "."]],
        )

    def test_finds_nested_command(self) -> None:
        self.assertEqual(find_command("echo one; jj status", "jj"), ["jj", "status"])

    def test_detects_file_writing_redirects(self) -> None:
        self.assertTrue(shell_writes_files("echo changed > file"))
        self.assertTrue(shell_writes_files("cat source >> destination"))
        self.assertFalse(shell_writes_files("cat < source"))
        self.assertFalse(shell_writes_files("echo warning >&2"))

    def test_redirect_metadata_belongs_to_its_command(self) -> None:
        request = build_request("echo changed > file; cat source", None)
        self.assertTrue(request.commands[0].writes_files)
        self.assertFalse(request.commands[1].writes_files)

    def test_compound_redirect_propagates_to_nested_command(self) -> None:
        request = build_request("{ echo changed; } > file", None)
        self.assertTrue(request.commands[0].writes_files)

    def test_outer_redirect_does_not_attach_to_command_substitution(self) -> None:
        request = build_request('echo "$(git status)" > file', None)
        self.assertTrue(request.commands[0].writes_files)
        self.assertFalse(request.commands[1].writes_files)

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

    def test_safe_commands_with_output_redirects_are_not_allowed(self) -> None:
        for command in ("echo changed > file", "cat source > destination"):
            with self.subTest(command=command):
                decision, _ = check_all_commands_safe(command)
                self.assertEqual(decision, "")

    def test_ruff_with_output_redirect_is_not_allowed(self) -> None:
        decision, _ = check_ruff_commands("ruff check . > report.txt")
        self.assertEqual(decision, "")

    def test_read_only_nested_operations_are_allowed(self) -> None:
        for command in ("jj bookmark list", "git worktree list"):
            with self.subTest(command=command):
                decision, _ = check_all_commands_safe(command)
                self.assertEqual(decision, "allow")

    def test_mutating_nested_operations_are_not_allowed(self) -> None:
        for command in (
            "jj bookmark delete main",
            "git worktree remove ../tree",
        ):
            with self.subTest(command=command):
                decision, _ = check_all_commands_safe(command)
                self.assertEqual(decision, "")

    def test_pip_is_denied_in_uv_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "pyproject.toml").touch()
            decision, _ = check_pip_command("pip install requests", tmp)
        self.assertEqual(decision, "deny")


class PolicyEngineTest(unittest.TestCase):
    def test_deny_takes_priority_and_all_checks_run(self) -> None:
        calls: list[str] = []

        def policy(name: str, priority: int, decision: Decision):
            def evaluate(request: Request) -> Decision:
                calls.append(name)
                return decision

            return FunctionPolicy(name, priority, evaluate)

        resolution = evaluate_policies(
            Request("echo ok", None, ()),
            [
                policy("advice", 100, Decision("advice", "warning")),
                policy("deny", 1, Decision("deny", "blocked")),
                policy("late", 200, Decision("allow")),
            ],
        )
        self.assertEqual(resolution.disposition, "deny")
        self.assertEqual(resolution.reason, "blocked")
        self.assertEqual(resolution.advice, ("warning",))
        self.assertEqual(calls, ["advice", "deny", "late"])

    def test_ask_takes_priority_over_advice(self) -> None:
        resolution = evaluate_policies(
            Request("echo ok", None, ()),
            [
                FunctionPolicy(
                    "advice", 100, lambda request: Decision("advice", "warning")
                ),
                FunctionPolicy("ask", 1, lambda request: Decision("ask", "confirm")),
                FunctionPolicy("allow", 200, lambda request: Decision("allow")),
            ],
        )
        self.assertEqual(resolution.disposition, "ask")
        self.assertEqual(resolution.reason, "confirm")

    def test_priority_breaks_ties_within_a_disposition(self) -> None:
        resolution = evaluate_policies(
            Request("echo ok", None, ()),
            [
                FunctionPolicy("low", 1, lambda request: Decision("deny", "low")),
                FunctionPolicy("high", 2, lambda request: Decision("deny", "high")),
            ],
        )
        self.assertEqual(resolution.reason, "high")
        self.assertEqual(resolution.policy_name, "high")

    def test_advice_is_prioritized_deduplicated_and_aggregated(self) -> None:
        resolution = evaluate_policies(
            Request("echo ok", None, ()),
            [
                FunctionPolicy("low", 1, lambda request: Decision("advice", "low")),
                FunctionPolicy("high", 3, lambda request: Decision("advice", "high")),
                FunctionPolicy(
                    "duplicate", 2, lambda request: Decision("advice", "low")
                ),
            ],
        )
        self.assertIsNone(resolution.disposition)
        self.assertEqual(resolution.advice, ("high", "low"))

    def test_registry_has_unique_policy_names(self) -> None:
        names = [policy.name for policy in POLICIES]
        self.assertEqual(len(names), len(set(names)))


class HookProtocolAdapterTest(unittest.TestCase):
    def test_codex_rewrite_uses_allow_with_updated_input(self) -> None:
        output = CODEX_ADAPTER.render(
            Resolution("allow"),
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
        output = CLAUDE_ADAPTER.render(Resolution("allow"))
        self.assertEqual(
            output,
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                }
            },
        )

    def test_claude_preserves_ask(self) -> None:
        output = CLAUDE_ADAPTER.render(Resolution("ask", "confirm"))
        self.assertEqual(
            output,
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": "confirm",
                }
            },
        )

    def test_codex_converts_ask_to_deny(self) -> None:
        output = CODEX_ADAPTER.render(Resolution("ask", "confirm"))
        self.assertEqual(
            output,
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "confirm",
                }
            },
        )

    def test_codex_bare_allow_emits_nothing(self) -> None:
        self.assertIsNone(CODEX_ADAPTER.render(Resolution("allow")))

    def test_codex_advice_is_additional_context(self) -> None:
        output = CODEX_ADAPTER.render(Resolution(None, advice=("warning",)))
        self.assertEqual(
            output,
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": "warning",
                }
            },
        )

    def test_claude_advice_is_advisory_allow(self) -> None:
        output = CLAUDE_ADAPTER.render(Resolution(None, advice=("warning",)))
        self.assertEqual(
            output,
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": "warning",
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

    def test_relative_rsync_source_is_checked_against_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp, "home")
            project = home / "code" / "project"
            (project / ".venv").mkdir(parents=True)
            (project / ".git").mkdir()
            (project / ".jj").mkdir()
            with unittest.mock.patch.dict(os.environ, {"HOME": str(home)}):
                decision, reason = evaluate_transfer(
                    "rsync -a . beta:project/", str(project)
                )

        self.assertEqual(decision, "deny")
        self.assertIn(".venv", reason or "")
        self.assertIn(".git", reason or "")
        self.assertIn(".jj", reason or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
