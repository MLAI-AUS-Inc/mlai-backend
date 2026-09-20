"""No-database policy and actual dispatch/restart control-flow tests.

Run with unittest, never manage.py test. Selected unmodified function ASTs are
executed from the real view module without importing its application models,
credentials, workers or network clients. ORM/HTTP/billing seams are controlled
fixtures, so these tests do not prove persistence, SQL races or real billing.
"""
import ast
from copy import deepcopy
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import uuid

from django.conf import settings
from django.db import DatabaseError

if not settings.configured:
    settings.configure(SECRET_KEY="unit-fixture-only", USE_TZ=True, USE_I18N=False,
                       DATABASES={"default": {"ENGINE": "django.db.backends.dummy"}},
                       REST_FRAMEWORK={"UNAUTHENTICATED_USER": None, "DEFAULT_AUTHENTICATION_CLASSES": []})

from rest_framework import status
from rest_framework.response import Response
from .editorial_catalog import approve_catalog, article_brief_for_catalog, update_catalog
from .tests_editorial_catalog_unit import approval_payload
from .tests_editorial_catalog_unit import approved_catalog, draft_catalog, edit_payload


def selected_brief():
    return {"audience_id": "BUILDER", "audience_version": 1, "offer_id": "studio", "offer_version": 1,
            "conversion_intent": "offer", "no_offer_reason": None, "country": "AU",
            "reader_task": "Show tested work", "distinct_contribution": "A reproducible example",
            "acceptance_criteria": ["Run the example"]}


def retired_catalog():
    original = approved_catalog()
    edit = edit_payload(original)
    edit["cta_options"][0].update(version=2, status="retired", approved_by=None, approved_at=None)
    return update_catalog(original, edit)


class CurrentArticleBriefTests(unittest.TestCase):
    def test_ci_includes_the_no_database_editorial_regressions(self):
        workflow = (Path(__file__).resolve().parent.parent / ".github/workflows/deploy.yml").read_text()
        step = workflow.split("- name: Run no-database editorial contract checks\n", 1)[1].split("\n    - ", 1)[0]
        self.assertIn("python -m unittest", step)
        for module in ("tests_editorial_catalog_unit", "tests_editorial_catalog_api_unit", "tests_editorial_dispatch_unit", "tests_editorial_snapshot_unit", "tests_editorial_revision_unit", "tests_article_admission_notice_unit"):
            self.assertIn("content_factory." + module, step)
        self.assertNotIn("manage.py", step)

    def test_all_four_icps_and_explicit_outside_keep_their_own_decision(self):
        for audience in ("SMB", "BUILDER", "FOUNDER_BUILDER", "COMMUNITY", "OUTSIDE"):
            with self.subTest(audience=audience):
                original = edit_payload(draft_catalog())
                original["expected_editorial_catalog_version"] = 0
                original["audience_options"][0]["id"] = audience
                original["cta_options"][0]["audience_ids"] = [audience]
                draft = update_catalog({}, original)
                approved = approve_catalog(draft, approval_payload(draft), actor_id="user:fixture", approved_at=datetime(2026, 9, 10, tzinfo=dt_timezone.utc))
                brief = {**selected_brief(), "audience_id": audience}
                if audience == "OUTSIDE":
                    brief.update(conversion_intent="none", offer_id=None, offer_version=None, no_offer_reason="Intentional non-commercial scope")
                self.assertEqual(article_brief_for_catalog(approved, {"editorial_brief": brief}), brief)

    def test_both_aliases_normalize_without_mutating_the_request(self):
        for fields in ({"editorial_brief": selected_brief()}, {"editorialBrief": selected_brief()},
                       {"editorial_brief": selected_brief(), "editorialBrief": selected_brief()}):
            with self.subTest(fields=list(fields)):
                original = deepcopy(fields)
                self.assertEqual(article_brief_for_catalog(approved_catalog(), fields), selected_brief())
                self.assertEqual(fields, original)

    def test_only_absent_catalog_and_absent_brief_preserve_legacy_behavior(self):
        self.assertIsNone(article_brief_for_catalog({}, {"topic": "Legacy"}))
        for strategy, payload in (({"editorial_catalog": {}}, {}), (draft_catalog(), {}),
                                  ({}, {"editorial_brief": selected_brief()})):
            with self.subTest(strategy=strategy), self.assertRaises(ValueError):
                article_brief_for_catalog(strategy, payload)

    def test_explicit_invalid_or_conflicting_alias_cannot_be_discarded(self):
        for value in (None, {}, [], False, ""):
            for alias in ("editorial_brief", "editorialBrief"):
                with self.subTest(alias=alias, value=value), self.assertRaises(ValueError):
                    article_brief_for_catalog({}, {alias: value})
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            article_brief_for_catalog(approved_catalog(), {"editorial_brief": selected_brief(), "editorialBrief": {**selected_brief(), "country": "NZ"}})

    def test_retired_changed_and_receiptless_policy_reject_the_stored_brief(self):
        legacy = approved_catalog()
        legacy["editorial_catalog"].pop("approval_receipts")
        changed = approved_catalog()
        changed["editorial_catalog"]["cta_options"][0]["body"] = "An unreviewed promise"
        for strategy in (retired_catalog(), draft_catalog(), legacy, changed, {"editorial_catalog": None}):
            with self.subTest(strategy=strategy), self.assertRaises(ValueError):
                article_brief_for_catalog(strategy, {"editorial_brief": selected_brief()})

    def test_no_offer_stays_no_offer_and_cannot_bypass_revoked_permission(self):
        brief = {**selected_brief(), "conversion_intent": "none", "offer_id": None,
                 "offer_version": None, "no_offer_reason": "A learning article"}
        self.assertEqual(article_brief_for_catalog(approved_catalog(), {"editorial_brief": brief}), brief)
        changed = approved_catalog()
        changed["editorial_catalog"]["audience_options"][0]["allow_no_offer"] = False
        with self.assertRaises(ValueError):
            article_brief_for_catalog(changed, {"editorial_brief": brief})


class ArticleDispatchControlFlowTests(unittest.TestCase):
    def setUp(self):
        self.strategy = approved_catalog()
        self.org = SimpleNamespace(pk="org-1", id="org-1", domain="example.test")
        self.user = SimpleNamespace(pk="user-1")
        self.context = SimpleNamespace(organization=self.org, profile=SimpleNamespace(user=self.user))
        self.config = SimpleNamespace(github_repo="fixture/site", pillar_strategy=deepcopy(self.strategy),
                                      github_connection_state="connected", publish_targets=[], authors=[], default_author_id="")
        self.config_model = SimpleNamespace(objects=Mock())
        self.config_model.objects.filter.return_value.only.return_value.first.side_effect = self.read_config
        self.posted = []
        self.lookup = Mock(return_value=("unknown", {}))
        self.refund = Mock()
        self.charge = Mock(return_value=(self.user, SimpleNamespace(id=123), 6))
        self.reuse = Mock(return_value=None)
        self.analytics = Mock(return_value={"fixture": True})
        self.local = Mock(side_effect=self.create_local)
        self.refund_pending = Mock()
        self.bind_existing = Mock(return_value="bound-existing-run")
        self.run = SimpleNamespace(run_id="source-1", workflow="article_generation", status="blocked", resume_available=False,
                                   github_repo="fixture/site", run_request={"topic": "Delivery", "editorial_brief": selected_brief()},
                                   result={}, save=Mock())
        class TransportError(Exception):
            pass
        self.transport_error = TransportError
        self.http = SimpleNamespace(RequestException=TransportError, post=Mock(side_effect=self.post))
        self.ns = {
            "uuid": uuid, "status": status, "Response": Response, "DatabaseError": DatabaseError,
            "OrganizationContentConfig": self.config_model, "article_brief_for_catalog": article_brief_for_catalog,
            "logger": Mock(), "timezone": SimpleNamespace(now=lambda: datetime(2026, 9, 10, tzinfo=dt_timezone.utc)),
            "ContentFactoryRunStatus": SimpleNamespace(FAILED="failed", BLOCKED="blocked", DENIED="denied"),
            "RESTARTABLE_ARTICLE_WORKFLOWS": {"article_generation", "content_factory_article", "direct_generate", "confirmed_topic"},
            "FAILED_RUN_STATUSES": {"failed", "blocked", "denied"},
            "CONTENT_FACTORY_REQUEST_SOURCE": "founder_tools", "CONTENT_FACTORY_ACTION_ARTICLE_GENERATION": "article_generation",
            "CONTENT_FACTORY_ACTION_CONTENT_ISLAND_TOPIC_GENERATION": "content_island_topic_generation",
            "CONTENT_FACTORY_KEYED_DISPATCH_ENDPOINTS": {"article", "discovery"}, "CONTENT_FACTORY_DISPATCH_MAX_POST_ATTEMPTS": 2,
            "CONTENT_FACTORY_KEYED_DISPATCH_WORKFLOWS": {"article_generation"}, "CONTENT_FACTORY_DISPATCH_ABSENT_GRACE_SECONDS": 180,
            "founder_actor_id_for_user": lambda user: "fixture-actor", "_get_config": lambda org: self.config,
            "_content_package_from_run": lambda run: {}, "_article_draft_title_keyword": lambda run: ("Delivery", "delivery"),
            "_effective_article_delivery_mode": lambda config, **kwargs: kwargs.get("requested_mode"),
            "_article_content_only_preview_not_available": lambda run: False,
            "_topic_is_already_written": Mock(return_value=None), "_mark_keyword_in_progress": Mock(),
            "analytics_config_for_content_factory": self.analytics,
            "_reuse_roo_points_authorization_for_article_job": self.reuse,
            "_content_factory_remote_config": lambda: {"enabled": True, "base_url": "https://worker.invalid", "api_key_configured": True, "is_local_env": True},
            "_remote_required_for_workflow": lambda workflow: True, "_mint_dispatch_client_request_id": lambda workflow: "fixture-key",
            "_content_factory_headers": lambda: {}, "_lookup_content_factory_dispatch_by_key": self.lookup,
            "_process_pending_dispatch_refund": self.refund_pending, "bind_dispatch_token_run": self.bind_existing,
            "sanitize_json_for_postgres": lambda value: value,
            "_content_factory_diagnostics": lambda *args, **kwargs: {},
            "_create_local_run": self.local, "http_client": self.http,
            "_refund_roo_points_for_article_start": self.refund, "_refund_roo_points_for_content_island_topic_start": Mock(),
            "charge_content_factory_request_for_user": self.charge, "InsufficientRooPointsError": type("Insufficient", (Exception,), {}),
            "build_roo_points_authorization_payload": lambda **kwargs: {"roo_points_billing_status": kwargs["billing_status"]},
            "get_content_factory_ai_agent_required_points": lambda domain: 6, "_roo_points_balance_for_user": lambda user: 20,
            "_resolve_context_or_response": lambda request: (self.context, None),
            "_setup_blocked_response_for_generation": lambda *args: None,
            "match_covered_topic": lambda **kwargs: None, "_github_repo_operable": lambda config: True,
            "article_system_ready": lambda config: True, "resolve_article_system": lambda config: {},
            "normalize_authors": lambda authors: authors, "resolve_default_author": lambda *args: None,
        }
        names = {"_run_mapping", "_request_value", "_bool_from_request", "_refresh_article_editorial_payload", "_friendly_content_factory_error", "_blocked_worker_payload",
                 "_restart_article_payload_from_run", "_restart_article_run", "_charge_roo_points_for_article", "_queue_content_factory_run",
                 "_run_result_from_remote", "_resolve_dispatch_token_run", "_fail_unconfirmed_dispatch_run"}
        tree = ast.parse(Path(__file__).with_name("vibe_marketing_views.py").read_text())
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        self.assertEqual({node.name for node in nodes}, names)
        article_view = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "VibeMarketingArticleView")
        article_post = next(node for node in article_view.body if isinstance(node, ast.FunctionDef) and node.name == "post")
        article_post.name = "_article_start_under_test"  # Body is unchanged; no APIView/model application setup.
        nodes.append(article_post)
        # Postponed annotations avoid importing app models just to execute these functions.
        nodes.insert(0, ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0))
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "vibe_marketing_views.py", "exec"), self.ns)

    def read_config(self):
        return SimpleNamespace(pillar_strategy=deepcopy(self.strategy))

    def post(self, url, *, json, headers, timeout):
        self.posted.append(deepcopy(json))
        return SimpleNamespace(status_code=202, content=b"fixture", json=lambda: {"run_id": "child-1", "status": "queued"})

    def create_local(self, **kwargs):
        remote = kwargs["remote_data"]
        return SimpleNamespace(run_id=remote.get("run_id") or kwargs["fallback_run_id"], status=remote.get("status"),
                               run_request=deepcopy(kwargs["payload"]), result=deepcopy(self.ns["_run_result_from_remote"](remote)),
                               workflow=kwargs["workflow"], domain=kwargs["domain"], save=Mock(),
                               created_at=self.ns["timezone"].now())

    def queue(self, *, payload=None, refund=False, endpoint="article"):
        return self.ns["_queue_content_factory_run"](
            endpoint=endpoint, workflow="article_generation" if endpoint == "article" else "auto_discovery",
            context=self.context, config=self.config,
            payload=payload if payload is not None else {"domain": self.org.domain, "editorial_brief": selected_brief()},
            billing_refund_context={"charged_user": self.user, "article_request": {"client_request_id": "fixture-key"}} if refund else None,
        )

    def start(self, data=None):
        return self.ns["_article_start_under_test"](None, SimpleNamespace(user=self.user, data=data if data is not None else {
            "topic": "Delivery", "editorial_brief": selected_brief(),
        }))

    def test_actual_start_handler_keeps_brief_through_charge_and_post(self):
        result = self.start({"topic": "Delivery", "editorialBrief": selected_brief()})
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.data["run_id"], "child-1")
        self.assertEqual(self.posted[0]["editorial_brief"], selected_brief())
        self.assertEqual(self.charge.call_args.kwargs["article_request"]["editorial_brief"], selected_brief())
        self.config_model.objects.filter.assert_called_with(organization=self.org)

    def test_actual_start_handler_rejects_explicit_invalid_brief_before_effects(self):
        result = self.start({"topic": "Delivery", "editorial_brief": None})
        self.assertEqual(result.status_code, 400)
        self.analytics.assert_not_called()
        self.charge.assert_not_called()
        self.http.post.assert_not_called()

    def test_actual_start_handler_does_not_downgrade_an_empty_configured_catalog(self):
        self.strategy = {"editorial_catalog": {}}
        self.config.pillar_strategy = deepcopy(self.strategy)
        self.assertEqual(self.start({"topic": "Delivery"}).status_code, 400)
        self.charge.assert_not_called()
        self.http.post.assert_not_called()

    def test_actual_start_rechecks_after_initial_valid_snapshot_before_charge(self):
        def provision_and_revoke(*args, **kwargs):
            self.strategy = retired_catalog()
            return {}
        self.analytics.side_effect = provision_and_revoke
        result = self.start()
        self.assertEqual(result.status_code, 400)
        self.charge.assert_not_called()
        self.http.post.assert_not_called()

    def test_actual_start_rechecks_policy_changed_during_charge_before_post(self):
        def charge_and_revoke(**kwargs):
            self.strategy = retired_catalog()
            return self.user, SimpleNamespace(id=123), 6
        self.charge.side_effect = charge_and_revoke
        result = self.start()
        self.assertEqual(result.data["status"], "blocked")
        self.charge.assert_called_once()
        self.http.post.assert_not_called()
        self.refund.assert_not_called()
        self.ns["_mark_keyword_in_progress"].assert_not_called()
        self.assertTrue(self.local.call_args.kwargs["payload"]["dispatch_pending_resolution"])

    def test_catalog_free_legacy_start_and_restart_remain_loadable(self):
        self.strategy = {}
        self.config.pillar_strategy = {}
        self.assertEqual(self.start({"topic": "Legacy"}).status_code, 202)
        self.run.run_request.pop("editorial_brief")
        child, error = self.ns["_restart_article_run"](run=self.run, context=self.context)
        self.assertIsNone(error)
        self.assertEqual(child.run_id, "child-1")
        self.assertTrue(all("editorial_brief" not in payload for payload in self.posted))

    def test_restart_preserves_the_exact_stored_brief(self):
        original = deepcopy(self.run.run_request)
        child, error = self.ns["_restart_article_run"](run=self.run, context=self.context)
        self.assertIsNone(error)
        self.assertEqual(self.posted[0]["editorial_brief"], selected_brief())
        self.assertEqual(self.posted[0]["restart_source_run_id"], "source-1")
        self.assertEqual(self.run.run_request, original)
        self.assertEqual(child.run_id, "child-1")
        self.run.save.assert_called_once()

    def test_restart_accepts_saved_camel_case_and_keeps_no_offer_explicit(self):
        self.run.run_request.pop("editorial_brief")
        brief = {**selected_brief(), "conversion_intent": "none", "offer_id": None,
                 "offer_version": None, "no_offer_reason": "Learning"}
        self.run.run_request["editorialBrief"] = brief
        child, error = self.ns["_restart_article_run"](run=self.run, context=self.context)
        self.assertIsNone(error)
        self.assertEqual(child.run_request["editorial_brief"], brief)

    def test_missing_stored_restart_brief_is_not_invented_from_generated_result(self):
        self.run.run_request.pop("editorial_brief")
        self.run.result["editorial_brief"] = selected_brief()
        child, error = self.ns["_restart_article_run"](run=self.run, context=self.context)
        self.assertIsNone(child)
        self.assertEqual(error.status_code, 409)
        self.reuse.assert_not_called()
        self.http.post.assert_not_called()

    def test_revoked_restart_does_not_reuse_billing_or_queue(self):
        self.strategy = retired_catalog()
        self.config.pillar_strategy = deepcopy(self.strategy)
        child, error = self.ns["_restart_article_run"](run=self.run, context=self.context)
        self.assertIsNone(child)
        self.assertEqual(error.status_code, 409)
        self.reuse.assert_not_called()
        self.http.post.assert_not_called()
        self.analytics.assert_not_called()
        self.run.save.assert_not_called()

    def test_revocation_after_restart_snapshot_is_checked_before_billing_reuse(self):
        self.strategy = retired_catalog()  # self.config still contains approved policy.
        child, error = self.ns["_restart_article_run"](run=self.run, context=self.context)
        self.assertIsNone(child)
        self.assertIsNotNone(error)
        self.reuse.assert_not_called()
        self.http.post.assert_not_called()

    def test_current_policy_is_checked_before_new_charge(self):
        self.strategy = retired_catalog()
        result = self.ns["_charge_roo_points_for_article"](SimpleNamespace(user=self.user, data={}), context=self.context,
                                                         payload={"editorial_brief": selected_brief()})
        self.assertEqual(result[3].status_code, 400)
        self.charge.assert_not_called()

    def test_valid_charge_keeps_the_resolved_brief_and_billing_contract(self):
        payload = {"editorialBrief": selected_brief()}
        result = self.ns["_charge_roo_points_for_article"](SimpleNamespace(user=self.user, data={}), context=self.context, payload=payload)
        self.assertIsNone(result[3])
        self.assertEqual(self.charge.call_args.kwargs["article_request"]["editorial_brief"], selected_brief())
        self.assertNotIn("editorialBrief", payload)
        self.assertEqual(payload["roo_points_billing_status"], "charged")

    def test_revocation_before_first_post_blocks_dispatch_and_verifies_prior_key_before_refund(self):
        self.strategy = retired_catalog()
        child = self.queue(refund=True)
        self.http.post.assert_not_called()
        self.lookup.assert_called_once()
        self.refund.assert_not_called()
        self.assertEqual(child.status, "blocked")
        self.assertTrue(child.run_request["dispatch_pending_resolution"])

    def test_rejected_repeated_key_returns_existing_run_without_post_or_refund(self):
        self.strategy = retired_catalog()
        self.lookup.return_value = ("dispatched", {"run_id": "previous-call", "status": "queued"})
        child = self.queue(refund=True)
        self.http.post.assert_not_called()
        self.refund.assert_not_called()
        self.assertEqual(child.run_id, "previous-call")
        self.assertEqual(child.result["diagnostics"]["editorial_policy_recheck"]["code"], "editorial_brief_invalid")

    def test_no_post_and_immediate_absent_lookup_still_preserves_grace_for_earlier_call(self):
        self.strategy = retired_catalog()
        self.lookup.return_value = ("absent", {})
        child = self.queue(refund=True)
        self.refund.assert_not_called()
        self.assertTrue(child.run_request["dispatch_pending_resolution"])

    def test_policy_rejected_dispatch_refund_waits_for_absent_lookup_after_grace(self):
        self.strategy = retired_catalog()
        self.lookup.return_value = ("absent", {})
        child = self.queue(refund=True)
        self.assertIsNone(self.ns["_resolve_dispatch_token_run"](child))
        self.refund_pending.assert_not_called()
        child.created_at -= timedelta(seconds=181)
        self.assertIs(self.ns["_resolve_dispatch_token_run"](child), child)
        self.refund_pending.assert_called_once_with(child)
        self.assertEqual(child.status, "failed")
        self.assertFalse(child.run_request["dispatch_pending_resolution"])
        self.http.post.assert_not_called()

    def test_policy_rejected_dispatch_can_bind_a_late_existing_run_without_new_post(self):
        self.strategy = retired_catalog()
        child = self.queue(refund=True)
        self.lookup.return_value = ("dispatched", {"run_id": "late-worker-run"})
        self.assertEqual(self.ns["_resolve_dispatch_token_run"](child), "bound-existing-run")
        self.bind_existing.assert_called_once_with(client_request_id="fixture-key", remote_run_id="late-worker-run")
        self.refund_pending.assert_not_called()
        self.http.post.assert_not_called()

    def test_lookup_recovers_an_existing_dispatch_without_a_second_post(self):
        self.http.post.side_effect = self.transport_error("lost response")
        self.lookup.return_value = ("dispatched", {"run_id": "existing-child", "status": "queued"})
        child = self.queue(refund=True)
        self.http.post.assert_called_once()
        self.assertEqual(child.run_id, "existing-child")
        self.refund.assert_not_called()

    def test_unchanged_policy_retry_reuses_the_same_key_and_brief(self):
        def lost_first(url, **kwargs):
            result = self.post(url, **kwargs)
            if len(self.posted) == 1:
                raise self.transport_error("lost response")
            return result
        self.http.post.side_effect = lost_first
        child = self.queue(refund=True)
        self.assertEqual(child.run_id, "child-1")
        self.assertEqual(len(self.posted), 2)
        self.assertEqual(self.posted[0], self.posted[1])
        self.refund.assert_not_called()

    def test_policy_revoked_after_lost_response_stops_retry_without_premature_refund(self):
        def lose_and_revoke(url, **kwargs):
            self.post(url, **kwargs)
            self.strategy = retired_catalog()
            raise self.transport_error("lost response")
        self.http.post.side_effect = lose_and_revoke
        child = self.queue(refund=True)
        self.assertEqual(len(self.posted), 1)
        self.refund.assert_not_called()
        self.assertTrue(child.run_request["dispatch_pending_resolution"])
        self.assertEqual(child.run_request["pending_billing_refund"]["charged_user_id"], "user-1")

    def test_unavailable_catalog_is_not_an_empty_legacy_catalog(self):
        self.config_model.objects.filter.return_value.only.return_value.first.side_effect = DatabaseError("must not leak database details")
        child = self.queue(refund=True)
        self.http.post.assert_not_called()
        self.assertEqual(child.result["diagnostics"]["content_factory_status_code"], 503)
        self.assertNotIn("must not leak", child.result["error"])

    def test_missing_configuration_is_not_recreated_or_treated_as_legacy(self):
        self.strategy = {}
        self.config_model.objects.filter.return_value.only.return_value.first.side_effect = lambda: None
        child = self.queue(payload={"domain": self.org.domain})
        self.assertEqual(child.result["diagnostics"]["content_factory_status_code"], 503)
        self.http.post.assert_not_called()
        self.config_model.objects.get_or_create.assert_not_called()

    def test_server_error_then_revocation_stops_retry_and_defers_refund(self):
        def server_error_and_revoke(url, **kwargs):
            self.post(url, **kwargs)
            self.strategy = retired_catalog()
            return SimpleNamespace(status_code=503)
        self.http.post.side_effect = server_error_and_revoke
        child = self.queue(refund=True)
        self.assertEqual(len(self.posted), 1)
        self.assertTrue(child.run_request["dispatch_pending_resolution"])
        self.refund.assert_not_called()

    def test_definitive_worker_rejection_retains_existing_refund_behavior(self):
        self.http.post.side_effect = lambda *args, **kwargs: SimpleNamespace(status_code=422, json=lambda: {"detail": "Rejected"}, text="Rejected")
        child = self.queue(refund=True)
        self.http.post.assert_called_once()
        self.refund.assert_called_once()
        self.assertFalse(child.run_request.get("dispatch_pending_resolution"))

    def test_non_article_dispatch_does_not_require_an_article_brief(self):
        self.strategy = retired_catalog()
        child = self.queue(payload={"domain": self.org.domain}, endpoint="discovery")
        self.assertEqual(child.run_id, "child-1")
        self.config_model.objects.filter.assert_not_called()
