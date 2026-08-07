"""End-to-end regression tests for bash-policy.py.

Self-contained (stdlib unittest only), matching test_anthropic_proxy.py.

The hook is driven as a subprocess through its own shebang rather than imported,
because it declares its dependency (``bashlex``) in PEP 723 metadata resolved by
``uv run --script``. Importing it from a plain interpreter raises ImportError, and
a test harness that swallows that failure reports a silent pass on every case —
which is exactly what happened the first time these checks were written by hand.

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


def decide(cwd, command, memo=None):
    """Return (permissionDecision, reason) as the harness would see them.

    ``memo`` isolates the preflight affirmation store; without it the global memo
    leaks affirmations between test cases and the deny cases spuriously pass.
    """
    payload = json.dumps(
        {"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": command}}
    )
    env = dict(os.environ)
    if memo is not None:
        env["WEFT_PREFLIGHT_MEMO"] = str(memo)
    proc = subprocess.run(
        [str(HOOK)],
        input=payload,
        capture_output=True,
        check=False,
        text=True,
        env=env,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(f"hook failed rc={proc.returncode}: {proc.stderr[:400]}")
    out = proc.stdout.strip()
    if not out:
        return "", ""
    spec = json.loads(out).get("hookSpecificOutput", {})
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
    """Rule 7: git mutations inside a jj repo, and the override comment."""

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
