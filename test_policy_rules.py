"""Focused unit tests for individual Bash policy modules."""

import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from bash_policy.adapters import CLAUDE_ADAPTER, CODEX_ADAPTER, KIMI_ADAPTER
from bash_policy.development import (
    check_all_commands_safe,
    check_jj_interactive_commands,
    check_pip_command,
    check_ruff_commands,
)
from bash_policy.engine import evaluate_policies
from bash_policy.hook import select_host
from bash_policy.models import Decision, FunctionPolicy, Request, Resolution
from bash_policy.registry import POLICIES
from bash_policy.remote_jobs import (
    check_remote_jobs_absolute_directory,
    check_remote_jobs_unquoted_tilde,
    check_remote_jobs_wait_flag,
)
from bash_policy.rewrites import RAM_GUARD, needs_ram_guard, rewrite_for_ram_guard
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


class JjInteractiveCommandTest(unittest.TestCase):
    def assert_denied(self, command: str, cwd: str | None = None) -> str:
        decision, reason = check_jj_interactive_commands(command, cwd)
        self.assertEqual(decision, "deny", command)
        assert reason is not None
        return reason

    def assert_allowed(self, command: str, cwd: str | None = None) -> None:
        decision, _ = check_jj_interactive_commands(command, cwd)
        self.assertEqual(decision, "", command)

    def test_editor_opening_commands_are_denied(self) -> None:
        for command in (
            "jj describe",
            "jj desc",
            "jj commit",
            "jj squash",
            "jj diffedit",
            "jj arrange",
            "jj resolve",
            "jj resolve src/main.py",
            "jj config edit",
            "jj sparse edit",
            "jj restore -i",
            "jj absorb -i",
            "jj commit -i -m 'wip'",
            "jj squash --tool meld",
            "jj describe -m 'wip' --editor",
            "jj unsquash --interactive",
        ):
            with self.subTest(command=command):
                self.assert_denied(command)

    def test_non_interactive_forms_are_not_denied(self) -> None:
        for command in (
            "jj describe -m 'fix: thing'",
            "jj describe --message='fix: thing'",
            "jj describe --stdin",
            "jj commit -m 'feat: thing'",
            "jj squash -m 'fix: thing'",
            "jj squash -u",
            "jj squash --use-destination-message",
            "jj resolve --list",
            "jj resolve --tool :ours",
            "jj restore src/main.py",
            "jj absorb",
            "jj log",
            "jj edit @-",
            "jj new",
            "jj config list",
        ):
            with self.subTest(command=command):
                self.assert_allowed(command)

    def test_split_is_allowed_when_paths_and_a_message_are_given(self) -> None:
        for command in (
            "jj split -m 'part one' a.txt",
            "jj split -p -m 'part one' a.txt",
            "jj split --message='part one' a.txt b.txt",
            "jj split -m'part one' a.txt",
            "jj split -pm 'part one' a.txt",
            "jj split -m 'part one' -- a.txt",
        ):
            with self.subTest(command=command):
                self.assert_allowed(command)

    def test_split_is_denied_when_it_would_open_an_editor(self) -> None:
        # No filesets: jj opens a diff editor to select the changes.
        self.assertIn("/jj-split", self.assert_denied("jj split"))
        self.assertIn("/jj-split", self.assert_denied("jj split -m 'part one'"))
        self.assertIn("/jj-split", self.assert_denied("jj split -m 'part one' -r @"))
        # A cluster whose last letter takes the value leaves no fileset behind.
        self.assertIn("/jj-split", self.assert_denied("jj split -pm 'part one'"))
        # Filesets but no message: jj opens an editor per new description.
        self.assertIn("-m/--message", self.assert_denied("jj split a.txt"))
        for command in (
            "jj split -i -m 'part one' a.txt",
            "jj split --tool meld -m 'part one' a.txt",
            "jj split -m 'part one' --editor a.txt",
        ):
            with self.subTest(command=command):
                self.assert_denied(command)

    def test_diff_formatting_tools_are_not_treated_as_editors(self) -> None:
        # --tool names a diff formatter on read-only commands, not an editor.
        for command in (
            "jj diff --tool difft",
            "jj log --tool difft",
            "jj show --tool difft",
        ):
            with self.subTest(command=command):
                self.assert_allowed(command)

    def test_help_is_not_denied(self) -> None:
        for command in ("jj split --help", "jj describe -h"):
            with self.subTest(command=command):
                self.assert_allowed(command)

    def test_override_comment_permits_the_command(self) -> None:
        self.assert_allowed("jj describe  # intentionally interactive")
        self.assert_allowed("jj split # intentionally interactive: rebasing by hand")

    def test_reason_names_the_alternative_and_the_override(self) -> None:
        reason = self.assert_denied("jj squash")
        self.assertIn("jj squash -m", reason)
        self.assertIn("# intentionally interactive", reason)

    def test_global_options_precede_the_subcommand(self) -> None:
        self.assert_denied("jj -R /tmp/repo --ignore-working-copy describe")
        self.assert_allowed("jj -R /tmp/repo --config ui.color=never describe -m 'x'")

    def test_later_commands_in_a_compound_are_checked(self) -> None:
        self.assert_denied("jj status && jj commit")

    def test_attached_short_value_is_not_read_as_another_flag(self) -> None:
        # -fmain is --from=main; the letters of its value are not options, so
        # neither the 'm' of -m nor the 'i' of -i may be read out of it.
        self.assert_denied("jj squash -fmain")
        self.assert_allowed("jj squash -fmain -m 'fix: thing'")
        self.assert_allowed("jj squash -m'fix: thing'")

    def test_clustered_short_flags_are_read_as_options(self) -> None:
        self.assert_denied("jj squash -im 'fix: thing'")
        self.assert_allowed("jj split -pm 'part one' a.txt")

    def test_squash_allows_a_blank_description_on_either_side(self) -> None:
        with unittest.mock.patch(
            "bash_policy.development._jj_rev_has_blank_description"
        ) as blank:
            blank.side_effect = lambda rev, cwd: rev == "abc"
            self.assert_allowed("jj squash --from abc --into def", "/tmp/repo")
            self.assert_denied("jj squash -f xyz -t def", "/tmp/repo")

    def test_guard_survives_an_unparseable_command(self) -> None:
        with unittest.mock.patch.dict(
            os.environ, {"BASH_POLICY_PARSER": "/nonexistent/parser"}
        ):
            self.assert_denied("jj status && jj commit")
            self.assert_allowed("jj commit -m 'feat: thing'")


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


class RamGuardRewriteTest(unittest.TestCase):
    def test_rewrites_uv_run(self) -> None:
        request = build_request("uv run python experiment.py", "/tmp/project")
        rewritten = rewrite_for_ram_guard(request)
        self.assertIsNotNone(rewritten)
        self.assertIn("ram-guard", rewritten or "")
        self.assertIn("/bin/zsh -c", rewritten or "")
        self.assertIn("uv run python experiment.py", rewritten or "")

    def test_uses_guard_name_when_guard_is_on_path(self) -> None:
        request = build_request("uv run python experiment.py", "/tmp/project")
        with unittest.mock.patch(
            "bash_policy.rewrites.shutil.which", return_value=str(RAM_GUARD)
        ):
            rewritten = rewrite_for_ram_guard(request)
        self.assertEqual(
            rewritten,
            "ram-guard -- /bin/zsh -c 'uv run python experiment.py'",
        )

    def test_uses_absolute_guard_when_different_guard_is_on_path(self) -> None:
        request = build_request("uv run python experiment.py", "/tmp/project")
        with unittest.mock.patch(
            "bash_policy.rewrites.shutil.which",
            return_value="/usr/local/bin/ram-guard",
        ):
            rewritten = rewrite_for_ram_guard(request)
        self.assertTrue(rewritten.startswith(f"{RAM_GUARD} -- "))

    def test_rewrites_prefixed_and_compound_uv_run(self) -> None:
        for command in (
            "mise exec -- uv run python experiment.py",
            "uv sync && uv run python experiment.py",
            "/Users/test/.local/bin/uv run python experiment.py",
        ):
            with self.subTest(command=command):
                self.assertTrue(needs_ram_guard(build_request(command, None)))

    def test_does_not_rewrite_other_uv_subcommands(self) -> None:
        for command in ("uv sync", "uv tool run ruff", "echo uv run", "uv run --help"):
            with self.subTest(command=command):
                expected = command == "uv run --help"
                self.assertEqual(
                    needs_ram_guard(build_request(command, None)), expected
                )

    def test_explicit_bypass_and_existing_guard_are_preserved(self) -> None:
        for command in (
            "LLM_RAM_GUARD=off uv run python experiment.py",
            "ram-guard -- uv run python experiment.py",
            "LLM_RAM_GUARD_ACTIVE=1 uv run python experiment.py",
        ):
            with self.subTest(command=command):
                self.assertFalse(needs_ram_guard(build_request(command, None)))


class HostSelectionTest(unittest.TestCase):
    def test_each_host_is_recognized(self) -> None:
        for payload, expected in (
            ({}, "claude"),
            ({"client_type": "kimi_code_cli"}, "kimi"),
            ({"model": "gpt-5"}, "codex"),
            ({"turn_id": "t1"}, "codex"),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(select_host(payload, []), expected)

    def test_explicit_host_wins_over_sniffing(self) -> None:
        payload = {"client_type": "kimi_code_cli"}
        self.assertEqual(select_host(payload, ["--host", "opencode"]), "opencode")

    def test_unknown_host_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            select_host({}, ["--host", "emacs"])


class HookProtocolAdapterTest(unittest.TestCase):
    def test_kimi_drops_a_rewrite_it_cannot_apply(self) -> None:
        # Verified against Kimi Code CLI 0.36.1: updatedInput is ignored and the
        # original command runs, so emitting one would only look like coverage.
        self.assertIsNone(
            KIMI_ADAPTER.render(Resolution(None), updated_input={"command": "guarded"})
        )

    def test_kimi_denies_rather_than_asking(self) -> None:
        output = KIMI_ADAPTER.render(Resolution("ask", "confirm?"))
        assert output is not None
        decision = output["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertEqual(decision["permissionDecisionReason"], "confirm?")

    def test_kimi_says_nothing_when_there_is_nothing_to_say(self) -> None:
        self.assertIsNone(KIMI_ADAPTER.render(Resolution("allow")))

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
