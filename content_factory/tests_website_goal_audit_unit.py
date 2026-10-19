"""Actual original-goal writer/read chain with only persistence boundaries replaced."""

from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.request import Request
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from founder_tools.services import FounderCompanyContext
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun

from . import website_goal as goals, website_goal_audit as audit, website_goal_runner as runner
from .models import OrganizationContentConfig
from .website_models import WebsiteConnection, WebsiteConnectionOperation
from .website_operations import operation_summary
from .website_views import WebsiteConnectionOperationView, guarded_owner_operation


def rows(value=None):
    result = Mock()
    for method in ("filter", "order_by", "select_related", "select_for_update", "exclude"):
        getattr(result, method).return_value = result
    result.first.return_value = value
    result.get.return_value = value
    return result


@override_settings(ARTICLES_WEBSITE_DOCTOR_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
    ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"], ARTICLES_SETUP_CONSENT_ENABLED=False)
class GoalAuditTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.user = get_user_model()(pk=4, email="fixture@example.test", slack_id="fixture-actor", is_active=True)
        self.organization = Organization(pk=7, domain="fixture.test")
        self.profile = VibeRaisingProfile(pk=8, user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)
        self.company = VibeRaisingCompany(pk=9, profile=self.profile, organization=self.organization, domain="fixture.test")
        self.context = FounderCompanyContext(profile=self.profile, company=self.company, organization=self.organization)
        self.connection = WebsiteConnection(organization=self.organization, authorized_by=self.user,
            github_repo="fixture/site", repository_id=10, installation_id="11", generation=1, branch="main",
            configuration_version=1, verified_sha="a" * 40)
        self.config = OrganizationContentConfig(organization=self.organization, website_connection=self.connection,
            github_repo=self.connection.github_repo)
        self.op = WebsiteConnectionOperation(connection=self.connection, generation=1, action="prepare", state="pending",
            payload={"company_id": "9", "requested_by_user_id": "4"}, receipt={}, updated_at=self.now)
        self.op.receipt["goal"] = goals.new_goal(str(self.op.pk), goals.scope_for(self.connection, 9), now=self.now)
        audit.seed(self.op, from_start=True, now=self.now)
        self.saved = []
        self.enterContext(patch.object(WebsiteConnectionOperation, "save", lambda op, **kwargs: self.saved.append(kwargs)))
        self.enterContext(patch.object(transaction.Atomic, "__enter__", lambda atomic: None))
        self.enterContext(patch.object(transaction.Atomic, "__exit__", lambda atomic, *args: False))
        self.operations = rows(self.op)
        self.enterContext(patch.object(WebsiteConnectionOperation.objects, "select_for_update", return_value=self.operations))

    def request(self, *, key="fixture-request", data=None):
        request = Request(APIRequestFactory().post("/?company_id=9", data or {}, format="json", HTTP_IDEMPOTENCY_KEY=key), parsers=[JSONParser()])
        request.user = self.user
        return request

    def observe(self, *, action="approve", run_id=None, initial=False, request=None):
        audit.observe_owner_http(request or self.request(), self.context, self.config, action=action,
            run_id=run_id, initial_setup=initial)

    def test_new_goal_is_seeded_by_real_runner_and_never_claims_zero_human_actions(self):
        self.op.receipt = {"status": "working"}
        with patch.object(WebsiteConnection, "operations", rows()), patch.object(
            WebsiteConnectionOperation.objects, "get_or_create", return_value=(self.op, True)):
            result = runner.start_goal(self.connection, company_id=9, user=self.user, retry=False, target="ready", now=self.now)
        self.assertIs(result, self.op)
        public = operation_summary(result)["receipt"]["goal_audit"]
        self.assertFalse(public["complete"])
        self.assertIsNone(public["human_actions"])
        self.assertEqual(public["goal_id"], str(self.op.pk))
        self.assertEqual(public["observed_owner_actions"], 0)
        self.assertIn("external_github_merge", public["coverage_gaps"])
        self.assertNotIn("before_audit_started", public["coverage_gaps"])

    def test_initial_setup_is_excluded_once_and_header_replays_are_deduplicated(self):
        self.observe(action="connection:prepare", initial=True)
        self.observe(action="connection:prepare", initial=True)
        self.assertEqual(audit.public_audit(self.op)["observed_owner_actions"], 0)
        self.observe(action="connection:prepare", initial=True, request=self.request(key="retry"))
        self.assertEqual(audit.public_audit(self.op)["observed_owner_actions"], 1)
        self.assertTrue(all(item["update_fields"] == ["receipt"] for item in self.saved))

    def test_changed_website_intent_under_one_request_key_is_another_observed_action(self):
        first = self.request(data={"branch": "main", "setup_consent": {"publish_mode": "review"}})
        changed = self.request(data={"branch": "main", "setup_consent": {"publish_mode": "auto"}})
        self.observe(action="connection:prepare", initial=True, request=first)
        self.observe(action="connection:prepare", initial=True, request=first)
        self.observe(action="connection:prepare", initial=True, request=changed)
        self.assertEqual(audit.public_audit(self.op)["observed_owner_actions"], 1)
        self.assertNotIn("publish_mode", str(self.op.receipt["goal_audit"]))

    def test_oversized_or_nonfinite_owner_intent_stays_unobserved_without_private_payload(self):
        for data in ({"private": "x" * (audit.MAX_INTENT_BYTES + 1)}, {"private": float("inf")}):
            request = self.request(data={})
            request._full_data = data
            self.observe(request=request)
        self.assertEqual(audit.public_audit(self.op)["observed_owner_actions"], 0)

    def test_original_first_article_link_is_used_instead_of_a_newer_goal(self):
        run = ContentFactoryRun(run_id="article", organization=self.organization, domain=self.organization.domain,
            run_request={"first_article_goal_operation_id": str(self.op.pk)})
        with patch.object(ContentFactoryRun.objects, "filter", return_value=rows(run)):
            self.observe(run_id=run.run_id)
        self.assertIn({"pk": str(self.op.pk)}, [call.kwargs for call in self.operations.filter.call_args_list])
        self.assertEqual(audit.public_audit(self.op)["observed_owner_actions"], 1)

    def test_existing_body_idempotency_key_deduplicates_replays_without_a_header(self):
        for _ in range(2):
            self.observe(request=self.request(key="", data={"client_request_id": "body-request"}))
        self.assertEqual(audit.public_audit(self.op)["observed_owner_actions"], 1)

    def test_setup_child_is_attributed_by_saved_parent_receipt(self):
        run = ContentFactoryRun(run_id="setup", organization=self.organization, domain=self.organization.domain, run_request={})
        self.op.receipt["setup_run_id"] = run.run_id
        with patch.object(ContentFactoryRun.objects, "filter", return_value=rows(run)):
            self.observe(run_id=run.run_id)
        self.assertEqual(audit.public_audit(self.op)["observed_owner_actions"], 1)
        self.assertTrue(any(call.args for call in self.operations.filter.call_args_list))

    def test_other_company_run_and_moved_connection_scope_cannot_write_this_audit(self):
        foreign = ContentFactoryRun(run_id="foreign", organization_id=99, domain="foreign.test", run_request={})
        with patch.object(ContentFactoryRun.objects, "filter", return_value=rows(foreign)):
            self.observe(run_id=foreign.run_id)
        self.assertFalse(self.saved)
        self.connection.generation += 1
        self.observe()
        self.assertFalse(self.saved)

    def test_internal_goal_post_shape_and_browser_body_cannot_claim_a_human_actor(self):
        internal = SimpleNamespace(user=self.user, data={}, method="POST", META={}, headers={"Idempotency-Key": "internal"},
            _website_prepare_dispatch=True)
        self.observe(request=internal)
        self.assertFalse(self.saved)
        request = self.request(data={"human_actions": 0, "complete": True, "_website_prepare_dispatch": True})
        self.observe(request=request)
        self.assertEqual(audit.public_audit(self.op)["observed_owner_actions"], 1)
        self.assertIsNone(audit.public_audit(self.op)["human_actions"])

    def test_missing_and_forged_legacy_telemetry_remain_unknown(self):
        for value in (None, {"complete": True, "human_actions": 0}):
            self.op.receipt["goal_audit"] = value
            public = operation_summary(self.op)["receipt"]["goal_audit"]
            self.assertFalse(public["complete"])
            self.assertIsNone(public["observed_owner_actions"])
            self.assertIn("before_audit_started", public["coverage_gaps"])
        self.observe()
        self.assertFalse(self.op.receipt["goal_audit"]["from_start"])
        self.assertEqual(audit.public_audit(self.op)["observed_owner_actions"], 1)

    def test_journal_is_bounded_and_overflow_keeps_coverage_unknown(self):
        self.op.receipt["goal_audit"]["event_ids"] = [goals.digest(index) for index in range(audit.MAX_EVENTS)]
        self.observe()
        public = audit.public_audit(self.op)
        self.assertEqual(public["observed_owner_actions"], audit.MAX_EVENTS)
        self.assertIn("observation_capacity", public["coverage_gaps"])
        self.assertFalse(public["complete"])

    def test_public_projection_never_exposes_raw_journal_actor_or_request_identity(self):
        self.observe(request=self.request(key="synthetic-private-request-canary"))
        public = operation_summary(self.op)["receipt"]["goal_audit"]
        self.assertNotIn("event_ids", public)
        self.assertNotIn("synthetic-private", str(public))
        self.assertEqual(public["basis"], "original_goal_audit")

    def test_default_off_adds_no_query_write_or_public_contract(self):
        with override_settings(ARTICLES_WEBSITE_DOCTOR_ENABLED=False), patch.object(
            WebsiteConnectionOperation.objects, "select_for_update") as query:
            self.observe()
            result = operation_summary(self.op)
        query.assert_not_called()
        self.assertFalse(self.saved)
        self.assertNotIn("goal_audit", result["receipt"])

    def test_missing_goal_enrollment_adds_no_query_write_or_public_contract(self):
        with override_settings(ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=[]), patch.object(
            WebsiteConnectionOperation.objects, "select_for_update") as query:
            self.observe()
            result = operation_summary(self.op)
        query.assert_not_called()
        self.assertFalse(self.saved)
        self.assertNotIn("goal_audit", result["receipt"])

    def test_completed_goal_does_not_acquire_later_owner_intents(self):
        self.op.state = "completed"
        self.observe()
        self.assertFalse(self.saved)
        self.assertEqual(audit.public_audit(self.op)["observed_owner_actions"], 0)

    def test_optional_observation_failure_does_not_change_the_action_result(self):
        with patch.object(WebsiteConnectionOperation.objects, "select_for_update", side_effect=RuntimeError("synthetic-private-canary")), self.assertLogs(
            audit.logger, level="WARNING") as logs:
            self.observe()
        self.assertEqual(logs.output, ["WARNING:content_factory.website_goal_audit:articles_goal_audit_observation_unavailable"])
        self.assertFalse(audit.public_audit(self.op)["complete"])

    def test_owner_intent_survives_an_in_flight_prepare_snapshot_without_touching_its_lease(self):
        from . import website_prepare as prepare
        claimed = deepcopy(self.op)
        claimed.state, claimed.attempts = "running", 3
        claimed._claimed_at = self.now
        claimed._claimed_goal_revision = claimed.receipt["goal"]["revision"]
        self.observe()
        persistence = rows()
        persistence.update.return_value = 1
        with patch.object(WebsiteConnectionOperation.objects, "filter", return_value=persistence):
            self.assertIs(prepare._persist_prepare_state(claimed, now=timezone.now()), claimed)
        saved = persistence.update.call_args.kwargs["receipt"]["goal_audit"]
        self.assertEqual(len(saved["event_ids"]), 1)
        self.assertEqual(self.op.updated_at, self.now)
        self.assertFalse(audit.public_audit(claimed)["complete"])

    def test_real_guarded_http_context_writer_and_public_reader_use_the_same_goal(self):
        """Keep founder selection, wrapper and projection real; replace only ORM/transactions."""
        companies = rows(self.company)
        context_boundaries = ExitStack()
        self.addCleanup(context_boundaries.close)
        context_boundaries.enter_context(patch.object(VibeRaisingProfile.objects, "get_or_create", return_value=(self.profile, False)))
        context_boundaries.enter_context(patch.object(VibeRaisingProfile, "companies", companies))
        context_boundaries.enter_context(patch.object(OrganizationContentConfig.objects, "get_or_create", return_value=(self.config, False)))
        context_boundaries.enter_context(patch.object(WebsiteConnectionOperation.objects, "filter", return_value=rows(self.op)))
        run = ContentFactoryRun(run_id="article", organization=self.organization, domain=self.organization.domain,
            run_request={"first_article_goal_operation_id": str(self.op.pk)})
        context_boundaries.enter_context(patch.object(ContentFactoryRun.objects, "filter", return_value=rows(run)))
        class OwnerControl:
            @guarded_owner_operation("setup", run_operation=True)
            def post(view, request, run_id, action):
                return Response({"accepted": True}, status=202)
        # cancel is independently scoped inside the real owner control; the
        # observer still validates its run and original goal before storing.
        response = OwnerControl().post(self.request(), run_id=run.run_id, action="cancel")
        self.assertEqual(response.status_code, 202)
        public = WebsiteConnectionOperationView().get(self.request(key="read"), self.op.pk)
        self.assertEqual(public.status_code, 200)
        self.assertEqual(public.data["operation"]["receipt"]["goal_audit"]["observed_owner_actions"], 1)
        self.assertIsNone(public.data["operation"]["receipt"]["goal_audit"]["human_actions"])
