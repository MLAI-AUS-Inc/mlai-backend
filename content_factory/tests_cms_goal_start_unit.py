"""Owner setup reserves exactly one original native goal/price, with no HTTP."""

from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import cms_goal_start as start
from . import cms_goal_authority as authority
from . import tests_cms_goal_billing_unit as fixtures
from .website_connections import contract_for
from .website_contract import WebsiteAuthorityError


@override_settings(**fixtures.CmsGoalBillingTests._overridden_settings)
class CmsGoalStartTests(SimpleTestCase):
    configure = fixtures.CmsGoalBillingTests.configure
    reserve = fixtures.CmsGoalBillingTests.reserve

    def setUp(self):
        fixtures.CmsGoalBillingTests.setUp(self)
        self.saved.remove(self.goal_operation)
        self.data = {**contract_for(self.connection), "client_request_id": str(uuid4()),
            "expected_policy_id": self.policy["operation_id"], "review_policy": "auto", "expected_cost_points": 6, "billing_confirmed": True}
        def debit(**kwargs):
            self.ledger.reference_id = kwargs["reference_id"]
            self.ledger.idempotency_key = kwargs["idempotency_key"]
            return self.ledger, True
        self.spend.side_effect = debit
        self.http.reset_mock()

    def start(self, **changes): return start.start(self.context, self.config, user=self.user, data={**self.data, **changes}, now=self.now)

    def test_one_owner_action_reserves_one_goal_original_price_and_payment_without_provider_http(self):
        operation = self.start()
        self.assertEqual((operation.action, operation.state), (authority.ACTION, "pending"))
        self.assertEqual(operation.receipt["first_article_billing"]["cost_points"], 6)
        self.assertEqual(operation.payload["consent"]["answers"]["publish_mode"], "auto")
        self.assertIsNone(operation.payload["source_run_id"])
        self.assertIsNone(operation.payload["generation_data"])
        self.assertEqual(operation.receipt["native_budget"]["started_at"], operation.receipt["goal"]["started_at"])
        self.spend.assert_called_once(); self.http.assert_not_called()

    def test_same_key_or_concurrent_different_key_reuses_original_site_goal_without_a_second_debit(self):
        first = self.start()
        self.assertIs(self.start(), first)
        self.assertIs(self.start(client_request_id=str(uuid4())), first)
        self.spend.assert_called_once()
        self.assertEqual(len([row for row in self.saved if row.action == authority.ACTION]), 1)
        self.price.return_value = 99
        self.assertIs(self.start(), first)
        self.spend.assert_called_once()

    def test_stale_policy_wrong_owner_or_changed_connection_does_not_reserve_goal_or_money(self):
        for changed in ({"expected_policy_id": str(uuid4())}, {"connection_generation": 2}, {"review_policy": "review"},
                {"billing_confirmed": 1}, {"billing_confirmed": False}, {"expected_cost_points": True},
                {"expected_cost_points": -1}, {"client_request_id": "not-a-uuid"}):
            with self.subTest(changed=changed), self.assertRaises(WebsiteAuthorityError): self.start(**changed)
        self.spend.assert_not_called()
        self.assertFalse(any(row.action == authority.ACTION for row in self.saved))

    def test_current_quote_must_match_before_debit_and_terminal_replay_never_becomes_new_spend(self):
        self.price.return_value = 99
        with self.assertRaises(WebsiteAuthorityError): self.start()
        self.spend.assert_not_called()
        # The real enclosing transaction rolls the failed reservation back.
        self.saved[:] = [row for row in self.saved if row.action != authority.ACTION]
        self.price.return_value = 6
        operation = self.start()
        for state in ("cancelled", "stopped", "needs_attention", "completed"):
            operation.state = state
            self.assertIs(self.start(), operation)
        self.spend.assert_called_once()
        with self.assertRaises(WebsiteAuthorityError): self.start(expected_cost_points=99)

    def test_projection_exposes_milestone_status_without_private_provider_or_financial_material(self):
        operation = self.start()
        projected = start.projection(operation, user=self.user)
        self.assertEqual(projected["id"], str(operation.pk))
        self.assertEqual(len(projected["milestones"]), 9)
        for private in ("api_origin", "consent", "payer_user_id", "spend_ledger_id", "policy_digest", "credential_operation_id", "synthetic-application-password"):
            self.assertNotIn(private, repr(projected))
        self.assertFalse(any("proof" in row for row in projected["milestones"]))

    def test_disabled_native_start_cannot_read_provider_or_reserve_a_goal(self):
        with override_settings(ARTICLES_CMS_GOALS_ENABLED=False), self.assertRaises(WebsiteAuthorityError): self.start()
        self.spend.assert_not_called(); self.http.assert_not_called()
