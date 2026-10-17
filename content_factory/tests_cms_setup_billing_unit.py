"""Original pre-discovery money is held once and unused setup credits stay durable."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import cms_setup_billing as billing
from . import cms_first_article_subject as subjects
from . import tests_cms_setup_authority_unit as fixtures
from .website_contract import WebsiteAuthorityError
from .dispatch_models import ContentFactoryDispatchOutbox
from .website_first_article_billing import request_key
from roo.permissions import InsufficientBalanceError


@override_settings(**fixtures.CmsSetupAuthorityTests._overridden_settings)
class CmsSetupBillingTests(SimpleTestCase):
    configure = fixtures.CmsSetupAuthorityTests.configure
    reserve = fixtures.CmsSetupAuthorityTests.reserve

    def setUp(self):
        fixtures.CmsSetupAuthorityTests.setUp(self)
        self.parent.state = "pending"
        self.parent.payload.pop("lease_id")
        self.key = request_key(subjects.from_setup_intent(self.intent, expected_binding=self.binding))
        self.ledger.reference_id = self.key
        self.ledger.idempotency_key = "content_factory:charge:" + self.key
        self.outbox_rows = self.stack.enter_context(patch.object(ContentFactoryDispatchOutbox.objects, "filter"))
        self.outbox_rows.return_value.exists.return_value = False
        self.refund = self.stack.enter_context(patch("integrations.services.article_generation._refund_content_factory_request"))
        self.credit = SimpleNamespace(user_id=self.payer.pk, delta_microroo=6_000_000, idempotency_key="content_factory:refund:" + self.key)
        self.refund.return_value = self.credit

    def hold(self): return billing.reserve(self.parent, user=self.user, now=self.now)

    def test_original_accepted_price_is_held_once_before_any_policy_or_provider_read(self):
        original = self.hold()
        self.assertEqual((original["client_request_id"], original["cost_points"], original["status"]), (self.key, 6, "reserved"))
        self.assertEqual(self.hold(), original)
        self.assertEqual(billing.require_held(self.parent), original)
        self.assertTrue(self.connection.capabilities["_cms_financial_records"])
        self.assertEqual(self.spend.call_count, 1)
        self.http.assert_not_called()

    def test_later_current_price_or_payer_does_not_reprice_or_debit_an_original_hold(self):
        original = self.hold()
        self.price.return_value = 99
        self.payer_lookup.return_value = SimpleNamespace(pk=999, is_active=True)
        self.assertEqual(self.hold(), original)
        self.assertEqual(billing.require_held(self.parent), original)
        self.assertEqual(self.spend.call_count, 1)

    def test_missing_changed_current_price_or_balance_refuses_the_initial_hold_before_discovery(self):
        self.price.return_value = 7
        with self.assertRaises(WebsiteAuthorityError): self.hold()
        self.spend.assert_not_called()
        self.price.return_value = 6
        self.spend.side_effect = InsufficientBalanceError("synthetic balance")
        with self.assertRaises(WebsiteAuthorityError): self.hold()
        self.assertNotIn("first_article_billing", self.parent.receipt)
        self.http.assert_not_called()

    def test_raw_financial_subject_or_intent_is_not_an_actual_setup_hold(self):
        for value in (self.intent, subjects.from_setup_intent(self.intent, expected_binding=self.binding), self.parent.__dict__):
            with self.subTest(value=type(value)), self.assertRaises(WebsiteAuthorityError): billing.reserve(value, user=self.user, now=self.now)
        self.spend.assert_not_called()

    def test_revoked_owner_credential_or_rollout_cannot_create_a_hold_but_original_receipt_remains_readable(self):
        original = self.hold()
        self.connection.state = "disconnected"
        with override_settings(ARTICLES_CMS_SETUP_ENABLED=False):
            self.assertEqual(billing.historical_reservation(self.parent), original)
            with self.assertRaises(WebsiteAuthorityError): self.hold()
        self.assertEqual(self.spend.call_count, 1)

    def test_missing_spend_changed_payer_or_recorded_credit_never_becomes_usable_money(self):
        self.hold()
        self.ledger_rows.return_value.select_related.return_value.filter.return_value.first.return_value = None
        with self.assertRaises(WebsiteAuthorityError): billing.require_held(self.parent)
        self.ledger_rows.return_value.select_related.return_value.filter.return_value.first.return_value = self.ledger
        self.ledger.user_id = 999
        with self.assertRaises(WebsiteAuthorityError): billing.require_held(self.parent)
        self.ledger.user_id = self.payer.pk
        self.refunds.return_value.exists.return_value = True
        with self.assertRaises(WebsiteAuthorityError): billing.require_held(self.parent)
        self.assertEqual(self.spend.call_count, 1)

    def test_terminal_unused_hold_credits_the_actual_original_payer_without_provider_or_cf_calls(self):
        original = self.hold()
        self.parent.state = "cancelled"
        self.connection.state = "disconnected"
        with override_settings(ARTICLES_CMS_SETUP_ENABLED=False, ARTICLES_CMS_CHANNEL_ENABLED=False):
            credited = billing.release(self.parent, reason="Synthetic unused setup")
        self.assertEqual(credited["status"], "refunded")
        self.assertEqual(credited["client_request_id"], original["client_request_id"])
        self.assertIs(self.refund.call_args.kwargs["user"], self.payer)
        self.assertEqual(self.refund.call_args.kwargs["article_request"]["roo_points_ledger_id"], str(self.ledger.pk))
        self.http.assert_not_called()

    def test_lost_credit_ack_reuses_the_real_original_credit_and_never_sends_another_refund(self):
        self.hold()
        self.parent.state = "stopped"
        self.refunds.return_value.exists.return_value = True
        self.refunds.return_value.first.return_value = self.credit
        self.assertEqual(billing.release(self.parent, reason="Synthetic prior credit")["status"], "refunded")
        self.assertEqual(billing.release(self.parent, reason="Synthetic replay")["status"], "refunded")
        self.refund.assert_not_called()

    def test_active_setup_actual_outbox_or_wrong_credit_blocks_unused_settlement(self):
        self.hold()
        with self.assertRaises(WebsiteAuthorityError): billing.release(self.parent, reason="Still active")
        self.parent.state = "cancelled"
        self.outbox_rows.return_value.exists.return_value = True
        with self.assertRaises(WebsiteAuthorityError): billing.release(self.parent, reason="Actual dispatch exists")
        self.refund.assert_not_called()
        self.outbox_rows.return_value.exists.return_value = False
        self.refund.return_value = SimpleNamespace(user_id=999, delta_microroo=6_000_000, idempotency_key=self.credit.idempotency_key)
        with self.assertRaises(WebsiteAuthorityError): billing.release(self.parent, reason="Wrong payer")
        self.assertEqual(self.parent.receipt["first_article_billing"]["status"], "reserved")

    def test_altered_original_payment_fields_never_reuse_the_accepted_hold(self):
        self.hold()
        original = deepcopy(self.parent.receipt)
        for changes in ({"client_request_id": "other"}, {"payer_user_id": "0"}, {"cost_points": 7},
                {"binding": {"goal_id": str(uuid4())}}):
            self.parent.receipt = deepcopy(original)
            self.parent.receipt["first_article_billing"].update(changes)
            from .website_goal import digest
            receipt = self.parent.receipt["first_article_billing"]
            receipt["digest"] = digest({key: value for key, value in receipt.items() if key != "digest"})
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): billing.require_held(self.parent)

    def test_holds_and_credits_require_an_actual_database_transaction(self):
        with patch("django.db.transaction.get_connection", return_value=SimpleNamespace(in_atomic_block=False)):
            with self.assertRaises(WebsiteAuthorityError): self.hold()
            with self.assertRaises(WebsiteAuthorityError): billing.require_held(self.parent)
            with self.assertRaises(WebsiteAuthorityError): billing.release(self.parent, reason="Outside transaction")
        self.spend.assert_not_called(); self.refund.assert_not_called()
