"""CMS provider work retains the exact original payer and current private draft."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from roo.models import Ledger
from . import cms_article_source as sources
from . import cms_artifact_review as reviews
from . import tests_cms_artifact_review_unit as fixtures
from .tests_cms_connection_contract_unit import TEST_KEY
from .website_contract import WebsiteAuthorityError


@override_settings(ARTICLES_CHANNEL_DISCOVERY_ENABLED=True, ARTICLES_PUBLISHING_CHANNELS_ENABLED=True,
    ARTICLES_CMS_CHANNEL_ENABLED=True, ARTICLES_CHANNEL_ARTIFACTS_ENABLED=True,
    CONNECTOR_CREDENTIAL_KEYS=fixtures.json.dumps({"test": TEST_KEY}), CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="test")
class CmsArticleSourceTests(SimpleTestCase):
    reserve = fixtures.CmsArtifactReviewTests.reserve

    def setUp(self):
        fixtures.CmsArtifactReviewTests.setUp(self)
        self.run.run_request.update(roo_points_authorized=True, roo_points_action="article_generation", roo_points_cost=6,
            roo_points_billing_status="charged", roo_points_ledger_id="31")
        self.outbox.payload["request"] = deepcopy(self.run.run_request)
        self.operation = reviews.start_review(self.run)
        reviews.advance_review(self.operation.pk, now=self.now)
        self.ledger = SimpleNamespace(pk=31, user_id=4, delta_microroo=-6_000_000,
            idempotency_key="content_factory:charge:" + self.run.run_request["client_request_id"])
        self.spend_patch = patch("content_factory.billing_lineage.original_article_spend_for_run", return_value=self.ledger)
        self.spend = self.spend_patch.start(); self.addCleanup(self.spend_patch.stop)
        self.refund_patch = patch.object(Ledger.objects, "filter")
        self.refund = self.refund_patch.start(); self.addCleanup(self.refund_patch.stop)
        self.refund.return_value.exists.return_value = False

    def test_exact_collected_article_retains_original_payer_spend_and_canonical_handoff(self):
        value = sources.current_article(self.operation)
        self.assertEqual(value.handoff, self.value)
        self.assertEqual(value.review_operation_id, str(self.operation.pk))
        self.assertEqual(value.billing["spend_ledger_id"], "31")
        self.assertEqual(value.billing["payer_user_id"], "4")
        self.assertEqual(value.billing["cost_points"], 6)
        self.assertNotIn("synthetic-application-password", repr(value))
        self.refund.assert_called_once_with(idempotency_key="content_factory:refund:" + self.run.run_request["client_request_id"])

    def test_committed_refund_denies_a_spend_before_the_run_refund_stamp_arrives(self):
        self.refund.return_value.exists.return_value = True
        with self.assertRaises(WebsiteAuthorityError): sources.current_article(self.operation)

    def test_missing_original_spend_wrong_payer_amount_or_ledger_cannot_be_requoted_as_payment(self):
        for changed in (None, SimpleNamespace(**{**self.ledger.__dict__, "user_id": 99}),
                SimpleNamespace(**{**self.ledger.__dict__, "delta_microroo": -5_000_000}),
                SimpleNamespace(**{**self.ledger.__dict__, "pk": 32})):
            self.spend.return_value = changed
            with self.subTest(ledger=changed), self.assertRaises(WebsiteAuthorityError): sources.current_article(self.operation)

    def test_only_an_original_explicit_free_outbox_decision_can_skip_a_spend(self):
        self.run.run_request.update(roo_points_billing_status="free", roo_points_cost=0)
        self.run.run_request.pop("roo_points_ledger_id")
        self.outbox.payload["request"] = deepcopy(self.run.run_request)
        result = sources.current_article(self.operation)
        self.assertEqual(result.billing["basis"], "free")
        self.assertIsNone(result.billing["spend_ledger_id"])
        self.spend.assert_not_called()
        self.run.organization_id = 99
        with self.assertRaises(WebsiteAuthorityError): sources.original_billing(self.run)
        self.run.organization_id = 7
        # A caller's free flag cannot replace the original charged outbox.
        self.outbox.payload["request"].update(roo_points_cost=6, roo_points_billing_status="charged", roo_points_ledger_id="31")
        with self.assertRaises(WebsiteAuthorityError): sources.current_article(self.operation)

    def test_pending_refund_gated_boolean_or_unstamped_payments_never_allow_provider_work(self):
        for changes in ({"roo_points_authorized": 1}, {"roo_points_action": "setup"}, {"roo_points_cost": True},
                {"roo_points_billing_status": "gated"}, {"roo_points_billing_status": "refunded"},
                {"pending_billing_refund": True}, {"roo_points_refund_ledger_id": "32"}):
            original = deepcopy(self.run.run_request)
            self.run.run_request.update(changes)
            self.outbox.payload["request"] = deepcopy(self.run.run_request)
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): sources.current_article(self.operation)
            self.run.run_request = original
        self.outbox.payload["request"] = deepcopy(self.run.run_request)
        self.run.result["reconciliation_refund_pending"] = True
        with self.assertRaises(WebsiteAuthorityError): sources.current_article(self.operation)

    def test_changed_collected_source_scope_or_receipt_never_borrows_git_or_worker_approval(self):
        original = deepcopy(self.operation.payload)
        self.operation.payload["handoff"]["approved"] = True
        with self.assertRaises(WebsiteAuthorityError): sources.current_article(self.operation)
        self.operation.payload = original
        self.operation.receipt["approval_recorded"] = True
        with self.assertRaises(WebsiteAuthorityError): sources.current_article(self.operation)
        self.operation.receipt["approval_recorded"] = False
        self.run.result["generation"] = 3
        with self.assertRaises(WebsiteAuthorityError): sources.current_article(self.operation)
