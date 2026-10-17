"""Quotes disclose current real choices without creating a goal or a debit."""

from copy import deepcopy
import json
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from . import cms_setup_quote as quotes
from . import cms_publication_policy as policies
from . import tests_cms_goal_start_unit as fixtures
from .channel_artifact_contract import digest


@override_settings(**{**fixtures.CmsGoalStartTests._overridden_settings, "ARTICLES_CMS_STATUS_ENABLED": True})
class CmsSetupQuoteTests(SimpleTestCase):
    configure = fixtures.CmsGoalStartTests.configure
    reserve = fixtures.CmsGoalStartTests.reserve

    def setUp(self):
        fixtures.CmsGoalStartTests.setUp(self)
        self.connection.refresh_from_db = Mock()
        self.goal_operation.updated_at = self.now

    def read(self):
        return quotes.for_context(self.context, self.config, user=self.user, expected_binding=self.binding)

    def test_current_policy_and_price_are_read_without_a_hold_provider_request_or_goal(self):
        before = deepcopy((self.connection.capabilities, [(row.action, row.payload, row.receipt) for row in self.saved]))
        value = self.read()
        self.assertEqual((value["firstArticlePoints"], value["setupPoints"], value["goalTarget"]), (6, 0, "first_article_live"))
        self.assertEqual(value["policy"], policies.projection(self.policy))
        self.assertEqual(value["connectionGeneration"], self.binding["generation"])
        self.assertEqual(value["digest"], digest({key: item for key, item in value.items() if key != "digest"}))
        self.assertEqual(before, (self.connection.capabilities, [(row.action, row.payload, row.receipt) for row in self.saved]))
        self.spend.assert_not_called(); self.http.assert_not_called()

    def test_missing_policy_does_not_invent_native_defaults_or_review_consent(self):
        self.connection.capabilities.pop(policies.REFERENCE)
        value = self.read()
        self.assertIsNone(value["policy"])
        self.assertEqual(value["platform"], self.binding["platform"])
        self.assertEqual(value["imagePlacement"], "article")
        self.http.assert_not_called(); self.spend.assert_not_called()

    def test_zero_is_a_real_quote_and_unknown_or_changed_prices_are_not_zero(self):
        self.price.return_value = 0
        self.assertEqual(self.read()["firstArticlePoints"], 0)
        for value in (None, True, -1, 1.5, "6", 10**9):
            self.price.return_value = value
            self.assertIsNone(self.read())
        self.price.side_effect = [6, 7]
        self.assertIsNone(self.read())

    def test_scope_revision_and_exact_recorded_mode_change_the_quote_identity(self):
        original = self.read()
        self.connection.configuration_version += 1
        revised = self.read()
        self.assertNotEqual(original["digest"], revised["digest"])
        self.assertEqual(revised["configurationRevision"], self.connection.configuration_version)
        self.configure(review_policy="review", client_request_id="11111111-2222-3333-4444-555555555555", expected_policy_id=self.policy["operation_id"], publication_confirmed=True, native_defaults_confirmed=True)
        value = self.read()
        self.assertEqual(value["policy"]["reviewPolicy"], "review")
        self.assertNotEqual(value["digest"], revised["digest"])

    def test_existing_or_terminal_goal_never_offers_a_new_start_or_payment(self):
        self.saved.append(self.goal_operation)
        for state in ("pending", "completed", "stopped", "cancelled"):
            self.goal_operation.state = state
            self.assertIsNone(self.read())
        self.spend.assert_not_called()

    def test_quote_contains_no_credential_provider_origin_or_private_policy_fields(self):
        rendered = json.dumps(self.read())
        for private in ("api_origin", "credential_operation_id", "native_template", "owner_user_id", "billing_request_id", "synthetic-application-password", self.key):
            self.assertNotIn(private, rendered)

    def test_owner_change_during_price_read_or_mismatched_expected_binding_denies_quote(self):
        def changed(*args):
            self.user.is_active = False
            return 6
        self.price.side_effect = changed
        self.assertIsNone(self.read())
        self.user.is_active = True
        self.price.side_effect = None
        self.assertIsNone(quotes.for_context(self.context, self.config, user=self.user,
            expected_binding={**self.binding, "generation": self.binding["generation"] + 1}))
        self.spend.assert_not_called(); self.http.assert_not_called()

    def test_feature_off_does_not_read_authority_credentials_or_price(self):
        with override_settings(ARTICLES_CMS_STATUS_ENABLED=False), patch.object(quotes, "_current") as current:
            self.assertIsNone(self.read())
        current.assert_not_called()


    def test_unconfigured_status_retains_the_scoped_quote_without_a_git_setup_plan(self):
        from .cms_goal_status import for_context
        value = for_context(self.context, self.config, user=self.user, now=self.now)
        self.assertEqual(value["nativeSetup"]["firstArticlePoints"], 6)
        self.assertEqual(value["action"]["id"], "cms_configure")
        self.assertNotIn("setupPlan", value)
        self.http.assert_not_called(); self.spend.assert_not_called()

    def test_a_concurrent_goal_start_cannot_return_a_second_setup_quote(self):
        from .cms_goal_status import for_context
        def started(*args):
            if self.goal_operation not in self.saved: self.saved.append(self.goal_operation)
            return 6
        self.price.side_effect = started
        value = for_context(self.context, self.config, user=self.user, now=self.now)
        self.assertEqual(value["operationId"], str(self.goal_operation.pk))
        self.assertNotIn("nativeSetup", value)
        self.assertNotEqual((value.get("action") or {}).get("id"), "cms_configure")
        self.spend.assert_not_called()

    def test_invalid_configuration_revision_is_not_a_current_quote(self):
        for value in (0, True, "2", -1):
            self.connection.configuration_version = value
            self.assertIsNone(self.read())
