"""Expensive memory acceptance is required only for the implementation it covers."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from scripts import check_bootstrap_profile_scope as scope


class BootstrapProfileScopeTests(TestCase):
    def test_current_endpoint_and_fixture_changes_require_acceptance(self):
        for name in scope.EXACT_PATHS | {"tests/test_bootstrap_run_lifetime.py", "tests/fixtures/gunicorn_memory_app.py"}:
            with self.subTest(path=name):
                self.assertTrue(scope.covers([name]))
        self.assertFalse(scope.covers(["roo/views.py", "docs/README.md", "content_factory/website_rollout.py"]))

    def test_immutable_event_bases_and_manual_branch_scope(self):
        self.assertEqual(scope.base_revision({"pull_request": {"base": {"sha": "a" * 40}}}, "pull_request"), "a" * 40)
        self.assertEqual(scope.base_revision({"before": "b" * 40}, "push"), "b" * 40)
        self.assertEqual(scope.base_revision({}, "workflow_dispatch"), "origin/main")
        for event, kind in (({}, "pull_request"), ({"before": "0" * 40}, "push"), ({"before": "--output=private"}, "push"), ({}, "unknown")):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                scope.base_revision(event, kind)

    def test_real_scope_command_emits_required_for_relevant_change_and_false_for_unrelated_hotfix(self):
        with TemporaryDirectory() as directory:
            event, output = Path(directory) / "event.json", Path(directory) / "output"
            event.write_text(json.dumps({"before": "b" * 40}))
            environment = {"GITHUB_REPOSITORY": "MLAI-AUS-Inc/mlai-backend", "GITHUB_SHA": "a" * 40,
                "GITHUB_EVENT_PATH": str(event), "GITHUB_OUTPUT": str(output), "GITHUB_EVENT_NAME": "push"}
            with patch.dict(scope.os.environ, environment, clear=True), patch.object(scope.subprocess, "check_output") as diff:
                diff.return_value = "content_factory/vibe_marketing_views.py\n"
                scope.main()
                diff.assert_called_once_with(["git", "diff", "--name-only", "--no-renames", "b" * 40, "a" * 40, "--"], cwd=scope.ROOT, text=True)
                diff.return_value = "roo/views.py\n"
                scope.main()
            self.assertEqual(output.read_text(), "required=true\nrequired=false\n")

    def test_unowned_event_cannot_skip_acceptance(self):
        with patch.dict(scope.os.environ, {"GITHUB_REPOSITORY": "unowned/repository"}, clear=True), patch.object(scope.subprocess, "check_output") as diff:
            with self.assertRaises(ValueError):
                scope.main()
            diff.assert_not_called()
