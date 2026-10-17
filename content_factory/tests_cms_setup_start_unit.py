"""One actual owner acceptance persists the original private choices and hold."""

from copy import deepcopy
from uuid import uuid4
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from . import cms_setup_start as start
from . import cms_setup_authority as authority
from . import cms_setup_billing as billing
from . import cms_setup_quote as quotes
from . import tests_cms_setup_billing_unit as fixtures
from .website_connections import contract_for
from .website_contract import WebsiteAuthorityError


@override_settings(**fixtures.CmsSetupBillingTests._overridden_settings)
class CmsSetupStartTests(SimpleTestCase):
    configure = fixtures.CmsSetupBillingTests.configure
    reserve = fixtures.CmsSetupBillingTests.reserve

    def setUp(self):
        fixtures.CmsSetupBillingTests.setUp(self)
        self.saved.remove(self.parent)
        self.saved.remove(self.child)
        self.data = {**contract_for(self.connection), "company_id": str(self.company.pk), "client_request_id": str(uuid4()), "setup_quote_digest": self.quote["digest"],
            **deepcopy(self.intent["answers"])}
        def debit(**options):
            self.ledger.reference_id = options["reference_id"]
            self.ledger.idempotency_key = options["idempotency_key"]
            return self.ledger, True
        self.spend.side_effect = debit

    def start(self, **changes):
        return start.start(self.context, self.config, user=self.user, data={**deepcopy(self.data), **changes}, now=self.now)

    def test_one_acceptance_saves_actual_root_private_intent_and_hold_without_provider_io_or_fake_goal(self):
        value = self.start()
        self.assertEqual((value.action, value.state), (authority.ACTION, "pending"))
        self.assertEqual(authority.historical(value)["operation_id"], str(value.pk))
        self.assertEqual(value.receipt["first_article_billing"]["cost_points"], 6)
        self.assertEqual(value.receipt["native_budget"]["started_at"], authority.historical(value)["accepted_at"])
        self.assertEqual(len([row for row in self.saved if row.action == authority.ACTION]), 1)
        self.assertEqual(len([row for row in self.saved if row.action == authority.INTENT_ACTION]), 1)
        self.assertNotIn("goal", value.receipt)
        self.assertNotIn("policy_operation_id", value.payload)
        self.assertNotIn("lease_id", value.payload)
        self.spend.assert_called_once(); self.http.assert_not_called()

    def test_lost_response_and_another_click_reuse_original_site_request_and_never_debit_again(self):
        first = self.start()
        self.assertIs(self.start(), first)
        self.assertIs(self.start(client_request_id=str(uuid4())), first)
        self.price.return_value = 99
        self.assertIs(self.start(), first)
        self.spend.assert_called_once(); self.http.assert_not_called()

    def test_different_confirmations_mode_price_or_quote_cannot_mutate_an_existing_setup(self):
        first = self.start()
        before = deepcopy((first.payload, first.receipt))
        for changes in ({"review_policy": "review"}, {"expected_cost_points": 7}, {"expected_cost_points": 6.0},
                {"publication_confirmed": 1}, {"native_defaults_confirmed": False}, {"billing_confirmed": 1},
                {"setup_quote_digest": "0" * 64}, {"extra": "ignored"}):
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): self.start(**changes)
        self.assertEqual((first.payload, first.receipt), before)
        self.spend.assert_called_once()

    def test_stale_current_quote_price_or_configuration_is_not_initial_acceptance(self):
        self.price.return_value = 7
        with self.assertRaises(WebsiteAuthorityError): self.start()
        self.price.return_value = 6
        self.connection.configuration_version += 1
        with self.assertRaises(WebsiteAuthorityError): self.start()
        self.assertFalse(any(row.action == authority.ACTION for row in self.saved))
        self.spend.assert_not_called(); self.http.assert_not_called()

    def test_initial_choices_require_actual_booleans_integer_price_and_closed_input(self):
        for changes in ({"publication_confirmed": False}, {"publication_confirmed": 1}, {"native_defaults_confirmed": 1},
                {"billing_confirmed": False}, {"expected_cost_points": True}, {"expected_cost_points": -1},
                {"review_policy": "approved"}, {"setup_quote_digest": None}, {"lease_id": str(uuid4())}, {"goal": {}}):
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): self.start(**changes)
        self.spend.assert_not_called(); self.http.assert_not_called()

    def test_owner_or_site_changes_refuse_before_creating_intent_or_debit(self):
        for target, key, changed in ((self.user, "is_active", False), (self.company, "profile_id", 999),
                (self.connection, "generation", 2), (self.connection, "authorized_by_id", 999)):
            original = getattr(target, key)
            setattr(target, key, changed)
            with self.subTest(key=key), self.assertRaises(WebsiteAuthorityError): self.start()
            setattr(target, key, original)
        for changes in ({"website_connection_id": str(uuid4())}, {"github_repo": "other/repository"}, {"domain": "other.test"},
                {"company_id": str(uuid4())}, {"connection_generation": True}):
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): self.start(**changes)
        self.spend.assert_not_called(); self.http.assert_not_called()

    def test_minimal_current_company_and_connection_tuple_needs_no_empty_git_fields(self):
        data = {key: value for key, value in self.data.items() if key not in {"github_repo", "repository_id", "domain", "branch", "app_root"}}
        operation = start.start(self.context, self.config, user=self.user, data=data, now=self.now)
        self.assertEqual(operation.action, authority.ACTION)
        self.spend.assert_called_once(); self.http.assert_not_called()

    def test_explicit_current_zero_price_holds_and_settles_zero_without_a_debit_or_a_missing_paid_spend(self):
        from django.contrib.auth import get_user_model
        self.price.return_value = 0
        quote = quotes.for_context(self.context, self.config, user=self.user, expected_binding=self.binding)
        operation = self.start(setup_quote_digest=quote["digest"], expected_cost_points=0)
        receipt = operation.receipt["first_article_billing"]
        self.assertEqual(receipt["cost_points"], 0)
        self.assertIsNone(receipt["spend_ledger_id"])
        self.price.return_value = 99
        with patch.object(get_user_model().objects, "filter") as payer:
            payer.return_value.first.return_value = self.payer
            self.assertEqual(billing.require_held(operation), receipt)
        operation.state = "cancelled"
        self.assertEqual(billing.release(operation, reason="Synthetic unused free setup")["status"], "refunded")
        self.spend.assert_not_called(); self.refund.assert_not_called(); self.http.assert_not_called()

    def test_terminal_first_article_setup_remains_the_original_operation_instead_of_a_new_debit(self):
        operation = self.start()
        for state in ("cancelled", "stopped", "needs_attention"):
            operation.state = state
            self.assertIs(self.start(client_request_id=str(uuid4())), operation)
        self.spend.assert_called_once()

    def test_manual_review_choice_is_original_consent_and_not_a_git_preference(self):
        self.config.auto_publish = True
        operation = self.start(review_policy="review")
        self.assertEqual(authority.historical(operation)["answers"]["review_policy"], "review")
        self.assertEqual(billing.historical_reservation(operation)["cost_points"], 6)
        self.http.assert_not_called()

    def test_acceptance_rollout_off_does_not_read_quote_or_create_rows_or_debit(self):
        with override_settings(ARTICLES_CMS_SETUP_ENABLED=False), patch.object(quotes, "for_context") as quote:
            with self.assertRaises(WebsiteAuthorityError): self.start()
        quote.assert_not_called(); self.spend.assert_not_called()
        self.assertFalse(any(row.action == authority.ACTION for row in self.saved))
