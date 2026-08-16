"""End-to-end regression tests for bash-policy.py.

Self-contained (stdlib unittest only), matching test_anthropic_proxy.py. The hook
is driven as a subprocess through its own shebang so these tests exercise its real
input and output protocol as well as the compiled parser helper.

Run: python3 test_bash_policy.py
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
HOOK = HERE / "bash-policy.py"


def invoke_hook(cwd, command, memo=None, env_overrides=None, payload_overrides=None):
    """Return the hook's decoded JSON output, or an empty dictionary."""
    payload = {
        "tool_name": "Bash",
        "cwd": str(cwd),
        "tool_input": {"command": command},
    }
    if payload_overrides:
        payload.update(payload_overrides)
    env = dict(os.environ)
    if memo is not None:
        env["WEFT_PREFLIGHT_MEMO"] = str(memo)
    if env_overrides:
        env.update(env_overrides)
    proc = subprocess.run(
        [str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        check=False,
        text=True,
        env=env,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(f"hook failed rc={proc.returncode}: {proc.stderr[:400]}")
    out = proc.stdout.strip()
    if not out:
        return {}
    return json.loads(out)


def decide(cwd, command, memo=None, env_overrides=None, payload_overrides=None):
    """Return (permissionDecision, reason) as the harness would see them.

    ``memo`` isolates the preflight affirmation store; without it the global memo
    leaks affirmations between test cases and the deny cases spuriously pass.
    """
    output = invoke_hook(
        cwd,
        command,
        memo=memo,
        env_overrides=env_overrides,
        payload_overrides=payload_overrides,
    )
    spec = output.get("hookSpecificOutput", {})
    return spec.get("permissionDecision", ""), spec.get("permissionDecisionReason", "")


class WeftPreflightTest(unittest.TestCase):
    """Rule 12: first `weft run` of a script version, in projects with a lab-notebook/."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.proj = self.tmp / "proj"
        (self.proj / "lab-notebook").mkdir(parents=True)
        self.script = self.proj / "exp_test.py"
        self.script.write_text("# v1\n")
        self.cmd = "weft run -m x 'uv run exp_test.py'"
        self.memo = self.tmp / "memo.json"  # per-test isolation

    def tearDown(self):
        self._tmp.cleanup()

    def assertDenied(self, cwd, command):
        decision, reason = decide(cwd, command, self.memo)
        self.assertEqual(decision, "deny", f"expected deny, got {decision!r}")
        self.assertIn("preflight-checked", reason)

    def assertNotDenied(self, cwd, command):
        decision, _ = decide(cwd, command, self.memo)
        self.assertNotEqual(decision, "deny")

    def test_unaffirmed_first_run_is_denied(self):
        self.assertDenied(self.proj, self.cmd)

    def test_non_weft_command_untouched(self):
        self.assertNotDenied(self.proj, "python3 exp_test.py")

    def test_project_without_lab_notebook_is_exempt(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        (plain / "exp_test.py").write_text("# v1\n")
        self.assertNotDenied(plain, self.cmd)

    def test_marker_affirms_and_is_remembered(self):
        self.assertNotDenied(self.proj, self.cmd + "  # preflight-checked")
        self.assertNotDenied(self.proj, self.cmd)

    def test_edited_script_is_gated_again(self):
        self.assertNotDenied(self.proj, self.cmd + "  # preflight-checked")
        self.script.write_text("# v2 edited\n")
        self.assertDenied(self.proj, self.cmd)

    def test_weft_run_without_script_path_is_exempt(self):
        self.assertNotDenied(self.proj, "weft run -m x 'just build'")


class GitInJjRepoTest(unittest.TestCase):
    """Rule 7: Git mutations and unshadowed reads inside a jj repo."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_git_commit_denied_in_jj_repo(self):
        repo = self.tmp / "repo"
        (repo / ".jj").mkdir(parents=True)
        decision, _ = decide(repo, "git commit -m x")
        self.assertEqual(decision, "deny")

    def test_git_advice_does_not_skip_later_pip_denial(self):
        repo = self.tmp / "repo"
        (repo / ".jj").mkdir(parents=True)
        (repo / "pyproject.toml").touch()
        decision, reason = decide(repo, "/usr/bin/git log; pip install requests")
        self.assertEqual(decision, "deny")
        self.assertIn("uv", reason)

    def test_mutating_git_branch_operations_are_denied(self):
        repo = self.tmp / "repo"
        (repo / ".jj").mkdir(parents=True)
        for command in (
            "/usr/bin/git branch -D topic",
            "git branch -m old new",
            "git branch topic",
        ):
            with self.subTest(command=command):
                decision, _ = decide(repo, command)
                self.assertEqual(decision, "deny")

    def test_git_text_in_quoted_python_heredoc_is_not_a_command(self):
        repo = self.tmp / "repo"
        (repo / ".jj").mkdir(parents=True)
        command = "python3 - <<'PY'\nprint('git commit')\nPY"
        decision, _ = decide(repo, command)
        self.assertNotEqual(decision, "deny")

    def test_git_expansion_in_unquoted_heredoc_is_a_command(self):
        repo = self.tmp / "repo"
        (repo / ".jj").mkdir(parents=True)
        command = 'python3 - <<PY\nprint("$(git commit)")\nPY'
        decision, _ = decide(repo, command)
        self.assertEqual(decision, "deny")

    def test_git_branch_uses_shadow_wrapper_in_jj_repo(self):
        repo = self.tmp / "repo"
        (repo / ".jj").mkdir(parents=True)
        shadow_dir = self.tmp / "shadow"
        shadow_dir.mkdir()
        shadow_git = shadow_dir / "git"
        shadow_git.write_text("#!/bin/bash\nexit 0\n")
        shadow_git.chmod(0o755)
        decision, reason = decide(
            repo,
            "git status --short --branch && git remote -v && git branch --show-current",
            env_overrides={
                "LLM_SHADOW_COMMANDS_DIR": str(shadow_dir),
                "PATH": f"{shadow_dir}:{os.environ['PATH']}",
            },
            payload_overrides={"model": "test-model", "turn_id": "turn-test"},
        )
        self.assertEqual(decision, "")
        self.assertEqual(reason, "")

    def test_read_only_git_branch_warns_without_shadow_wrapper(self):
        repo = self.tmp / "repo"
        (repo / ".jj").mkdir(parents=True)
        decision, reason = decide(
            repo,
            "/usr/bin/git branch --show-current",
            env_overrides={
                "AGENT_COMMAND_GUARDS_DIR": str(self.tmp / "missing-shadow")
            },
        )
        self.assertEqual(decision, "allow")
        self.assertIn("jj bookmark list", reason)

    def test_override_comment_allows_it(self):
        repo = self.tmp / "repo"
        (repo / ".jj").mkdir(parents=True)
        decision, _ = decide(repo, "git commit -m x  # intentionally ignoring jj")
        self.assertNotEqual(decision, "deny")

    def test_plain_git_repo_unaffected(self):
        repo = self.tmp / "plain"
        (repo / ".git").mkdir(parents=True)
        decision, _ = decide(repo, "git commit -m x")
        self.assertNotEqual(decision, "deny")

    def test_git_log_warns_when_shadow_wrapper_is_absent(self):
        repo = self.tmp / "repo"
        (repo / ".jj").mkdir(parents=True)
        missing_shadow = self.tmp / "missing-shadow"

        decision, reason = decide(
            repo,
            "git log -1",
            env_overrides={"AGENT_COMMAND_GUARDS_DIR": str(missing_shadow)},
        )

        self.assertEqual(decision, "allow")
        self.assertIn("not using the Git shadow wrapper", reason)
        self.assertIn("jj log", reason)

    def test_git_reads_do_not_warn_when_shadow_wrapper_is_in_scope(self):
        repo = self.tmp / "repo"
        (repo / ".jj").mkdir(parents=True)
        shadow_dir = self.tmp / "shadow"
        shadow_dir.mkdir()
        shadow_git = shadow_dir / "git"
        shadow_git.write_text("#!/bin/bash\nexit 0\n")
        shadow_git.chmod(0o755)
        path = f"{shadow_dir}:{os.environ['PATH']}"

        for subcmd in ("log", "diff", "show", "status"):
            with self.subTest(subcmd=subcmd):
                decision, reason = decide(
                    repo,
                    f"git {subcmd}",
                    env_overrides={
                        "AGENT_COMMAND_GUARDS_DIR": str(shadow_dir),
                        "PATH": path,
                    },
                )
                self.assertEqual(decision, "allow")
                self.assertEqual(reason, "")

    def test_explicit_real_git_warns_even_when_shadow_is_on_path(self):
        repo = self.tmp / "repo"
        (repo / ".jj").mkdir(parents=True)
        shadow_dir = self.tmp / "shadow"
        shadow_dir.mkdir()
        shadow_git = shadow_dir / "git"
        shadow_git.write_text("#!/bin/bash\nexit 0\n")
        shadow_git.chmod(0o755)

        decision, reason = decide(
            repo,
            "/usr/bin/git status",
            env_overrides={
                "AGENT_COMMAND_GUARDS_DIR": str(shadow_dir),
                "PATH": f"{shadow_dir}:{os.environ['PATH']}",
            },
        )

        self.assertEqual(decision, "allow")
        self.assertIn("not using the Git shadow wrapper", reason)

    def test_override_comment_suppresses_read_warning(self):
        repo = self.tmp / "repo"
        (repo / ".jj").mkdir(parents=True)

        decision, reason = decide(
            repo,
            "git log  # intentionally ignoring jj",
            env_overrides={
                "AGENT_COMMAND_GUARDS_DIR": str(self.tmp / "missing-shadow")
            },
        )

        self.assertEqual(decision, "allow")
        self.assertEqual(reason, "")

    def test_git_global_options_do_not_hide_read_subcommand(self):
        repo = self.tmp / "repo"
        (repo / ".jj").mkdir(parents=True)
        missing_shadow = self.tmp / "missing-shadow"

        for command, expected_jj in (
            ("git --no-pager show @-", "jj show"),
            ("git -C elsewhere log -1", "jj log"),
        ):
            with self.subTest(command=command):
                decision, reason = decide(
                    repo,
                    command,
                    env_overrides={"AGENT_COMMAND_GUARDS_DIR": str(missing_shadow)},
                )
                self.assertEqual(decision, "allow")
                self.assertIn(expected_jj, reason)


class CodexOutputAdapterTest(unittest.TestCase):
    """Codex PreToolUse accepts deny, context, or allow with updatedInput."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.codex_payload = {"model": "test-model", "turn_id": "turn-test"}

    def tearDown(self):
        self._tmp.cleanup()

    def test_bare_allow_emits_nothing(self):
        output = invoke_hook(
            self.tmp,
            "jj status",
            payload_overrides=self.codex_payload,
        )
        self.assertEqual(output, {})

    def test_allow_with_advice_becomes_additional_context(self):
        (self.tmp / ".jj").mkdir()
        output = invoke_hook(
            self.tmp,
            "git log -1",
            env_overrides={
                "AGENT_COMMAND_GUARDS_DIR": str(self.tmp / "missing-shadow")
            },
            payload_overrides=self.codex_payload,
        )
        hook_output = output["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", hook_output)
        self.assertIn("Use `jj log` instead", hook_output["additionalContext"])

    def test_deny_is_preserved(self):
        (self.tmp / ".jj").mkdir()
        output = invoke_hook(
            self.tmp,
            "git commit -m x",
            payload_overrides=self.codex_payload,
        )
        hook_output = output["hookSpecificOutput"]
        self.assertEqual(hook_output["permissionDecision"], "deny")
        self.assertIn("Use `jj`", hook_output["permissionDecisionReason"])

    def test_ask_is_conservatively_denied(self):
        output = invoke_hook(
            self.tmp,
            "pip install requests",
            payload_overrides=self.codex_payload,
        )
        hook_output = output["hookSpecificOutput"]
        self.assertEqual(hook_output["permissionDecision"], "deny")
        self.assertIn("Are you sure", hook_output["permissionDecisionReason"])

    def test_uv_run_is_rewritten_with_full_tool_input(self):
        output = invoke_hook(
            self.tmp,
            "uv run python experiment.py",
            payload_overrides={
                **self.codex_payload,
                "tool_input": {
                    "command": "uv run python experiment.py",
                    "timeout": 30,
                },
            },
        )
        hook_output = output["hookSpecificOutput"]
        self.assertEqual(hook_output["permissionDecision"], "allow")
        updated = hook_output["updatedInput"]
        self.assertEqual(updated["timeout"], 30)
        self.assertIn("ram-guard", updated["command"])

    def test_uv_run_bypass_is_not_rewritten(self):
        output = invoke_hook(
            self.tmp,
            "LLM_RAM_GUARD=off uv run python experiment.py",
            payload_overrides=self.codex_payload,
        )
        self.assertNotIn("updatedInput", output.get("hookSpecificOutput", {}))


class ClaudeOutputAdapterTest(unittest.TestCase):
    """Claude PreToolUse preserves its native ask decision."""

    def test_ask_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            decision, reason = decide(tmp, "pip install requests")
        self.assertEqual(decision, "ask")
        self.assertIn("Are you sure", reason)

    def test_uv_run_is_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = invoke_hook(tmp, "uv run python experiment.py")
        hook_output = output["hookSpecificOutput"]
        self.assertEqual(hook_output["permissionDecision"], "allow")
        self.assertIn("ram-guard", hook_output["updatedInput"]["command"])


class BashSyntaxTest(unittest.TestCase):
    """Bash constructs parse without crashing the global Bash gate."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_hook_handles_supported_bash_syntax(self):
        commands = (
            'case ":$PATH:" in *":$HOME/.local/bin:"*) echo present;; esac',
            "echo $((1 + 2))",
            "time sleep 0.01",
        )

        for command in commands:
            with self.subTest(command=command):
                output = invoke_hook(self.tmp, command)
                self.assertEqual(
                    output["hookSpecificOutput"]["permissionDecision"], "allow"
                )

    def test_hook_fails_open_for_invalid_syntax(self):
        self.assertEqual(invoke_hook(self.tmp, "echo 'unterminated"), {})


class SetupTest(unittest.TestCase):
    def test_setup_works_outside_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            home.mkdir()
            result = subprocess.run(
                [str(HERE / "setup")],
                cwd=tmp_path,
                env={**os.environ, "HOME": str(home)},
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((home / ".claude/hooks/bash-policy-hook").is_symlink())


if __name__ == "__main__":
    unittest.main(verbosity=2)
