"""Review-opportunity integration tests kept separate from policy work in flight."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from test_bash_policy import decide


class ReviewOpportunityTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        (self.project / "lab-notebook").mkdir(parents=True)
        (self.project / "exp_test.py").write_text("# exact version\n")
        self.memo = self.root / "memo.json"
        self.command = "weft run -m x 'uv run exp_test.py'"

    def tearDown(self):
        self.temporary.cleanup()

    def test_denial_records_structured_provenance(self):
        decision, reason = decide(self.project, self.command, self.memo)
        self.assertEqual(decision, "deny")
        self.assertIn("preflight-checked", reason)

        database = self.root / "review-state" / "state.sqlite3"
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT review_kind, target_sha256, checks_json FROM opportunities"
            ).fetchone()
        self.assertEqual(row[0], "remote-preflight")
        self.assertRegex(row[1], r"^[0-9a-f]{64}$")
        check = json.loads(row[2])[0]
        self.assertIn(
            check["status"], {"pass", "fail", "unavailable", "exception", "timeout"}
        )

    def test_broken_recorder_cannot_change_the_denial(self):
        invalid_state = self.root / "not-a-directory"
        invalid_state.write_text("occupied\n")
        decision, reason = decide(
            self.project,
            self.command,
            self.memo,
            env_overrides={"AGENT_REVIEW_STATE": str(invalid_state)},
        )
        self.assertEqual(decision, "deny")
        self.assertIn("preflight-checked", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
