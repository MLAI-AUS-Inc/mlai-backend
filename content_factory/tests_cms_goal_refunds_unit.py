"""Native credits survive revocation/erasure without losing earned-work proof."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.db import OperationalError
from django.test import SimpleTestCase, override_settings

from . import cms_goal_refunds as refunds
from . import cms_goal_billing as billing
from . import cms_goal_contract as goals
from . import cms_goal_settlement as settlement
from . import tests_cms_goal_billing_unit as fixtures
from .dispatch_models import ContentFactoryDispatchOutbox
from .website_models import WebsiteConnectionOperation


@override_settings(ARTICLES_CHANNEL_DISCOVERY_ENABLED=True, ARTICLES_PUBLISHING_CHANNELS_ENABLED=True,
    ARTICLES_CMS_CHANNEL_ENABLED=True, ARTICLES_CHANNEL_ARTIFACTS_ENABLED=True, ARTICLES_CMS_GOALS_ENABLED=True,
    ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
    ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"],
    CONNECTOR_CREDENTIAL_KEYS=json.dumps({"test": fixtures.fixtures.fixtures.TEST_KEY}), CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="test")
class CmsGoalRefundTests(SimpleTestCase):
    configure = fixtures.CmsGoalBillingTests.configure
    reserve = fixtures.CmsGoalBillingTests.reserve
    hold = fixtures.CmsGoalBillingTests.hold
    prepare_generation = fixtures.CmsGoalBillingTests.prepare_generation
    consume = fixtures.CmsGoalBillingTests.consume

    def setUp(self):
        fixtures.CmsGoalBillingTests.setUp(self)
        self.prepare_generation()
        (_payer, _ledger, _cost), payload = self.consume()
        payload.update(roo_points_authorized=True, roo_points_action="article_generation", roo_points_billing_status="charged")
        self.outbox.client_request_id = self.key
        self.outbox.payload = {"request": deepcopy(payload)}
        self.outbox.save = Mock()
        self.dispatch_lock = self.stack.enter_context(patch.object(ContentFactoryDispatchOutbox.objects, "select_for_update"))
        self.dispatch_lock.return_value.filter.return_value.first.return_value = self.outbox
        self.credit = SimpleNamespace(user_id=5, delta_microroo=6_000_000, idempotency_key="content_factory:refund:" + self.key)
        self.credit_call = self.stack.enter_context(patch("integrations.services.article_generation._refund_content_factory_request", return_value=self.credit))
        @contextmanager
        def atomic(*args, **kwargs):
            # Model the DB transaction boundary, including rollback before the
            # real compare-and-set retry, without opening a local database.
            fields = ("payload", "receipt", "state", "attempts", "next_attempt_at", "updated_at")
            saved = [(row, {key: deepcopy(getattr(row, key, None)) for key in fields}) for row in self.saved]
            original_dispatch = deepcopy(self.outbox.payload)
            try:
                yield
            except Exception:
                for row, values in saved:
                    for key, value in values.items(): setattr(row, key, value)
                self.outbox.payload = original_dispatch
                raise
        self.stack.enter_context(patch("django.db.transaction.atomic", side_effect=atomic))

    def reserve_credit(self):
        self.goal_operation.state, self.goal_operation.next_attempt_at = "cancelled", None
        self.goal_operation.payload.pop("lease_id", None)
        return refunds.reserve_refund(self.goal_operation, now=self.now)

    def advance_credit(self, row, *, now=None):
        return refunds.advance_refund(row.pk, now=now or self.now + timedelta(seconds=1))

    def earn_draft(self):
        original = {"run_id": "original-native-run", "run_generation": 0, "envelope_digest": self.payload["publishing_channel_artifact"]["digest"]}
        value = goals.bind_source(self.goal, original, expected_scope=self.scope, now=self.now)
        proof = goals.make_proof("first_draft", expected_scope=self.scope, original_source=original, subject_digest="e" * 64, at=self.now)
        self.goal_operation.receipt["goal"] = goals.refresh(value, {"first_draft": proof}, expected_scope=self.scope, now=self.now)
        self.goal_operation.payload["source_run_id"] = original["run_id"]

    def test_original_credit_is_queued_once_and_settles_after_revocation_and_disabled_flags(self):
        row = self.reserve_credit()
        self.assertIs(refunds.reserve_refund(self.goal_operation, now=self.now), row)
        self.credit_call.assert_not_called()
        self.connection.generation = 2; self.connection.state = "disconnected"; self.private.state = "deleted"
        with override_settings(ARTICLES_CMS_GOALS_ENABLED=False, ARTICLES_CMS_CHANNEL_ENABLED=False):
            self.assertEqual(self.advance_credit(row), "completed")
        self.assertEqual((row.state, row.receipt["status"]), ("completed", "credited"))
        self.assertEqual(self.goal_operation.receipt["first_article_billing"]["status"], "refunded")
        self.assertEqual(settlement.validate(self.outbox)["decision"], "refunded")
        self.assertIsNone(self.advance_credit(row))
        self.credit_call.assert_called_once()
        self.assertIs(self.credit_call.call_args.kwargs["user"], self.payer)

    def test_native_credit_locks_organization_before_operation_and_original_ledger(self):
        row = self.reserve_credit(); order = []
        from organizations.models import Organization
        with patch.object(Organization.objects, "select_for_update", side_effect=lambda: order.append("organization") or Mock()), \
                patch.object(WebsiteConnectionOperation.objects, "select_for_update", side_effect=lambda: order.append("operation") or self.manager):
            self.assertEqual(self.advance_credit(row), "completed")
        self.assertEqual(order[0], "organization")
        self.assertIn("operation", order[1:])

    def test_useful_native_draft_keeps_original_charge_and_records_minimized_financial_audit(self):
        self.earn_draft()
        row = self.reserve_credit()
        self.assertEqual(row.payload["retention_subject"]["basis"], "goal_first_draft")
        self.assertEqual(self.advance_credit(row), "completed")
        self.assertEqual(row.receipt["status"], "retained_after_draft")
        self.assertEqual(self.goal_operation.receipt["first_article_billing"]["status"], "reserved")
        self.assertEqual(settlement.validate(self.outbox)["decision"], "retained")
        self.credit_call.assert_not_called()

    def test_handoff_snapshot_prevents_credit_after_purge_even_before_parent_collected_draft(self):
        fixtures.CmsGoalBillingTests.test_private_accepted_handoff_retains_generation_charge_even_before_parent_collects_draft_proof(self)
        row = self.reserve_credit()
        self.assertEqual(row.payload["retention_subject"]["basis"], "private_native_handoff")
        self.goal_operation.payload["generation_data"] = {"topic": "Synthetic private planning"}
        self.goal_operation.receipt["private_preview"] = "Synthetic private planning"
        refunds.retain_for_erasure(self.connection)
        self.assertIsNone(self.goal_operation.payload["generation_data"])
        self.assertNotIn("private_preview", self.goal_operation.receipt)
        WebsiteConnectionOperation.objects.filter(action="cms-review").update(payload={}, state="deleted", receipt={"status": "erased"})
        self.assertEqual(self.advance_credit(row), "completed")
        self.assertEqual((self.goal_operation.state, self.goal_operation.payload), ("deleted", {}))
        self.assertEqual(settlement.validate(self.outbox)["decision"], "retained")
        self.assertTrue(billing.defer_child_refund({"client_request_id": self.key}, resolved_domain="fixture.test"))
        self.credit_call.assert_not_called()

    def test_already_committed_credit_wins_a_stale_retention_snapshot_without_another_refund(self):
        self.earn_draft(); row = self.reserve_credit()
        self.refunds.return_value.exists.return_value = True
        self.refunds.return_value.first.return_value = self.credit
        self.assertEqual(self.advance_credit(row), "completed")
        self.assertEqual((row.receipt["status"], self.goal_operation.receipt["first_article_billing"]["status"]), ("credited", "refunded"))
        self.assertEqual(settlement.validate(self.outbox)["decision"], "refunded")
        self.credit_call.assert_not_called()

    def test_database_credit_failure_rolls_back_then_retries_the_original_hold(self):
        row = self.reserve_credit()
        self.credit_call.side_effect = OperationalError("synthetic credit failure")
        self.assertEqual(self.advance_credit(row), "pending")
        self.assertEqual((row.state, row.attempts), ("pending", 1))
        self.assertEqual(self.goal_operation.receipt["first_article_billing"]["status"], "reserved")
        self.assertNotIn(settlement.REFERENCE, self.outbox.payload)
        self.credit_call.side_effect = None
        self.assertEqual(self.advance_credit(row, now=row.next_attempt_at + timedelta(seconds=1)), "completed")
        keys = [call.kwargs["article_request"]["client_request_id"] for call in self.credit_call.call_args_list]
        self.assertEqual(keys, [self.key, self.key])

    def test_missing_credit_acknowledgement_stays_scheduled_instead_of_claiming_settlement(self):
        row = self.reserve_credit()
        self.credit_call.return_value = None
        self.assertEqual(self.advance_credit(row), "pending")
        self.assertEqual(row.receipt["status"], "credit_pending")
        self.assertEqual(self.goal_operation.receipt["first_article_billing"]["status"], "reserved")

    def test_foreign_snapshot_or_boolean_schema_cannot_select_a_different_credit(self):
        row = self.reserve_credit()
        row.payload["scope_digest"] = "f" * 64
        self.assertEqual(self.advance_credit(row), "needs_attention")
        self.credit_call.assert_not_called()
        self.assertEqual(self.goal_operation.receipt["first_article_billing"]["status"], "reserved")

    def test_abandoned_credit_lease_reclaims_once_and_keeps_parent_cancelled(self):
        row = self.reserve_credit()
        row.state, row.next_attempt_at = "running", self.now - timedelta(seconds=1)
        self.assertEqual(self.advance_credit(row), "completed")
        self.assertEqual(self.goal_operation.state, "cancelled")
        self.credit_call.assert_called_once()

    def test_erasure_after_completed_credit_immediately_removes_parent_planning_and_consent(self):
        row = self.reserve_credit()
        self.assertEqual(self.advance_credit(row), "completed")
        self.assertFalse(refunds.retain_for_erasure(self.connection))
        self.assertEqual((self.goal_operation.state, self.goal_operation.payload), ("deleted", {}))
        self.assertTrue(row.payload["purge_after_reconciliation"])
        self.assertEqual(settlement.validate(self.outbox)["decision"], "refunded")
