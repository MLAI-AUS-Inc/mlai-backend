"""Actual authenticated dispatch, admission and MCP transport observation."""
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.db import transaction
from django.test import SimpleTestCase, override_settings
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView

from . import website_owner_transport as transport, website_goal_audit as audit, website_goal_runner as runner
from . import website_views, tests_website_goal_audit_unit as fixtures
from .website_models import WebsiteConnection, WebsiteConnectionOperation
from organizations.models import Organization


@override_settings(ARTICLES_WEBSITE_DOCTOR_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
    ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"], ARTICLES_SETUP_CONSENT_ENABLED=False)
class OwnerTransportTests(SimpleTestCase):
    def setUp(self):
        self.fixture = fixtures.GoalAuditTests("runTest")
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.op = self.fixture.op
        self.factory = APIRequestFactory()
        self.enterContext(patch.object(website_views, "_context", return_value=(self.fixture.context, self.fixture.config, None)))
        self.enterContext(patch.object(Organization.objects, "select_for_update", return_value=fixtures.rows(self.fixture.organization)))
        self.enterContext(patch.object(WebsiteConnection.objects, "select_for_update", return_value=fixtures.rows(self.fixture.connection)))

    def request(self, cls, *, data=None, method="post", **kwargs):
        request = getattr(self.factory, method)("/synthetic-owned-action", data or {}, format="json", HTTP_IDEMPOTENCY_KEY="same-key")
        force_authenticate(request, user=self.fixture.user)
        return cls.as_view()(request, **kwargs)

    def test_authenticated_intent_is_committed_before_mutation_and_survives_exception(self):
        fixture = self
        order = []
        real = audit.observe_transport
        def observed(*args):
            order.append("intent")
            return real(*args)
        class Mutation(transport.ObservedOwnerAPIView):
            permission_classes = [IsAuthenticated]
            def post(self, request):
                order.append("mutation")
                fixture.assertEqual(audit.public_audit(fixture.op)["observed_owner_actions"], 1)
                raise ValueError("synthetic rollback")
        with patch.object(audit, "observe_transport", side_effect=observed), self.assertRaisesMessage(ValueError, "synthetic rollback"):
            self.request(Mutation, data={"choice": "review"})
        self.assertEqual(order, ["intent", "mutation"])
        self.assertEqual(audit.public_audit(self.op)["observed_owner_actions"], 1)
        self.assertIsNone(transport.CURRENT.get())

    def test_rejected_mutation_counts_once_and_existing_handler_hook_does_not_double_count(self):
        fixture = self
        class Mutation(website_views.WebsiteConnectionActionView):
            permission_classes = [IsAuthenticated]
            def post(self, request):
                audit.observe_owner_http(request, fixture.fixture.context, fixture.fixture.config, action="legacy")
                return Response({"code":"synthetic_refusal"}, status=409)
        first = self.request(Mutation, data={"choice": "review"})
        self.assertEqual(first.status_code, 409)
        self.request(Mutation, data={"choice": "review"})
        self.assertEqual(audit.public_audit(self.op)["observed_owner_actions"], 1)
        self.request(Mutation, data={"choice": "auto"})
        self.assertEqual(audit.public_audit(self.op)["observed_owner_actions"], 2)

    @override_settings(ARTICLES_OWNER_AUDIT_REQUIRED_ENABLED=True, ARTICLES_OWNER_AUDIT_REQUIRED_ORGANIZATIONS=["7"])
    def test_initial_setup_identity_is_in_admission_before_dispatch_response_can_be_lost(self):
        fixture = self
        self.op.receipt = {}
        class Admission(website_views.WebsiteConnectionActionView):
            permission_classes = [IsAuthenticated]
            def post(self, request, **kwargs):
                runner.start_goal(fixture.fixture.connection, company_id=9, user=fixture.fixture.user,
                    retry=False, target="ready", now=fixture.fixture.now)
                raise ValueError("lost setup response")
        with patch.object(WebsiteConnection, "operations", fixtures.rows()), patch.object(
                WebsiteConnectionOperation.objects, "get_or_create", return_value=(self.op, True)), \
                self.assertRaisesMessage(ValueError, "lost setup response"):
            self.request(Admission, data={"goal_target":"ready"}, action="prepare")
        value = audit.public_audit(self.op)
        self.assertEqual(value["observed_owner_actions"], 0)
        self.assertIsNotNone(self.op.receipt["goal_audit"]["initial_setup_event"])
        self.assertEqual(audit.transport_coverage(self.op)["coverage"], "complete")
        self.assertNotIn("uncovered_owner_endpoints", value["coverage_gaps"])
        self.assertIn("external_github_push", value["coverage_gaps"])
        self.assertFalse(value["complete"])
        self.assertIsNone(value["human_actions"])

    @override_settings(ARTICLES_OWNER_AUDIT_REQUIRED_ENABLED=True, ARTICLES_OWNER_AUDIT_REQUIRED_ORGANIZATIONS=["7"])
    def test_existing_post_admission_hook_cannot_count_the_initial_press_twice(self):
        fixture = self
        self.op.receipt = {}
        class Admission(website_views.WebsiteConnectionActionView):
            permission_classes = [IsAuthenticated]
            def post(self, request, **kwargs):
                runner.start_goal(fixture.fixture.connection, company_id=9, user=fixture.fixture.user,
                    retry=False, target="ready", now=fixture.fixture.now)
                audit.observe_owner_http(request, fixture.fixture.context, fixture.fixture.config,
                    action="connection:prepare", initial_setup=True, operation=fixture.op)
                return Response({})
        with patch.object(WebsiteConnection, "operations", fixtures.rows()), patch.object(
                WebsiteConnectionOperation.objects, "get_or_create", return_value=(self.op, True)):
            self.request(Admission, data={"goal_target":"ready"}, action="prepare")
        self.assertEqual(audit.public_audit(self.op)["observed_owner_actions"], 0)

    def test_loaded_http_and_mcp_inventory_refuses_a_new_unobserved_mutation_class(self):
        self.assertEqual(transport.inventory()["coverage"], "complete")
        class Unobserved(APIView):
            def post(self, request):
                return Response({})
        Unobserved.__module__ = website_views.__name__
        with patch.object(website_views, "Unobserved", Unobserved, create=True):
            self.assertIn("owner_http_class_uncovered", transport.inventory()["gaps"])

    def test_default_off_and_reads_do_not_add_context_queries_or_observations(self):
        class Mutation(transport.ObservedOwnerAPIView):
            permission_classes = [IsAuthenticated]
            def get(self, request): return Response({})
            def post(self, request): return Response({})
        with patch.object(website_views, "_context") as context:
            self.request(Mutation, method="get")
            with override_settings(ARTICLES_WEBSITE_DOCTOR_ENABLED=False):
                self.request(Mutation)
        context.assert_not_called()
        self.assertEqual(audit.public_audit(self.op)["observed_owner_actions"], 0)

    def test_mcp_context_is_server_owned_precedes_service_and_preserves_service_exception(self):
        from . import mcp_operations
        from .models import OrganizationContentConfig
        arguments = {"idempotencyKey":"same-mcp-key", "topic":"new topic"}
        with patch.object(mcp_operations, "context_for", return_value=self.fixture.context), \
                patch.object(OrganizationContentConfig.objects, "filter", return_value=fixtures.rows(self.fixture.config)):
            with self.assertRaisesMessage(ValueError, "synthetic service refusal"):
                with transport.mcp_intent(self.fixture.user, self.fixture.company, name="generate_article", arguments=arguments):
                    self.assertEqual(transport.CURRENT.get()["origin"], "mcp")
                    self.assertEqual(audit.public_audit(self.op)["observed_owner_actions"], 1)
                    raise ValueError("synthetic service refusal")
        self.assertIsNone(transport.CURRENT.get())
        self.assertEqual(audit.public_audit(self.op)["observed_owner_actions"], 1)

    def test_mcp_disabled_context_never_swallows_a_business_exception(self):
        with override_settings(ARTICLES_WEBSITE_DOCTOR_ENABLED=False), self.assertRaisesMessage(ValueError, "unchanged"):
            with transport.mcp_intent(self.fixture.user, self.fixture.company, name="generate_article", arguments={}):
                raise ValueError("unchanged")

    @override_settings(ARTICLES_OWNER_AUDIT_REQUIRED_ENABLED=True, ARTICLES_OWNER_AUDIT_REQUIRED_ORGANIZATIONS=["7"])
    def test_required_http_scope_refuses_failed_observation_before_domain_effect(self):
        called = []
        class Mutation(transport.ObservedOwnerAPIView):
            permission_classes = [IsAuthenticated]
            def post(self, request):
                called.append(True)
                return Response({})
        with patch.object(audit, "observe_transport", side_effect=RuntimeError("private storage error")):
            response = self.request(Mutation)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(called, [])
        self.assertNotIn("private storage error", str(response.data))

    @override_settings(ARTICLES_OWNER_AUDIT_REQUIRED_ENABLED=True, ARTICLES_OWNER_AUDIT_REQUIRED_ORGANIZATIONS=["7"])
    def test_required_mcp_scope_refuses_failed_observation_before_domain_effect(self):
        from . import mcp_operations
        from .models import OrganizationContentConfig
        called = []
        with patch.object(mcp_operations, "context_for", return_value=self.fixture.context), \
                patch.object(OrganizationContentConfig.objects, "filter", return_value=fixtures.rows(self.fixture.config)), \
                patch.object(audit, "observe_transport", return_value=False), self.assertRaises(transport.OwnerAuditRequired):
            with transport.mcp_intent(self.fixture.user, self.fixture.company,
                    name="generate_article", arguments={"idempotencyKey":"key"}):
                called.append(True)
        self.assertEqual(called, [])

    def test_best_effort_admission_cannot_claim_complete_internal_coverage(self):
        self.op.receipt = {}
        observed = transport.descriptor(self.fixture.user, self.fixture.context, self.fixture.config,
            action="prepare", data={}, replay="initial", origin="http")
        token = transport.CURRENT.set(observed)
        try:
            self.op.receipt["goal"] = transport.goals.new_goal(str(self.op.pk),
                transport.goals.scope_for(self.fixture.connection, 9), now=self.fixture.now)
            audit.seed(self.op, from_start=True, now=self.fixture.now)
        finally:
            transport.CURRENT.reset(token)
        self.assertEqual(audit.transport_coverage(self.op)["coverage"], "partial")
        self.assertEqual(audit.transport_coverage(self.op)["policy"], "best_effort")

    @override_settings(ARTICLES_OWNER_AUDIT_REQUIRED_ENABLED=True, ARTICLES_OWNER_AUDIT_REQUIRED_ORGANIZATIONS=["7"])
    def test_ordinary_repeat_after_completion_is_allowed_outside_the_original_window(self):
        fixture = self
        self.op.state = "completed"
        before = deepcopy(self.op.receipt)
        class Repeat(transport.ObservedOwnerAPIView):
            permission_classes = [IsAuthenticated]
            def post(self, request):
                audit.observe_owner_http(request, fixture.fixture.context, fixture.fixture.config, action="generate")
                return Response({"ordinary_repeat": True})
        response = self.request(Repeat, data={"topic": "second article"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.op.receipt, before)

    @override_settings(ARTICLES_OWNER_AUDIT_REQUIRED_ENABLED=True, ARTICLES_OWNER_AUDIT_REQUIRED_ORGANIZATIONS=["7"])
    def test_ordinary_repeat_run_control_does_not_need_a_completed_first_goal_lineage(self):
        from workflow_runs.models import ContentFactoryRun
        from . import website_review_action_audit as review_audit, vibe_marketing_views as views
        self.op.state = "completed"
        run = SimpleNamespace(run_id="ordinary-repeat", run_request={})
        before = deepcopy(self.op.receipt)
        class Repeat(views.VibeMarketingRunControlView):
            permission_classes = [IsAuthenticated]
            def post(self, request, **kwargs): return Response({"ordinary_repeat": True})
        with patch.object(ContentFactoryRun.objects, "filter", return_value=fixtures.rows(run)), \
                patch.object(views, "_run_belongs_to_context", return_value=True), \
                patch.object(review_audit, "original_goal", return_value=(None, "lineage")):
            response = self.request(Repeat, data={}, run_id=run.run_id, action="approve")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.op.receipt, before)

    def test_actual_grant_checked_mcp_write_dispatch_records_before_its_domain_service(self):
        from uuid import uuid4
        from startup_updates.mcp import articles
        from . import mcp_operations, website_goal as goals
        from .models import OrganizationContentConfig
        self.fixture.company.pk = uuid4()
        self.op.payload["company_id"] = str(self.fixture.company.pk)
        self.op.receipt["goal"] = goals.new_goal(str(self.op.pk),
            goals.scope_for(self.fixture.connection, self.fixture.company.pk), now=self.fixture.now)
        principal = SimpleNamespace(user=self.fixture.user, grant={"id":"synthetic-grant"})
        arguments = {"companyId":str(self.fixture.company.pk), "topic":"new topic", "expectedCostPoints":6,
            "idempotencyKey":str(uuid4()), "websiteConnection":{"website_connection_id":str(self.fixture.connection.pk),
                "connection_generation":1, "connection_target_id":"native", "repository_id":10}}
        def started(*args, **kwargs):
            self.assertEqual(audit.public_audit(self.op)["observed_owner_actions"], 1)
            self.assertEqual(transport.CURRENT.get()["origin"], "mcp")
            return {"runId":"accepted"}
        with patch.object(articles, "valid_grant", return_value=principal), \
                patch.object(articles, "company_for", return_value=self.fixture.company), \
                patch.object(OrganizationContentConfig.objects, "filter", return_value=fixtures.rows(self.fixture.config)), \
                patch.object(mcp_operations, "start", side_effect=started):
            self.assertEqual(articles.call_tool(principal, "generate_article", arguments), {"runId":"accepted"})
        self.assertIsNone(transport.CURRENT.get())

    @override_settings(ARTICLES_OWNER_AUDIT_REQUIRED_ENABLED=True, ARTICLES_OWNER_AUDIT_REQUIRED_ORGANIZATIONS=["7"])
    def test_malformed_initial_transport_and_capacity_never_claim_complete_coverage(self):
        self.op.receipt = {}
        observed = transport.descriptor(self.fixture.user, self.fixture.context, self.fixture.config,
            action="prepare", data={}, replay="initial", origin="http")
        token = transport.CURRENT.set(observed)
        try:
            self.op.receipt["goal"] = transport.goals.new_goal(str(self.op.pk),
                transport.goals.scope_for(self.fixture.connection, 9), now=self.fixture.now)
            audit.seed(self.op, from_start=True, now=self.fixture.now)
        finally:
            transport.CURRENT.reset(token)
        saved = deepcopy(self.op.receipt)
        for field, invalid in (("initial_event", None), ("manifest_digest", []), ("gaps", {}),
                ("required", 1), ("from_start", False)):
            with self.subTest(field=field):
                self.op.receipt = deepcopy(saved)
                self.op.receipt["goal_audit"]["transport"][field] = invalid
                self.assertEqual(audit.transport_coverage(self.op)["coverage"], "partial")
        self.op.receipt = deepcopy(saved)
        self.op.receipt["goal_audit"]["from_start"] = False
        self.assertEqual(audit.transport_coverage(self.op)["coverage"], "partial")
        self.op.receipt = deepcopy(saved)
        self.op.receipt["goal_audit"]["overflow"] = True
        self.assertEqual(audit.transport_coverage(self.op)["gaps"], ["owner_transport_capacity"])
        self.op.receipt = deepcopy(saved)
        with override_settings(ARTICLES_OWNER_AUDIT_REQUIRED_ENABLED=False):
            self.assertEqual(audit.transport_coverage(self.op)["gaps"], ["owner_transport_policy_not_enforced"])
