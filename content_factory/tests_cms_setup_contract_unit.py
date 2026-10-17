"""Original setup choices stay closed and separate from current work permission."""

from copy import deepcopy
from datetime import timedelta, timezone
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import cms_setup_contract as contract
from . import cms_setup_quote as quotes
from . import tests_cms_setup_quote_unit as fixtures
from .channel_artifact_contract import digest


def sealed(value):
    return {**value, "digest": digest({key: item for key, item in value.items() if key != "digest"})}


@override_settings(**fixtures.CmsSetupQuoteTests._overridden_settings)
class CmsSetupContractTests(SimpleTestCase):
    configure = fixtures.CmsSetupQuoteTests.configure
    reserve = fixtures.CmsSetupQuoteTests.reserve

    def setUp(self):
        fixtures.CmsSetupQuoteTests.setUp(self)
        self.quote = quotes.for_context(self.context, self.config, user=self.user, expected_binding=self.binding)
        self.identifier, self.request_id = str(uuid4()), str(uuid4())
        self.answers = {"review_policy": "auto", "publication_confirmed": True, "native_defaults_confirmed": True,
            "billing_confirmed": True, "expected_cost_points": self.quote["firstArticlePoints"]}

    def make(self, **changes):
        return contract.make_intent(self.quote, **{"operation_id": self.identifier, "request_id": self.request_id,
            "binding": self.binding, "credential_operation_id": str(self.private.pk), "user_id": self.user.pk,
            "answers": self.answers, "now": self.now, **changes})

    def test_original_choices_round_trip_without_a_goal_hold_or_provider_request(self):
        value = self.make()
        self.assertEqual(contract.validate_intent(value, expected_binding=self.binding), value)
        self.assertEqual(value["accepted_at"], self.now.isoformat())
        self.assertEqual(value["operation_id"], self.identifier)
        self.assertEqual(value["answers"]["expected_cost_points"], 6)
        self.assertEqual(value["quote"]["policy"]["id"], self.policy["operation_id"])
        self.assertNotIn("goal_id", value)
        self.spend.assert_not_called(); self.http.assert_not_called()

    def test_explicit_review_can_replace_the_displayed_default_but_missing_choice_cannot(self):
        value = self.make(answers={**self.answers, "review_policy": "review"})
        self.assertEqual(value["quote"]["policy"]["reviewPolicy"], "auto")
        self.assertEqual(value["answers"]["review_policy"], "review")
        for mode in (None, True, "", "automatic", []):
            with self.subTest(mode=mode), self.assertRaises(contract.CmsSetupContractError):
                self.make(answers={**self.answers, "review_policy": mode})

    def test_no_existing_policy_remains_absent_instead_of_becoming_invented_consent(self):
        self.quote = sealed({**self.quote, "policy": None})
        value = self.make()
        self.assertIsNone(value["quote"]["policy"])
        self.assertNotIn("policy_operation_id", value)
        self.assertNotIn("native_template", value)

    def test_zero_price_still_requires_explicit_billing_and_publication_confirmations(self):
        self.quote = sealed({**self.quote, "firstArticlePoints": 0})
        free = {**self.answers, "expected_cost_points": 0}
        self.assertEqual(self.make(answers=free)["answers"]["expected_cost_points"], 0)
        for key in ("publication_confirmed", "native_defaults_confirmed", "billing_confirmed"):
            for choice in (False, None, 1, "true"):
                with self.subTest(key=key, choice=choice), self.assertRaises(contract.CmsSetupContractError):
                    self.make(answers={**free, key: choice})

    def test_wrong_or_coerced_price_and_unknown_answers_do_not_become_accepted(self):
        for price in (0, 7, True, 6.0, "6", None):
            with self.subTest(price=price), self.assertRaises(contract.CmsSetupContractError):
                self.make(answers={**self.answers, "expected_cost_points": price})
        with self.assertRaises(contract.CmsSetupContractError): self.make(answers={**self.answers, "approved": True})
        with self.assertRaises(contract.CmsSetupContractError): self.make(answers={key: value for key, value in self.answers.items() if key != "native_defaults_confirmed"})

    def test_quote_scope_cannot_borrow_another_company_connection_generation_or_platform(self):
        for key, value in (("companyId", str(uuid4())), ("connectionId", str(uuid4())),
                ("connectionGeneration", self.binding["generation"] + 1), ("platform", "ghost")):
            original = self.quote
            self.quote = sealed({**original, key: value})
            with self.subTest(key=key), self.assertRaises(contract.CmsSetupContractError): self.make()
            self.quote = original

    def test_intent_validation_needs_the_separately_resolved_original_binding(self):
        value = self.make()
        for changes in ({"company_id": str(uuid4())}, {"connection_id": str(uuid4())},
                {"generation": self.binding["generation"] + 1}, {"owner_user_id": str(self.user.pk + 1)}):
            with self.subTest(changes=changes), self.assertRaises(contract.CmsSetupContractError):
                contract.validate_intent(value, expected_binding={**self.binding, **changes})

    def test_noncanonical_ids_and_wrong_actor_do_not_identify_an_original_intent(self):
        for changes in ({"operation_id": "not-a-uuid"}, {"request_id": True}, {"credential_operation_id": "secret-private-key"},
                {"user_id": self.user.pk + 1}, {"request_id": self.request_id.upper()}):
            with self.subTest(changes=changes), self.assertRaises(contract.CmsSetupContractError): self.make(**changes)

    def test_accepted_time_requires_canonical_utc_and_is_preserved_across_validation(self):
        for now in (self.now.replace(tzinfo=None), self.now.astimezone(timezone(timedelta(hours=1)))):
            with self.assertRaises(contract.CmsSetupContractError): self.make(now=now)
        value = self.make()
        self.assertEqual(contract.validate_intent(value, expected_binding=self.binding)["accepted_at"], value["accepted_at"])

    def test_constructor_and_validator_do_not_share_mutable_input_objects(self):
        value = self.make()
        before = deepcopy(value)
        self.quote["policy"]["author"] = "Changed after acceptance"
        self.answers["review_policy"] = "review"
        self.binding["generation"] += 1
        self.assertEqual(value, before)
        validated = contract.validate_intent(value, expected_binding=before["binding"])
        validated["answers"]["review_policy"] = "review"
        self.assertEqual(value, before)

    def test_unknown_private_fields_versions_or_changed_digests_are_rejected(self):
        original = self.make()
        for changes in ({"version": True}, {"version": 2}, {"kind": "cms_site_goal"}, {"lease_id": str(uuid4())},
                {"digest": "f" * 64}, {"approved": True}):
            with self.subTest(changes=changes), self.assertRaises(contract.CmsSetupContractError):
                contract.validate_intent({**original, **changes}, expected_binding=self.binding)

    def test_quote_rejects_even_rechecksummed_unknown_fields_and_malformed_metadata(self):
        for changes in ({"version": True}, {"setupPoints": False}, {"setupPoints": 6}, {"goalTarget": "ready"},
                {"configurationRevision": 0}, {"firstArticlePoints": True}, {"api_origin": "https://synthetic-private.test/"},
                {"policy": {**self.quote["policy"], "author": "x" * 201}},
                {"policy": {**self.quote["policy"], "native_template": {"author": 999}}}):
            with self.subTest(changes=changes), self.assertRaises(contract.CmsSetupContractError) as error:
                contract.validate_quote(sealed({**self.quote, **changes}))
            self.assertNotIn("synthetic-private", str(error.exception))

    def test_both_native_platforms_use_actual_binding_and_quote_without_git_fields(self):
        binding = {**self.binding, "platform": "ghost", "api_origin": "https://admin.synthetic-ghost.test/"}
        quote = sealed({**self.quote, "platform": "ghost"})
        value = contract.make_intent(quote, operation_id=self.identifier, request_id=self.request_id, binding=binding,
            credential_operation_id=str(self.private.pk), user_id=self.user.pk, answers=self.answers, now=self.now)
        self.assertEqual(contract.validate_intent(value, expected_binding=binding)["binding"], binding)
        self.assertFalse(any("repo" in key or "sha" in key for key in value))
