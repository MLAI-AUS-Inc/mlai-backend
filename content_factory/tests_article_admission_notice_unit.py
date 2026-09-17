"""Actual callback routing/writer ASTs with in-memory ORM seams, never a test DB."""
import ast
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from .tests_editorial_dispatch_unit import selected_brief  # Dummy DB settings only.
from .article_admission_notice import NOTICE_CODES, notice_for_run
from .vibe_marketing_workflows import DISCOVERY_WORKFLOWS
from rest_framework import status
from rest_framework.response import Response


def payload():
    digest = hashlib.sha256(json.dumps(selected_brief(), sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return {"event_type": "article_admission_attention", "job_id": "original", "run_id": "original",
            "domain": "example.test", "github_repo": "fixture/site", "workflow": "confirmed_topic",
            "emitted_at": "2026-09-11T00:00:00+00:00", "admission_notice": {
                "schema_version": "2026-09-11.1", "task_id": "attempt", "brief_sha256": digest,
                "error_code": "article_task_dispatch_uncertain"}}


class AdmissionNoticeReceiverTests(unittest.TestCase):
    def setUp(self):
        self.run = SimpleNamespace(run_id="original", workflow="article_generation", domain="example.test",
            github_repo="fixture/site", run_request={"editorial_brief": selected_brief(), "client_request_id": "original-key"},
            status="running", current_step="research", resume_available=True,
            last_event_emitted_at=datetime(2026, 9, 11, 1, tzinfo=timezone.utc),
            result={"valuable_artifact": "retained", "task_adapter_handoff": {"state": "uncertain"}}, save=Mock())
        self.locked = False
        self.reads = []
        @contextmanager
        def atomic():
            self.assertFalse(self.locked)
            self.locked = True
            try:
                yield
            finally:
                self.locked = False
        def first():
            self.assertTrue(self.locked)
            self.reads.append(True)
            return self.run
        queryset = SimpleNamespace(first=first)
        self.filter = Mock(return_value=queryset)
        self.select = Mock(return_value=SimpleNamespace(filter=self.filter))
        self.ns = {"transaction": SimpleNamespace(atomic=atomic), "status": status, "Response": Response,
                   "ContentFactoryRun": SimpleNamespace(objects=SimpleNamespace(select_for_update=self.select)),
                   "_callback_event_emitted_at": lambda data: datetime.fromisoformat(data["emitted_at"])}
        tree = ast.parse((Path(__file__).parent / "service_views.py").read_text())
        names = {"_record_article_admission_attention", "_merge_django_owned_run_result"}
        constants = {"_DJANGO_OWNED_RUN_RESULT_KEYS", "_DJANGO_OWNED_RUN_RESULT_PREFIXES"}
        nodes = [node for node in tree.body if (isinstance(node, ast.FunctionDef) and node.name in names)
                 or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in node.targets))]
        view = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ContentFactoryCallbackView")
        route = next(node for node in view.body if isinstance(node, ast.FunctionDef) and node.name == "_dispatch_callback_event")
        nodes.append(route)
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "actual-admission-callback-bodies", "exec"), self.ns)
        self.handle = self.ns["_record_article_admission_attention"]

    def test_actual_route_does_not_call_terminal_capacity_or_error_handlers(self):
        view = SimpleNamespace(_handle_generation_failed=Mock(), _handle_generation_blocked=Mock(), _handle_error=Mock())
        result = self.ns["_dispatch_callback_event"](view, payload(), event_type="article_admission_attention", job_id="original")
        self.assertEqual(result.status_code, 200)
        for handler in vars(view).values():
            handler.assert_not_called()

    def test_every_typed_code_records_only_an_observation_even_for_terminal_or_active_runs(self):
        for code in NOTICE_CODES:
            for run_status in ("queued", "running", "blocked", "failed", "cancelled", "denied", "completed"):
                with self.subTest(code=code, status=run_status):
                    self.run.status = run_status
                    self.run.result.pop("article_admission_notice", None)
                    self.run.save.reset_mock()
                    before = deepcopy({k: v for k, v in vars(self.run).items() if k not in {"save", "result"}})
                    data = payload()
                    data["admission_notice"]["error_code"] = code
                    data.update(refundable=True, auto_refunded=True, refund_points=999, resume_available=True, status="failed",
                                error="SECRET", next_action="start_and_charge_again", run_request={"editorial_brief": None})
                    response = self.handle(data)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(before, {k: v for k, v in vars(self.run).items() if k not in {"save", "result"}})
                    self.run.save.assert_called_once_with(update_fields=["result"])
                    self.assertEqual(self.run.result["valuable_artifact"], "retained")
                    self.assertNotIn("SECRET", str(self.run.result))
                    self.assertEqual(set(self.run.result["article_admission_notice"]),
                                     {"schema_version", "task_id", "brief_sha256", "error_code", "observed_at"})

    def test_unknown_run_is_retryable_and_never_created_or_bound(self):
        self.run = None
        response = self.handle(payload())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"], "article_admission_run_not_found")

    def test_cross_tenant_repository_brief_workflow_and_ids_do_not_write(self):
        for change in ({"domain": "other.test"}, {"github_repo": "fixture/other"}, {"workflow": "repo_scan"},
                       {"job_id": "other"}, {"run_id": "other"},
                       {"admission_notice": {**payload()["admission_notice"], "brief_sha256": None}}):
            with self.subTest(change=change):
                response = self.handle({**payload(), **change})
                self.assertEqual(response.status_code, 409)
                self.run.save.assert_not_called()

    def test_original_reader_cannot_be_filled_in_from_an_observation(self):
        self.run.run_request = {}
        self.assertEqual(self.handle(payload()).status_code, 409)
        self.run.save.assert_not_called()
        data = payload()
        data["admission_notice"]["brief_sha256"] = None
        self.assertEqual(self.handle(data).status_code, 200)
        self.assertEqual(self.run.run_request, {})

    def test_bad_known_history_and_conflicting_aliases_fail_without_repair(self):
        for changes in ({"editorial_brief": {}}, {"editorialBrief": None}, {"domain": "other.test"}, {"github_repo": "fixture/other"}):
            with self.subTest(changes=changes):
                self.run.run_request = {"editorial_brief": selected_brief(), **changes}
                self.assertEqual(self.handle(payload()).status_code, 409)
                self.run.save.assert_not_called()

    def test_exact_replay_or_older_notice_cannot_replace_newer_observation(self):
        self.handle(payload())
        original = deepcopy(self.run.result)
        self.run.save.reset_mock()
        self.handle(payload())
        older = payload()
        older["emitted_at"] = "2026-09-10T00:00:00+00:00"
        older["admission_notice"]["error_code"] = "editorial_brief_required"
        self.handle(older)
        self.run.save.assert_not_called()
        self.assertEqual(self.run.result, original)

    def test_future_worker_snapshot_preserves_historical_notice_without_changing_worker_status(self):
        self.handle(payload())
        merge = self.ns["_merge_django_owned_run_result"]
        updated = merge(self.run.result, {"status": "running", "valuable_artifact": "updated"})
        self.assertEqual(updated["article_admission_notice"], self.run.result["article_admission_notice"])
        self.assertEqual(updated["status"], "running")
        self.assertEqual(updated["valuable_artifact"], "updated")

    def test_dashboard_remote_refresh_also_preserves_the_recorded_attempt(self):
        tree = ast.parse((Path(__file__).parent / "vibe_marketing_views.py").read_text())
        functions = {"_run_mapping", "_merge_django_owned_article_result"}
        constants = {"PUBLISH_MERGE_EVIDENCE_RESULT_KEYS", "DJANGO_OWNED_ARTICLE_RESULT_KEYS", "DJANGO_OWNED_ARTICLE_RESULT_PREFIXES"}
        nodes = [node for node in tree.body if (isinstance(node, ast.FunctionDef) and node.name in functions)
                 or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in node.targets))]
        scope = {}
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "actual-dashboard-merge", "exec"), scope)
        self.handle(payload())
        updated = scope["_merge_django_owned_article_result"](self.run.result, {"status": "running"})
        self.assertEqual(updated.get("article_admission_notice"), self.run.result["article_admission_notice"])

    def test_recovery_advice_is_not_accepted_as_arbitrary_callback_content(self):
        for change in ({"schema_version": "unknown"}, {"error_code": "unknown"}, {"task_id": "../secret"}):
            with self.subTest(change=change):
                data = payload()
                data["admission_notice"].update(change)
                self.assertEqual(self.handle(data).status_code, 409)
                self.run.save.assert_not_called()

    def test_four_icps_and_explicit_no_offer_match_canonical_hash_without_approval(self):
        for icp in ("SMB", "BUILDER", "FOUNDER_BUILDER", "COMMUNITY", "OUTSIDE"):
            with self.subTest(icp=icp):
                brief = {**selected_brief(), "audience_id": icp}
                if icp == "OUTSIDE":
                    brief.update(conversion_intent="none", offer_id=None, offer_version=None, no_offer_reason="Intentional education")
                self.run.run_request = {"editorialBrief": brief}
                data = payload()
                data["admission_notice"]["brief_sha256"] = hashlib.sha256(json.dumps(brief, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                self.assertEqual(self.handle(data).status_code, 200)
                self.assertEqual(self.run.run_request, {"editorialBrief": brief})

    def test_missing_timestamp_is_not_a_newer_notice(self):
        with self.assertRaises(ValueError):
            notice_for_run(vars(self.run), payload(), None)

    def test_polling_result_keeps_the_notice_in_the_actual_compact_allowlist(self):
        tree = ast.parse((Path(__file__).parent / "vibe_marketing_views.py").read_text())
        assignment = next(n for n in tree.body if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "COMPACT_RUN_RESULT_KEYS" for t in n.targets))
        self.assertIn("article_admission_notice", ast.literal_eval(assignment.value))
        functions = {"_run_mapping", "_compact_result_value", "_compact_result_for_run"}
        constants = {"COMPACT_RUN_RESULT_KEYS", "COMPACT_AUTOFILL_LIST_LIMITS", "DISCOVERY_WORKFLOWS"}
        nodes = [node for node in tree.body if (isinstance(node, ast.FunctionDef) and node.name in functions)
                 or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in node.targets))]
        scope = {"DISCOVERY_WORKFLOWS": DISCOVERY_WORKFLOWS}
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "actual-compact-result", "exec"), scope)
        self.handle(payload())
        compact = scope["_compact_result_for_run"](self.run)
        self.assertEqual(compact["article_admission_notice"], self.run.result["article_admission_notice"])
        self.assertNotIn("error_code", compact)


if __name__ == "__main__":
    unittest.main()
