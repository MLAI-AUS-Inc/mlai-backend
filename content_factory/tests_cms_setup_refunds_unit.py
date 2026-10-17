"""Unused setup credits survive cancellation, disabled rollouts and privacy erasure."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from . import cms_setup_refunds as refunds
from . import cms_setup_authority as authority
from . import cms_setup_billing as billing
from . import tests_cms_setup_billing_unit as fixtures
from .website_contract import WebsiteAuthorityError


@override_settings(**fixtures.CmsSetupBillingTests._overridden_settings)
class CmsSetupRefundTests(SimpleTestCase):
    configure = fixtures.CmsSetupBillingTests.configure
    reserve = fixtures.CmsSetupBillingTests.reserve
    hold = fixtures.CmsSetupBillingTests.hold

    def setUp(self):
        fixtures.CmsSetupBillingTests.setUp(self)
        self.hold()

    def queued(self):
        self.parent.state, self.parent.next_attempt_at = "cancelled", None
        return refunds.reserve_refund(self.parent, now=self.now)

    def advance(self, row): return refunds.advance_refund(row.pk, now=self.now)

    def test_one_original_credit_job_settles_without_provider_or_cf_after_owner_revocation_and_flags_off(self):
        row = self.queued()
        self.assertIs(self.queued(), row)
        self.connection.state = "disconnected"
        self.connection.generation += 1
        self.connection.capabilities = {"_cms_financial_records": True}
        with override_settings(ARTICLES_CMS_SETUP_ENABLED=False, ARTICLES_CMS_CHANNEL_ENABLED=False):
            self.assertEqual(self.advance(row), "completed")
            self.assertIsNone(self.advance(row))
        self.assertEqual(self.parent.receipt["first_article_billing"]["status"], "refunded")
        self.refund.assert_called_once(); self.http.assert_not_called()

    def test_lost_credit_ack_observes_actual_credit_and_avoids_another_request(self):
        row = self.queued()
        self.refunds.return_value.exists.return_value = True
        self.refunds.return_value.first.return_value = self.credit
        self.assertEqual(self.advance(row), "completed")
        self.refund.assert_not_called()

    def test_lost_completed_transaction_ack_returns_actual_completion_without_replacing_the_committed_job(self):
        row = self.queued()
        self.refunds.return_value.exists.return_value = True
        self.refunds.return_value.first.return_value = self.credit
        release = billing.release
        def committed(*args, **kwargs):
            release(*args, **kwargs)
            row.state, row.attempts, row.next_attempt_at = "completed", row.attempts + 1, None
            row.receipt = {"status": "credited"}
            raise OSError("Synthetic lost committed transaction acknowledgement")
        with patch.object(billing, "release", side_effect=committed): self.assertEqual(self.advance(row), "completed")
        self.assertEqual((row.state, row.attempts, row.receipt), ("completed", 1, {"status": "credited"}))
        self.refund.assert_not_called()

    def test_transient_credit_failure_stays_durable_with_original_intent_and_receipt(self):
        row = self.queued()
        original = deepcopy((self.parent.payload, self.parent.receipt))
        self.refund.side_effect = WebsiteAuthorityError("website_prepare_dependency_transient", "Synthetic credit acknowledgement pending.", retryable=True)
        self.assertEqual(self.advance(row), "pending")
        self.assertEqual(row.state, "pending")
        self.assertGreater(row.next_attempt_at, self.now)
        self.assertEqual((self.parent.payload, self.parent.receipt), original)
        self.assertEqual(authority.historical(self.parent), self.intent)

    def test_other_parent_or_changed_private_intent_does_not_authorize_credit(self):
        row = self.queued()
        row.payload["intent_digest"] = "0" * 64
        self.assertEqual(self.advance(row), "needs_attention")
        self.refund.assert_not_called()

    def test_promoted_parent_or_actual_dispatch_prevents_unused_setup_credit(self):
        row = self.queued()
        self.parent.action = "cms-goal"
        self.assertEqual(self.advance(row), "needs_attention")
        self.parent.action = authority.ACTION
        row.state, row.next_attempt_at = "pending", self.now
        self.outbox_rows.return_value.exists.return_value = True
        self.assertEqual(self.advance(row), "needs_attention")
        self.refund.assert_not_called()

    def test_lifecycle_cancellation_fences_lease_and_queues_money_before_key_or_generation_change(self):
        self.parent.state = "running"
        self.parent.payload["lease_id"] = self.lease
        held = deepcopy(self.parent.receipt["first_article_billing"])
        refunds.cancel_held_setups(self.connection)
        self.assertEqual(self.parent.state, "cancelled")
        self.assertNotIn("lease_id", self.parent.payload)
        self.assertEqual(self.parent.receipt["first_article_billing"], held)
        self.assertEqual(len([row for row in self.saved if row.action == refunds.ACTION]), 1)
        self.refund.assert_not_called()

    def test_privacy_erasure_keeps_original_private_proof_until_credit_and_then_erases_planning_data(self):
        row = self.queued()
        self.assertTrue(refunds.retain_for_erasure(self.connection))
        self.assertEqual(authority.historical(self.parent), self.intent)
        self.assertTrue(row.payload["purge_after_reconciliation"])
        self.assertEqual(set(self.parent.receipt), {"first_article_billing"})
        self.assertEqual(self.advance(row), "completed")
        self.assertEqual((self.parent.payload, self.parent.state), ({}, "deleted"))
        self.assertEqual((self.child.payload, self.child.state), ({}, "deleted"))
        self.assertEqual(self.parent.receipt, {"status": "financial_records_settled"})
        self.refund.assert_called_once()

    def test_already_settled_unused_credit_can_erase_private_intent_without_another_financial_action(self):
        row = self.queued()
        self.assertEqual(self.advance(row), "completed")
        self.assertFalse(refunds.retain_for_erasure(self.connection))
        self.assertEqual(self.child.payload, {})
        self.assertEqual(self.parent.payload, {})
        self.refund.assert_called_once()

    def test_active_or_future_credit_job_cannot_credit_or_create_an_unused_money_decision(self):
        self.assertIsNone(refunds.reserve_refund(self.parent, now=self.now))
        row = self.queued()
        row.next_attempt_at = self.now + timedelta(minutes=1)
        self.assertIsNone(self.advance(row))
        self.refund.assert_not_called()
