"""Actual audit persistence and public API; disposable PostgreSQL CI only."""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.request import Request
from rest_framework.parsers import JSONParser
from rest_framework.test import APIRequestFactory, force_authenticate

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from founder_tools.services import get_founder_company_context

from . import website_goal_audit as audit, website_prepare as prepare
from .tests_website_connections import WebsiteDatabaseFixture
from .website_views import WebsiteConnectionOperationView


def _create_audit_scope(self):
    self.user = get_user_model().objects.create_user(email="audit-fixture@example.test", slack_id="audit-fixture")
    profile = VibeRaisingProfile.objects.create(user=self.user, role="founder")
    self.company = VibeRaisingCompany.objects.create(profile=profile, organization=self.org,
        name="Audit fixture", domain=self.org.domain)
    self.website.authorized_by = self.user
    self.website.save(update_fields=["authorized_by"])
    self.context = get_founder_company_context(self.user, company_id=self.company.pk)
    rollout = override_settings(ARTICLES_WEBSITE_DOCTOR_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
        ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=[str(self.org.pk)], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
        ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=[str(self.org.pk)], ARTICLES_SETUP_CONSENT_ENABLED=False)
    rollout.enable()
    self.addCleanup(rollout.disable)
    self.op = prepare.start_prepare(self.config, company_id=self.company.pk, user=self.user)


class GoalAuditPersistenceTests(WebsiteDatabaseFixture, TestCase):
    def setUp(self):
        super().setUp()
        _create_audit_scope(self)

    def request(self, key="audit-request"):
        request = Request(APIRequestFactory().post("/", {}, format="json", HTTP_IDEMPOTENCY_KEY=key), parsers=[JSONParser()])
        request.user = self.user
        return request

    def public(self):
        request = APIRequestFactory().get("/", {"company_id": self.company.pk})
        force_authenticate(request, user=self.user)
        response = WebsiteConnectionOperationView.as_view()(request, operation_id=self.op.pk)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["operation"]["receipt"]["goal_audit"]

    def test_real_goal_audit_writer_and_authenticated_original_operation_read(self):
        self.assertFalse(self.public()["complete"])
        self.assertIsNone(self.public()["human_actions"])
        for _ in range(2):
            audit.observe_owner_http(self.request(), self.context, self.config, action="approve")
        result = self.public()
        self.assertEqual(result["observed_owner_actions"], 1)
        self.assertNotIn("event_ids", result)
        self.assertIn("external_github_review", result["coverage_gaps"])

    def test_owner_observation_survives_real_sql_prepare_compare_and_set(self):
        claimed = prepare._claim_prepare(self.op.pk, timezone.now())
        self.assertIsNotNone(claimed)
        audit.observe_owner_http(self.request(), self.context, self.config, action="approve")
        self.op.refresh_from_db()
        self.assertEqual(self.op.updated_at, claimed._claimed_at)
        self.assertIs(prepare._persist_prepare_state(claimed, now=timezone.now()), claimed)
        self.assertEqual(self.public()["observed_owner_actions"], 1)

    def test_observation_rolls_back_with_its_transaction(self):
        with self.assertRaisesRegex(RuntimeError, "synthetic rollback"):
            with transaction.atomic():
                audit.observe_owner_http(self.request(), self.context, self.config, action="approve")
                raise RuntimeError("synthetic rollback")
        self.assertEqual(self.public()["observed_owner_actions"], 0)
        self.assertIsNone(self.public()["human_actions"])


class OwnerTransportPersistenceTests(WebsiteDatabaseFixture, TransactionTestCase):
    """The pre-dispatch journal is outside the later mutation's rollback."""
    def setUp(self):
        super().setUp()
        _create_audit_scope(self)

    def test_authenticated_intent_survives_real_domain_transaction_rollback(self):
        from rest_framework.permissions import IsAuthenticated
        from .website_owner_transport import ObservedOwnerAPIView
        from .website_models import WebsiteConnectionOperation
        fixture = self
        class Mutation(ObservedOwnerAPIView):
            permission_classes = [IsAuthenticated]
            def post(self, request):
                with transaction.atomic():
                    WebsiteConnectionOperation.objects.filter(pk=fixture.op.pk).update(receipt={"rolled_back":True})
                    raise RuntimeError("synthetic mutation rollback")
        request = APIRequestFactory().post("/", {"company_id":str(self.company.pk)}, format="json")
        force_authenticate(request, user=self.user)
        with self.assertRaisesRegex(RuntimeError, "synthetic mutation rollback"):
            Mutation.as_view()(request)
        self.op.refresh_from_db()
        self.assertNotIn("rolled_back", self.op.receipt)
        self.assertEqual(audit.public_audit(self.op)["observed_owner_actions"], 1)

    def test_strict_transport_stores_initial_event_with_goal_before_lost_response(self):
        from .website_views import WebsiteConnectionActionView
        from .website_models import WebsiteConnectionOperation
        fixture = self
        self.op.delete()
        class Admission(WebsiteConnectionActionView):
            def post(self, request, **kwargs):
                fixture.op = prepare.start_prepare(fixture.config, company_id=fixture.company.pk, user=fixture.user)
                raise RuntimeError("synthetic lost setup response")
        request = APIRequestFactory().post("/", {"company_id":str(self.company.pk)}, format="json")
        force_authenticate(request, user=self.user)
        with override_settings(ARTICLES_OWNER_AUDIT_REQUIRED_ENABLED=True,
                ARTICLES_OWNER_AUDIT_REQUIRED_ORGANIZATIONS=[str(self.org.pk)]), \
                self.assertRaisesRegex(RuntimeError, "synthetic lost setup response"):
            Admission.as_view()(request, action="prepare")
        self.op = WebsiteConnectionOperation.objects.get(pk=self.op.pk)
        self.assertEqual(audit.transport_coverage(self.op)["coverage"], "complete")
        self.assertEqual(audit.public_audit(self.op)["observed_owner_actions"], 0)
