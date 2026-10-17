"""Actual policy promotes one setup identity, original consent time and paid hold."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import cms_setup_start as start
from . import cms_setup_authority as authority
from . import cms_publication_policy as policies
from . import cms_goal_authority as native
from . import cms_goal_start as native_start
from . import cms_goal_billing as native_billing
from . import tests_cms_setup_start_unit as fixtures
from .website_connections import contract_for
from .website_contract import WebsiteAuthorityError


@override_settings(**fixtures.CmsSetupStartTests._overridden_settings)
class CmsSetupPromotionTests(SimpleTestCase):
    configure = fixtures.CmsSetupStartTests.configure
    reserve = fixtures.CmsSetupStartTests.reserve

    def setUp(self):
        fixtures.CmsSetupStartTests.setUp(self)
        self.parent = fixtures.CmsSetupStartTests.start(self)
        self.lease = str(uuid4())
        self.parent.state = "running"
        self.parent.payload["lease_id"] = self.lease
        self.parent.next_attempt_at = self.now + timedelta(minutes=3)
        self.dispatch = authority.CmsSetupDispatch(str(self.parent.pk), self.lease)

    def discover(self):
        data = authority.policy_data(authority.current(self.parent, lease_id=self.lease))
        return policies.configure(self.context, self.config, user=self.user, data=data, setup_scope=self.dispatch)

    def promote(self): return start.promote(self.dispatch, now=self.now)

    def test_actual_policy_promotes_same_operation_original_hold_and_accepted_start_without_model_dispatch(self):
        held = deepcopy(self.parent.receipt["first_article_billing"])
        intent = authority.historical(self.parent)
        identifier, key = self.parent.pk, self.parent.idempotency_key
        saved_policy = self.discover()
        self.price.return_value = 99
        result = self.promote()
        self.assertIs(result, self.parent)
        self.assertEqual((result.pk, result.idempotency_key, result.action, result.state), (identifier, key, native.ACTION, "pending"))
        self.assertEqual(result.receipt["first_article_billing"], held)
        self.assertEqual(result.receipt["goal"]["started_at"], intent["accepted_at"])
        self.assertEqual(result.payload["consent"]["accepted_at"], intent["accepted_at"])
        self.assertEqual(result.receipt["native_budget"]["started_at"], intent["accepted_at"])
        self.assertEqual(result.payload["policy_operation_id"], saved_policy["operation_id"])
        self.assertEqual(native_billing.historical_reservation(result), held)
        native.current(result, user=self.user)
        self.assertIsNone(result.payload["source_run_id"])
        self.assertNotIn("lease_id", result.payload)
        self.assertNotIn("intent_operation_id", result.payload)
        self.assertTrue(all(row["proof"] is None for row in result.receipt["goal"]["milestones"]))
        self.spend.assert_called_once()
        self.assertEqual(self.http.call_count, 2)

    def test_lost_setup_and_promotion_ack_replays_same_accepted_request_without_another_payment(self):
        self.discover()
        self.promote()
        self.http.reset_mock()
        self.price.return_value = 99
        for request in (self.data["client_request_id"], str(uuid4())):
            result = start.start(self.context, self.config, user=self.user, data={**self.data, "client_request_id": request}, now=self.now)
            self.assertIs(result, self.parent)
        self.spend.assert_called_once(); self.http.assert_not_called()
        self.assertEqual(len([row for row in self.saved if row.action == native.ACTION]), 1)

    def test_no_policy_wrong_lease_cancelled_or_expired_setup_cannot_become_a_goal(self):
        with self.assertRaises(WebsiteAuthorityError): self.promote()
        self.discover()
        for dispatch in (self.dispatch.__dict__, authority.CmsSetupDispatch(str(self.parent.pk), str(uuid4()))):
            with self.subTest(dispatch=type(dispatch)), self.assertRaises(WebsiteAuthorityError): start.promote(dispatch, now=self.now)
        self.parent.next_attempt_at = self.now - timedelta(seconds=1)
        with self.assertRaises(WebsiteAuthorityError): self.promote()
        self.parent.next_attempt_at = self.now + timedelta(minutes=3)
        self.parent.state = "cancelled"
        with self.assertRaises(WebsiteAuthorityError): self.promote()
        self.assertEqual(self.parent.action, authority.ACTION)
        self.assertNotIn("goal", self.parent.receipt)
        self.spend.assert_called_once()

    def test_changed_real_current_policy_cannot_be_adopted_as_original_setup(self):
        first = self.discover()
        self.configure(client_request_id=str(uuid4()), expected_policy_id=first["operation_id"])
        with self.assertRaises(WebsiteAuthorityError): self.promote()
        self.assertEqual(self.parent.action, authority.ACTION)
        self.assertNotIn("goal", self.parent.receipt)

    def test_unknown_or_exhausted_original_budget_and_already_credited_hold_cannot_promote(self):
        self.discover()
        original = deepcopy(self.parent.receipt["native_budget"])
        self.parent.receipt["native_budget"] = None
        with self.assertRaises(WebsiteAuthorityError): self.promote()
        self.parent.receipt["native_budget"] = original
        with override_settings(ARTICLES_GOAL_ACTIVE_SECONDS=1):
            self.parent.receipt["native_budget"]["budget_used"]["active_seconds"] = 1
            with self.assertRaises(WebsiteAuthorityError): self.promote()
        self.parent.receipt["native_budget"] = deepcopy(original)
        self.refunds.return_value.exists.return_value = True
        with self.assertRaises(WebsiteAuthorityError): self.promote()
        self.assertEqual(self.parent.action, authority.ACTION)
        self.spend.assert_called_once()

    def test_legacy_start_cannot_reserve_parallel_native_goal_while_the_original_setup_exists(self):
        saved = self.discover()
        data = {**contract_for(self.connection), "client_request_id": str(uuid4()), "expected_policy_id": saved["operation_id"],
            "review_policy": "auto", "expected_cost_points": 6, "billing_confirmed": True}
        for state in ("running", "cancelled", "stopped"):
            self.parent.state = state
            with self.subTest(state=state), self.assertRaises(WebsiteAuthorityError): native_start.start(self.context, self.config, user=self.user, data=data)
        self.spend.assert_called_once()

    def test_separate_real_backend_usage_is_carried_into_native_budget_without_an_invented_cf_report(self):
        selected = self.parent.receipt["native_budget"]
        selected["setup_usage"] = {"version": 1, "operation_id": str(self.parent.pk), "counter_semantics": "reservation_upper_bounds_v1",
            "active_seconds": 180, "provider_seconds": 40}
        selected["budget_used"] = {"active_seconds": 180, "provider_seconds": 40}
        self.discover()
        discovered = deepcopy(self.parent.receipt["native_budget"])
        self.promote()
        self.assertEqual(self.parent.receipt["native_budget"], discovered)
        self.assertEqual(discovered["usage_reports"], {})
        self.assertEqual(discovered["setup_usage"]["operation_id"], str(self.parent.pk))
        self.assertEqual(discovered["setup_usage"]["active_seconds"], 180)
        self.assertEqual(discovered["setup_usage"]["provider_seconds"], 400)
