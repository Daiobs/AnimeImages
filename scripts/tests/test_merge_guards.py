"""Offline event replays; no labels, settings, or remote PRs are changed."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from protected_files import APPROVAL_CONTEXT, inspect_pull_request  # noqa: E402
from risk_tier import classify, load_rules, markdown_summary  # noqa: E402


class FakeGitHub:
    def __init__(self, files, labels=()):
        self.files = files
        self.pr = {
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40, "ref": "main"},
            "changed_files": len(files),
            "labels": [{"name": name} for name in labels],
        }
        self.calls = []
        self.statuses = {}
        self.change_on_second_read = None
        self.reads = 0

    def request(self, method, path, data=None):
        self.calls.append((method, path))
        if method == "DELETE":
            self.pr["labels"] = []
            return None
        if method == "POST" and path.startswith("/statuses/"):
            self.statuses[path.split("/")[-1]] = [
                {**data, "creator": {"login": "github-actions[bot]"}}
            ]
            return None
        if path.startswith("/commits/"):
            page = int(path.rsplit("=", 1)[1])
            return self.statuses.get(path.split("/")[2], [])[(page - 1) * 100:page * 100]
        if "/files?" in path:
            page = int(path.rsplit("=", 1)[1])
            return self.files[(page - 1) * 100:page * 100]
        self.reads += 1
        if self.reads == 2 and self.change_on_second_read:
            self.pr["head"]["sha"] = self.change_on_second_read
        return copy.deepcopy(self.pr)


def event(action="opened"):
    return {
        "number": 108, "action": action,
        "pull_request": {"head": {"sha": "a" * 40}},
        "label": {"name": "human-approved"},
        "sender": {"type": "User", "login": "reviewer"},
    }


class MergeGuardTests(unittest.TestCase):
    def setUp(self):
        self.rules = load_rules()

    def test_risk_tiers_examples_and_highest_match(self):
        cases = [
            (["templates/xhs_download/result.html"], "L0"),
            (["xhs_download/templates/result.html"], "L0"),
            (["docs/example.md"], "L0"),
            (["static/js/ytdlp_result_picker.js"], "L1"),
            (["new/unknown.file"], "L1"),
            (["xhs_download/services/common.py"], "L2"),
            (["static/js/ordinary_media_download_sw.js"], "L2"),
            (["static/js/ordinary_media_streaming_protocol.js"], "L2"),
            (["docs/product-invariants.md"], "L2"),
            (["contracts/test_backend_contracts.py"], "L2"),
            (["AGENTS.md"], "L2"),
            (["templates/result.html", ".github/workflows/protected-files.yml"], "L2"),
        ]
        for paths, tier in cases:
            with self.subTest(paths=paths):
                self.assertEqual(classify(paths, self.rules)["tier"], tier)

    def test_template_passes_without_label(self):
        api = FakeGitHub([{"filename": "templates/result.html"}])
        result = inspect_pull_request(api, event(), self.rules)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["tier"], "L0")
        self.assertFalse(any(method != "GET" for method, _ in api.calls))

    def test_common_fails_then_human_label_passes_then_push_revokes_and_fails(self):
        api = FakeGitHub([{"filename": "xhs_download/services/common.py"}])
        self.assertFalse(inspect_pull_request(api, event(), self.rules)["allowed"])
        api.pr["labels"] = [{"name": "human-approved"}]
        self.assertTrue(inspect_pull_request(api, event("labeled"), self.rules)["allowed"])
        result = inspect_pull_request(api, event("synchronize"), self.rules)
        self.assertFalse(result["allowed"])
        self.assertEqual(api.pr["labels"], [])
        self.assertIn(("DELETE", "/issues/108/labels/human-approved"), api.calls)
        self.assertFalse(result["approved"])

    def test_push_revokes_approval_for_non_l2_too(self):
        api = FakeGitHub([{"filename": "templates/result.html"}], ["human-approved"])
        self.assertTrue(inspect_pull_request(api, event("synchronize"), self.rules)["allowed"])
        self.assertEqual(api.pr["labels"], [])

    def test_changed_workflow_and_rules_are_not_executed_or_loaded_from_pr(self):
        api = FakeGitHub([
            {"filename": ".github/workflows/protected-files.yml", "patch": "exit 0"},
            {"filename": ".github/risk-tiers.json", "patch": '{"default_tier": "L0"}'},
            {"filename": "scripts/protected_files.py", "patch": "print('approved')"},
        ])
        result = inspect_pull_request(api, event(), self.rules)
        self.assertEqual(result["tier"], "L2")
        self.assertFalse(result["allowed"])
        self.assertTrue(all("/contents/" not in path for _, path in api.calls))

    def test_rename_cannot_escape_previous_protected_path(self):
        api = FakeGitHub([{
            "filename": "docs/harmless.md",
            "previous_filename": "xhs_download/services/common.py",
        }])
        self.assertFalse(inspect_pull_request(api, event(), self.rules)["allowed"])

    def test_pagination_and_incomplete_list_fail_closed(self):
        files = [{"filename": f"docs/{index}.md"} for index in range(101)]
        files.append({"filename": "xhs_download/services/common.py"})
        api = FakeGitHub(files)
        self.assertFalse(inspect_pull_request(api, event(), self.rules)["allowed"])
        self.assertIn(("GET", "/pulls/108/files?per_page=100&page=2"), api.calls)
        api = FakeGitHub([{"filename": "docs/a.md"}])
        api.pr["changed_files"] = 2
        with self.assertRaisesRegex(RuntimeError, "Incomplete"):
            inspect_pull_request(api, event(), self.rules)

    def test_stale_event_or_mid_check_head_change_cannot_pass(self):
        api = FakeGitHub([{"filename": "templates/result.html"}])
        api.pr["head"]["sha"] = "c" * 40
        with self.assertRaisesRegex(RuntimeError, "Head changed"):
            inspect_pull_request(api, event("labeled"), self.rules)
        api = FakeGitHub([{"filename": "templates/result.html"}])
        api.change_on_second_read = "c" * 40
        with self.assertRaisesRegex(RuntimeError, "Head/base changed"):
            inspect_pull_request(api, event(), self.rules)

    def test_non_main_target_does_not_bypass(self):
        api = FakeGitHub([{"filename": "templates/result.html"}])
        api.pr["base"]["ref"] = "other-branch"
        with self.assertRaisesRegex(RuntimeError, "targeting main"):
            inspect_pull_request(api, event(), self.rules)

    def test_retained_label_cannot_approve_new_head_when_synchronize_was_not_delivered(self):
        api = FakeGitHub([{"filename": "xhs_download/services/common.py"}], ["human-approved"])
        self.assertTrue(inspect_pull_request(api, event("labeled"), self.rules)["allowed"])
        api.pr["head"]["sha"] = "c" * 40
        edited = event("edited")
        edited["pull_request"]["head"]["sha"] = "c" * 40
        self.assertFalse(inspect_pull_request(api, edited, self.rules)["allowed"])

    def test_retarget_push_retarget_and_stale_push_both_revoke(self):
        for stale in (False, True):
            with self.subTest(stale=stale):
                api = FakeGitHub([{"filename": "xhs_download/services/common.py"}], ["human-approved"])
                self.assertTrue(inspect_pull_request(api, event("labeled"), self.rules)["allowed"])
                api.pr["base"]["ref"] = "other"
                api.pr["head"]["sha"] = "c" * 40
                pushed = event("synchronize")
                pushed["pull_request"]["head"]["sha"] = "a" * 40 if stale else "c" * 40
                with self.assertRaises(RuntimeError):
                    inspect_pull_request(api, pushed, self.rules)
                self.assertEqual(api.pr["labels"], [])
                self.assertNotIn("c" * 40, api.statuses)
                api.pr["base"]["ref"] = "main"
                edited = event("edited")
                edited["pull_request"]["head"]["sha"] = "c" * 40
                self.assertFalse(inspect_pull_request(api, edited, self.rules)["allowed"])

    def test_manual_removal_revokes_and_other_label_does_not_approve(self):
        api = FakeGitHub([{"filename": "xhs_download/services/common.py"}], ["human-approved"])
        self.assertTrue(inspect_pull_request(api, event("labeled"), self.rules)["allowed"])
        api.pr["labels"] = []
        self.assertFalse(inspect_pull_request(api, event("unlabeled"), self.rules)["allowed"])
        api.pr["labels"] = [{"name": "human-approved"}]
        unrelated = event("labeled")
        unrelated["label"]["name"] = "documentation"
        self.assertFalse(inspect_pull_request(api, unrelated, self.rules)["allowed"])

    def test_non_workflow_status_is_not_approval(self):
        api = FakeGitHub([{"filename": "xhs_download/services/common.py"}], ["human-approved"])
        api.statuses["a" * 40] = [{
            "context": f"{APPROVAL_CONTEXT}/pr-108", "state": "success", "creator": {"login": "contributor"},
        }]
        self.assertFalse(inspect_pull_request(api, event("edited"), self.rules)["allowed"])

    def test_latest_revocation_wins_over_older_success_with_status_pagination(self):
        api = FakeGitHub([{"filename": "xhs_download/services/common.py"}], ["human-approved"])
        api.statuses["a" * 40] = [
            {"context": f"unrelated-{index}", "state": "success"} for index in range(100)
        ] + [
            {"context": f"{APPROVAL_CONTEXT}/pr-108", "state": "failure", "creator": {"login": "github-actions[bot]"}},
            {"context": f"{APPROVAL_CONTEXT}/pr-108", "state": "success", "creator": {"login": "github-actions[bot]"}},
        ]
        self.assertFalse(inspect_pull_request(api, event("edited"), self.rules)["allowed"])

    def test_approval_from_another_pr_cannot_be_reused_for_the_same_sha(self):
        api = FakeGitHub([{"filename": "xhs_download/services/common.py"}], ["human-approved"])
        api.statuses["a" * 40] = [{
            "context": f"{APPROVAL_CONTEXT}/pr-107", "state": "success",
            "creator": {"login": "github-actions[bot]"},
        }]
        self.assertFalse(inspect_pull_request(api, event("edited"), self.rules)["allowed"])

    def test_bot_label_event_cannot_grant_approval(self):
        api = FakeGitHub([{"filename": "xhs_download/services/common.py"}], ["human-approved"])
        automated = event("labeled")
        automated["sender"] = {"type": "Bot", "login": "automation[bot]"}
        with self.assertRaisesRegex(RuntimeError, "not an automation"):
            inspect_pull_request(api, automated, self.rules)
        self.assertEqual(api.statuses, {})

    def test_delayed_unlabel_revokes_event_head_not_new_head(self):
        api = FakeGitHub([{"filename": "xhs_download/services/common.py"}], ["human-approved"])
        self.assertTrue(inspect_pull_request(api, event("labeled"), self.rules)["allowed"])
        api.pr["head"]["sha"] = "c" * 40
        api.statuses["c" * 40] = [{
            "context": f"{APPROVAL_CONTEXT}/pr-108", "state": "success",
            "creator": {"login": "github-actions[bot]"},
        }]
        with self.assertRaisesRegex(RuntimeError, "Head changed"):
            inspect_pull_request(api, event("unlabeled"), self.rules)
        self.assertEqual(api.statuses["a" * 40][0]["state"], "failure")
        self.assertEqual(api.statuses["c" * 40][0]["state"], "success")

    def test_cli_and_summary_treat_filenames_as_data(self):
        paths = ["templates/$(touch SHOULD_NOT_EXIST)<script>|x\n.md"]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "paths.json").write_text(json.dumps(paths), encoding="utf-8")
            completed = subprocess.run([
                sys.executable, str(ROOT / "scripts/risk_tier.py"),
                "--paths-json", str(path / "paths.json"),
                "--summary", str(path / "summary.md"),
            ], cwd=path, text=True, capture_output=True, check=True)
            self.assertEqual(json.loads(completed.stdout)["tier"], "L0")
            self.assertFalse((path / "SHOULD_NOT_EXIST").exists())
            summary = (path / "summary.md").read_text()
            self.assertNotIn("<script>", summary)
            self.assertIn("&#124;", summary)
            self.assertIn("&#10;", summary)

    def test_privileged_workflow_only_checks_out_main(self):
        workflow = (ROOT / ".github/workflows/protected-files.yml").read_text()
        self.assertIn("pull_request_target:", workflow)
        self.assertIn("synchronize", workflow)
        self.assertIn("ref: main", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertNotIn("pull_request.head", workflow)
        self.assertNotIn("pip install", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("concurrency:", workflow)
        self.assertEqual(workflow.count("run:"), 1)
        self.assertIn("run: python3 scripts/protected_files.py", workflow)


if __name__ == "__main__":
    unittest.main()
