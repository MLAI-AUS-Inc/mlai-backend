"""Actual revision view/dispatch bodies with controlled seams; no Django DB."""
import ast
import copy
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import uuid

from .tests_editorial_dispatch_unit import selected_brief, approved_catalog, retired_catalog
from .editorial_catalog import article_brief_for_catalog
from django.db import DatabaseError
from rest_framework import status
from rest_framework.response import Response


class Query(list):
    def order_by(self, *args):
        return self

    def exclude(self, **kwargs):
        return Query(x for x in self if not all(getattr(x, k) == v for k, v in kwargs.items()))

    def first(self):
        return self[0] if self else None

    def update(self, **kwargs):
        for row in self:
            for key, value in kwargs.items():
                setattr(row, key, value)


class EditorialRevisionTests(unittest.TestCase):
    def setUp(self):
        self.strategy = approved_catalog()
        self.org = SimpleNamespace(id=1, domain="example.test")
        self.context = SimpleNamespace(organization=self.org)
        self.run = SimpleNamespace(run_id="source", organization_id=1, domain="example.test",
                                   workflow="article_generation", status="completed", github_repo="fixture/site",
                                   run_request={"editorial_brief": selected_brief()}, result={}, save=Mock())
        self.source = self.run
        self.comments = [SimpleNamespace(id=1, run=self.source, status="draft", batch_id="", body="Explain the limits")]
        self.comment_queries = []
        self.config_model = SimpleNamespace(objects=Mock())
        self.config_model.objects.filter.return_value.only.return_value.first.side_effect = self.read_config
        self.run_model = SimpleNamespace(objects=Mock())
        self.run_model.objects.filter.return_value.first.side_effect = lambda: self.source
        self.comment_model = SimpleNamespace(objects=SimpleNamespace(filter=self.query_comments))
        self.reuse = Mock(side_effect=lambda **kw: kw["payload"].update(roo_points_authorized=True))
        self.remote = Mock(return_value=SimpleNamespace(status_code=202, content=b"json", json=lambda: {"run_id": "revision"}))
        self.local = Mock(side_effect=self.create_local)
        self.feedback = Mock()
        self.view = SimpleNamespace(_resolve_run=lambda *_: (self.context, self.run, None))
        self.ns = {
            "copy": copy, "uuid": uuid, "hashlib": hashlib, "Response": Response, "status": status,
            "DatabaseError": DatabaseError, "OrganizationContentConfig": self.config_model,
            "article_brief_for_catalog": article_brief_for_catalog,
            "ContentFactoryRun": self.run_model, "VibeMarketingComponentComment": self.comment_model,
            "ContentFactoryRunStatus": SimpleNamespace(FAILED="failed"),
            "VibeMarketingComponentCommentStatus": SimpleNamespace(DRAFT="draft", SUBMITTED="submitted"),
            "transaction": SimpleNamespace(atomic=nullcontext),
            "timezone": SimpleNamespace(now=lambda: datetime(2026, 9, 11, tzinfo=timezone.utc)),
            "normalize_company_domain": lambda s: str(s or "").strip().lower(),
            "_reuse_roo_points_authorization_for_article_job": self.reuse,
            "_create_editorial_feedback_candidates": self.feedback,
            "_remote_comment_payload": lambda c, **kw: {"comment_id": str(c.id), "body": c.body},
            "_serialize_component_comment": lambda c: {"id": c.id},
            "_serialize_run": lambda r, **kw: {"run_id": r.run_id, "result": r.result},
            "_component_feedback_from_run": lambda r: r.result,
            "_get_config": lambda _: SimpleNamespace(github_repo="fixture/site"),
            "_create_local_run": self.local, "founder_actor_id_for_user": lambda _: "fixture-user",
            "_content_factory_remote_config": lambda: {"enabled": True, "base_url": "https://worker.invalid"},
            "_content_factory_headers": lambda: {}, "logger": Mock(),
            "http_client": SimpleNamespace(post=self.remote, RequestException=ConnectionError),
            "_content_factory_diagnostics": lambda *a, **kw: {},
            "_blocked_worker_payload": lambda **kw: {"error": kw["technical_error"], "retryable": kw.get("retryable", False)},
        }
        path = Path(__file__).with_name("vibe_marketing_views.py")
        tree = ast.parse(path.read_text())
        names = {"_refresh_article_editorial_payload", "_revision_editorial_payload_from_run",
                 "_call_content_factory_component_revision", "_run_belongs_to_context",
                 "_component_revision_requested_run_id"}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "VibeMarketingRunCommentsSubmitView")
        method = copy.deepcopy(next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "post"))
        method.name = "submit_under_test"
        nodes.append(method)
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(path), "exec"), self.ns)

    def read_config(self):
        if isinstance(self.strategy, Exception):
            raise self.strategy
        return SimpleNamespace(pillar_strategy=copy.deepcopy(self.strategy))

    def query_comments(self, **kwargs):
        self.comment_queries.append(kwargs)
        return Query(c for c in self.comments if all(
            c.id in value if key == "id__in" else getattr(c, key) == value
            for key, value in kwargs.items()))

    def create_local(self, **kw):
        return SimpleNamespace(run_id=kw["remote_data"]["run_id"], run_request=copy.deepcopy(kw["payload"]), result={}, save=Mock())

    def submit(self):
        return self.ns["submit_under_test"](self.view, SimpleNamespace(user=SimpleNamespace(id=1)), self.run.run_id)

    def test_original_brief_reaches_remote_and_local_without_mutating_source(self):
        before = copy.deepcopy(self.run.run_request)
        response = self.submit()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.remote.call_args.kwargs["json"].get("editorial_brief"), selected_brief())
        self.assertEqual(self.local.call_args.kwargs["payload"].get("editorial_brief"), selected_brief())
        self.assertEqual(self.run.run_request, before)

    def test_retired_policy_stops_billing_comments_and_dispatch(self):
        self.strategy = retired_catalog()
        self.assertEqual(self.submit().status_code, 400)
        self.reuse.assert_not_called()
        self.feedback.assert_not_called()
        self.remote.assert_not_called()
        self.assertEqual(self.comments[0].status, "draft")

    def test_policy_outage_is_not_legacy_permission(self):
        self.strategy = DatabaseError("fixture outage")
        self.assertEqual(self.submit().status_code, 503)
        self.reuse.assert_not_called()
        self.remote.assert_not_called()

    def test_missing_brief_in_configured_catalog_is_not_inferred(self):
        self.run.run_request = {}
        self.run.result = {"editorial_brief": selected_brief()}
        self.assertEqual(self.submit().status_code, 400)
        self.reuse.assert_not_called()
        self.remote.assert_not_called()

    def test_failed_revision_cannot_recover_another_organizations_source(self):
        self.run = SimpleNamespace(run_id="failed-child", organization_id=1, workflow="article_revision", status="failed",
                                   run_request={"source_run_id": "source"}, result={})
        self.source.organization_id = 2
        self.assertEqual(self.submit().status_code, 404)
        self.assertFalse(any(q.get("run") is self.source for q in self.comment_queries))
        self.reuse.assert_not_called()
        self.remote.assert_not_called()

    def test_failed_revision_with_missing_source_does_not_fall_back(self):
        self.run = SimpleNamespace(run_id="failed-child", organization_id=1, workflow="article_revision", status="failed",
                                   run_request={"source_run_id": "missing"}, result={})
        self.source = None
        self.assertEqual(self.submit().status_code, 404)
        self.reuse.assert_not_called()

    def test_revocation_during_billing_stops_comment_submission(self):
        def withdraw(**kw):
            self.strategy = retired_catalog()
        self.reuse.side_effect = withdraw
        self.assertEqual(self.submit().status_code, 400)
        self.remote.assert_not_called()
        self.feedback.assert_not_called()
        self.assertEqual(self.comments[0].status, "draft")

    def test_revocation_after_batch_creation_stops_post_and_retains_batch(self):
        self.feedback.side_effect = lambda **kw: setattr(self, "strategy", retired_catalog())
        response = self.submit()
        self.assertEqual(response.status_code, 400)
        self.remote.assert_not_called()
        self.assertEqual(self.comments[0].status, "submitted")
        self.assertTrue(self.comments[0].batch_id)
        self.assertEqual(self.source.result["component_feedback_latest_batch"]["status"], "submitted")
        self.assertTrue(self.source.result["component_feedback_latest_batch"]["policyBlocked"])

    def test_existing_submitted_batch_keeps_idempotent_id(self):
        self.comments[0].status = "submitted"
        self.comments[0].batch_id = "earlier-batch"
        self.assertEqual(self.submit().status_code, 202)
        self.assertEqual(self.remote.call_args.kwargs["json"]["feedback_batch_id"], "earlier-batch")
        self.feedback.assert_not_called()

    def test_camel_brief_is_normalized_and_alias_conflict_rejected(self):
        self.run.run_request = {"editorialBrief": selected_brief()}
        self.assertEqual(self.submit().status_code, 202)
        payload = self.remote.call_args.kwargs["json"]
        self.assertEqual(payload.get("editorial_brief"), selected_brief())
        self.assertNotIn("editorialBrief", payload)
        self.remote.reset_mock()
        self.run.run_request["editorial_brief"] = {**selected_brief(), "country": "NZ"}
        self.assertEqual(self.submit().status_code, 400)
        self.remote.assert_not_called()

    def test_explicit_no_offer_is_preserved(self):
        brief = {**selected_brief(), "conversion_intent": "none", "offer_id": None, "offer_version": None,
                 "no_offer_reason": "Intentional learning article"}
        self.run.run_request["editorial_brief"] = brief
        self.assertEqual(self.submit().status_code, 202)
        self.assertEqual(self.remote.call_args.kwargs["json"].get("editorial_brief"), brief)

    def test_billing_denial_does_not_submit_comments(self):
        self.reuse.side_effect = lambda **kw: Response({"code": "billing_required"}, status=409)
        self.assertEqual(self.submit().status_code, 409)
        self.remote.assert_not_called()
        self.assertEqual(self.comments[0].status, "draft")

    def test_transport_failure_keeps_submitted_batch_for_same_key_retry(self):
        self.remote.side_effect = ConnectionError("fixture transport loss")
        self.assertEqual(self.submit().status_code, 202)
        original_key = self.remote.call_args.kwargs["json"]["requested_run_id"]
        self.remote.side_effect = None
        self.assertEqual(self.submit().status_code, 202)
        self.assertEqual(self.remote.call_args.kwargs["json"]["requested_run_id"], original_key)
        self.assertEqual(self.feedback.call_count, 1)

    def test_legacy_absence_remains_outside_catalog_gate(self):
        self.strategy = {}
        self.run.run_request = {}
        self.assertEqual(self.submit().status_code, 202)
        self.assertNotIn("editorial_brief", self.remote.call_args.kwargs["json"])

    def test_matching_failed_revision_recovers_source_and_keeps_its_decision(self):
        self.run = SimpleNamespace(**{**vars(self.source), "run_id": "failed-child", "workflow": "article_revision",
                                     "status": "failed", "run_request": {"source_run_id": "source", "editorial_brief": selected_brief()}})
        self.assertEqual(self.submit().status_code, 202)
        self.assertEqual(self.remote.call_args.kwargs["json"]["source_run_id"], "source")
        self.assertEqual(self.remote.call_args.kwargs["json"]["editorial_brief"], selected_brief())

    def test_known_failed_revision_cannot_switch_to_a_different_source_brief(self):
        self.run = SimpleNamespace(**{**vars(self.source), "run_id": "failed-child", "workflow": "article_revision",
                                     "status": "failed", "run_request": {"source_run_id": "source", "editorial_brief": {
                                         **selected_brief(), "reader_task": "Different original decision"}}})
        self.assertEqual(self.submit().status_code, 409)
        self.reuse.assert_not_called()
        self.remote.assert_not_called()

    def test_source_domain_drift_and_malformed_request_require_repair(self):
        for value in ({"domain": "other.example", "editorial_brief": selected_brief()}, None, []):
            with self.subTest(value=value):
                self.run.run_request = value
                self.assertEqual(self.submit().status_code, 409)
                self.remote.assert_not_called()
                self.reuse.assert_not_called()

    def test_post_denial_preserves_batch_then_retry_uses_the_same_key(self):
        self.feedback.side_effect = lambda **kw: setattr(self, "strategy", retired_catalog())
        self.assertEqual(self.submit().status_code, 400)
        batch_id = self.comments[0].batch_id
        self.strategy = approved_catalog()
        self.assertEqual(self.submit().status_code, 202)
        self.assertEqual(self.remote.call_args.kwargs["json"]["feedback_batch_id"], batch_id)
        self.feedback.assert_called_once()

    def test_worker_policy_errors_remain_actionable_without_a_fake_running_child(self):
        for code, reason in ((409, "editorial_revision_source_conflict"),
                             (503, "editorial_catalog_unavailable"),
                             (409, "saved_editorial_policy_invalid")):
            with self.subTest(code=code, reason=reason):
                self.remote.return_value = SimpleNamespace(status_code=code, content=b"json", text="",
                    json=lambda: {"detail": {"code": reason, "message": "Reload the source"}})
                response = self.submit()
                self.assertEqual(response.status_code, code)
                self.assertEqual(response.data["code"], reason)
                self.assertEqual(response.data["detail"], "Reload the source")
                self.assertEqual(self.source.result["component_feedback_latest_batch"]["status"], "submitted")
                self.local.assert_not_called()


if __name__ == "__main__":
    unittest.main()
