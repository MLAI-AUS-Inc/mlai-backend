"""The actual ordinary status GET advances its existing live ledger proof."""
import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import time

from django.test import SimpleTestCase
from rest_framework.response import Response

from . import tests_website_repeat_publication_unit as fixtures


class OrdinaryPublicationPollTests(SimpleTestCase):
    def setUp(self):
        self.fixture = fixtures.RepeatPublicationTests("runTest")
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.run = self.fixture.writing

    def actual_get(self):
        from . import vibe_marketing_views as views
        tree = ast.parse(Path(__file__).with_name("vibe_marketing_views.py").read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "VibeMarketingRunView")
        get = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "get")
        get.name = "ordinary_get_under_test"
        query = Mock(); query.prefetch_related.return_value = query; query.filter.return_value = query
        query.first.return_value = self.run; query.get.return_value = self.run
        identity = lambda run, **kwargs: run
        namespace = {"__name__": "content_factory._ordinary_poll_under_test", "__package__": "content_factory",
            "time": time, "logger": Mock(), "ARTICLE_WORKFLOWS": {"direct_generate"},
            "ContentFactoryRun": SimpleNamespace(objects=query),
            "ContentFactoryRunStatus": SimpleNamespace(COMPLETED="completed"),
            "_resolve_context_or_response": lambda *args, **kwargs: (self.fixture.context, None),
            "_run_belongs_to_context": lambda *args: True, "_is_publish_child_run": lambda *args: False,
            "_latest_review_ready_component_revision": lambda *args: None,
            "_run_pending_remote_dispatch": lambda *args: False, "_recover_publish_child_for_run": Mock(),
            "_annotate_publish_handoff_staleness": identity, "_annotate_publish_child_state": identity,
            "_article_run_has_completed_local_artifacts": lambda *args: True,
            "_local_publish_child_for_run": lambda *args, **kwargs: None,
            "_publish_handoff_pending_for_run": lambda *args: False,
            "_terminal_article_system_setup_has_local_failure": lambda *args: False,
            "_publish_child_missing_remote": lambda *args: False,
            "_is_status_poll_unavailable_payload": lambda *args: False,
            "_ensure_article_live_preview": identity, "_advance_publish_child_for_poll": lambda *args, **kwargs: None,
            "_run_mapping": views._run_mapping, "_release_failed_article_keyword": Mock(),
            "_serialize_run": lambda run, **kwargs: {"runId": run.run_id,
                "release_observations": run.result.get("release_observations", {})},
            "_log_terminal_repo_scan_status": Mock(), "_timed_vibe_response": lambda payload, **kwargs: Response(payload)}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[get], type_ignores=[])),
            "vibe_marketing_views.py", "exec"), namespace)
        with patch("content_factory.island_research.refund_empty_or_failed_research"):
            return namespace["ordinary_get_under_test"](None, SimpleNamespace(query_params={"view": "status"}), self.run.run_id)

    def test_ordinary_status_get_exposes_new_live_observation_and_keeps_its_separate_charge(self):
        observation = {"state": "verified", "method": "http_article_body", "merge_commit_sha": "b" * 40}
        self.fixture.refresh.return_value = [SimpleNamespace(id=21, source_run_id=self.run.run_id, _live_observation=observation)]
        original = deepcopy(self.run.run_request)
        response = self.actual_get()
        self.assertEqual(response.status_code, 200)
        self.fixture.refresh.assert_called_once()
        self.assertEqual(response.data["release_observations"], {"21": observation})
        self.assertEqual(self.run.run_request, original)
        self.assertEqual(self.run.run_request["roo_points_ledger_id"], "ordinary-spend")
        self.assertNotIn("first_article_goal_operation_id", self.run.run_request)

    def test_first_goal_status_get_does_not_acquire_an_ordinary_repeat_refresh(self):
        self.run.run_request["first_article_goal_operation_id"] = "existing-first-goal"
        response = self.actual_get()
        self.assertEqual(response.status_code, 200)
        self.fixture.refresh.assert_not_called()
        self.fixture.selected.select_related.assert_not_called()
