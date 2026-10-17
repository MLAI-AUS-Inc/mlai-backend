"""Managed native work stops without losing the original read recovery path."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import cms_goal_authority as authority
from . import cms_goal_budget as budget
from . import cms_goal_effects as effects
from . import channel_artifacts
from . import tests_cms_goal_billing_unit as fixtures
from .website_contract import WebsiteAuthorityError


@override_settings(**fixtures.CmsGoalBillingTests._overridden_settings)
class CmsGoalEffectsTests(SimpleTestCase):
    configure = fixtures.CmsGoalBillingTests.configure
    reserve = fixtures.CmsGoalBillingTests.reserve
    hold = fixtures.CmsGoalBillingTests.hold
    prepare_generation = fixtures.CmsGoalBillingTests.prepare_generation

    def setUp(self):
        fixtures.CmsGoalBillingTests.setUp(self)
        self.prepare_generation()
        self.payload.update(roo_points_ledger_id="11", roo_points_cost=6, roo_points_billing_user_id=5)
        self.outbox.payload = {"request": deepcopy(self.payload)}
        self.outbox.organization_id = 7
        self.goal_operation.receipt["native_budget"] = budget.initial(self.goal)

    def test_active_original_goal_admits_work_and_reuses_the_original_held_payment(self):
        self.assertIs(effects.require_work(self.payload, now=self.now), self.goal_operation)
        self.assertEqual(self.spend.call_count, 1)
        self.assertEqual(self.goal_operation.receipt["first_article_billing"]["spend_ledger_id"], "11")

    def test_stopped_needs_review_completed_or_superseded_goal_cannot_admit_new_work(self):
        for state in ("stopped", "cancelled", "needs_user", "completed", "deleted", "needs_attention"):
            self.goal_operation.state = state
            with self.subTest(state=state), self.assertRaises(effects.CmsGoalWorkDenied): effects.require_work(self.payload)
        self.goal_operation.state = "running"
        self.goal_operation.payload["policy_digest"] = "f" * 64
        with self.assertRaises(effects.CmsGoalWorkDenied): effects.require_work(self.payload)

    def test_private_source_reads_survive_a_stopped_goal_but_model_admission_does_not(self):
        self.goal_operation.state = "stopped"
        self.assertEqual(channel_artifacts.authorize_payload(self.payload).envelope, self.payload[channel_artifacts.ENVELOPE])
        query = {"billing_request_id": self.key, "artifact_operation_id": self.payload[channel_artifacts.ENVELOPE]["artifact_operation_id"],
            "envelope_digest": self.payload[channel_artifacts.ENVELOPE]["digest"], "domain": self.payload["domain"]}
        with self.assertRaises(effects.CmsGoalWorkDenied): channel_artifacts.admission_receipt(query)

    def test_original_outbox_and_payment_cannot_be_replaced_by_worker_goal_selectors(self):
        for changed in ({"cms_goal_operation_id": str(uuid4())}, {"cms_goal_id": str(uuid4())},
                {"cms_goal_operation_id": None}, {"client_request_id": "cms-articles-first:other"}):
            with self.subTest(changed=changed), self.assertRaises(effects.CmsGoalWorkDenied):
                effects.require_work({**self.payload, **changed})
        self.outbox.payload["request"]["roo_points_cost"] = 1
        with self.assertRaises(effects.CmsGoalWorkDenied): effects.require_work(self.payload)

    def test_missing_private_budget_or_exhausted_wall_and_usage_stops_new_work(self):
        for value in (None, {}, {**budget.initial(self.goal), "version": True},
                {**budget.initial(self.goal), "budget_used": {"model_cost": 25}},
                {**budget.initial(self.goal), "budget_used": {"active_seconds": 7200}},
                {**budget.initial(self.goal), "usage_invalid": True}):
            self.goal_operation.receipt["native_budget"] = value
            with self.subTest(value=value), self.assertRaises(effects.CmsGoalWorkDenied): effects.require_work(self.payload, now=self.now)
        self.goal_operation.receipt["native_budget"] = budget.initial(self.goal)
        with self.assertRaises(effects.CmsGoalWorkDenied): effects.require_work(self.payload, now=self.now + timedelta(days=3))

    def test_required_owner_review_wait_pauses_wall_time_but_not_recorded_model_usage(self):
        value = budget.initial(self.goal)
        value["owner_wait_started_at"] = self.now.isoformat()
        self.goal_operation.receipt["native_budget"] = value
        effects.require_work(self.payload, now=self.now + timedelta(days=3))
        value["budget_used"]["model_cost"] = 25
        with self.assertRaises(effects.CmsGoalWorkDenied): effects.require_work(self.payload, now=self.now + timedelta(days=3))

    def test_new_model_work_requires_known_prior_usage_and_original_unrefunded_spend(self):
        self.goal_operation.receipt["native_budget"]["missing_usage_run_ids"] = ["original-run"]
        with self.assertRaises(effects.CmsGoalWorkDenied): effects.require_work(self.payload, now=self.now, action="first-article-generation")
        self.goal_operation.receipt["native_budget"].pop("missing_usage_run_ids")
        self.refunds.return_value.exists.return_value = True
        with self.assertRaises(effects.CmsGoalWorkDenied): effects.require_work(self.payload, now=self.now)
        self.refunds.return_value.exists.return_value = False
        self.ledger_rows.return_value.select_related.return_value.filter.return_value.first.return_value = None
        with self.assertRaises(effects.CmsGoalWorkDenied): effects.require_work(self.payload, now=self.now)

    def test_ordinary_cms_article_has_no_native_goal_lookup_or_new_budget_requirement(self):
        with patch.object(authority, "current") as current:
            self.assertIsNone(effects.require_work({"client_request_id": "ordinary-cms-paid"}))
        current.assert_not_called()

