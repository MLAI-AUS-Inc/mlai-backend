"""Original monetary scope survives real policy discovery, without granting work."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import Mock

from django.test import SimpleTestCase, override_settings

from . import cms_first_article_subject as subjects
from . import cms_goal_contract as goals
from . import cms_goal_consent as consent
from . import cms_goal_billing as billing
from . import cms_goal_authority as authority
from . import cms_setup_contract as setup
from . import cms_setup_quote as quotes
from . import cms_publication_policy as policies
from . import tests_cms_goal_billing_unit as fixtures
from .website_contract import WebsiteAuthorityError
from .website_first_article_billing import request_key, _binding


@override_settings(**{**fixtures.CmsGoalBillingTests._overridden_settings, "ARTICLES_CMS_STATUS_ENABLED": True, "ARTICLES_CMS_SETUP_ENABLED": True})
class CmsFirstArticleSubjectTests(SimpleTestCase):
    configure = fixtures.CmsGoalBillingTests.configure
    reserve = fixtures.CmsGoalBillingTests.reserve
    hold = fixtures.CmsGoalBillingTests.hold
    prepare_generation = fixtures.CmsGoalBillingTests.prepare_generation
    consume = fixtures.CmsGoalBillingTests.consume

    def setUp(self):
        fixtures.CmsGoalBillingTests.setUp(self)
        self.connection.refresh_from_db = Mock()

    def value(self): return authority.historical(self.goal_operation)

    def original_intent(self):
        self.saved.remove(self.goal_operation)
        original_policy = self.connection.capabilities.pop(policies.REFERENCE)
        try:
            quote = quotes.for_context(self.context, self.config, user=self.user, expected_binding=self.binding)
        finally:
            self.connection.capabilities[policies.REFERENCE] = original_policy
            self.saved.append(self.goal_operation)
        self.assertIsNone(quote["policy"])
        return setup.make_intent(quote, operation_id=str(self.goal_operation.pk), request_id=str(uuid4()), binding=self.binding,
            credential_operation_id=str(self.private.pk), user_id=self.user.pk, now=self.now,
            answers={"review_policy": "auto", "publication_confirmed": True, "native_defaults_confirmed": True,
                "billing_confirmed": True, "expected_cost_points": 6})

    def test_setup_and_actual_goal_share_one_subject_key_and_original_price_after_policy_discovery(self):
        original = self.original_intent()
        before = subjects.from_setup_intent(original, expected_binding=self.binding)
        after = subjects.from_goal(self.value())
        self.assertEqual(before, after)
        self.assertEqual(request_key(before), request_key(after))
        self.assertEqual(_binding(before), _binding(after))
        self.assertTrue(request_key(before).startswith("cms-articles-first:"))
        self.assertNotIn("policy_operation_id", before["scope"])
        self.assertNotIn("milestones", before)
        with self.assertRaises(goals.CmsGoalError): goals.validate_goal(before, self.scope)
        self.assertEqual(before["kind"], subjects.KIND)
        self.spend.assert_not_called()

    def test_different_policy_references_do_not_mint_another_monetary_subject(self):
        value = self.value()
        self.prepare_generation()
        different = self.configure(client_request_id=str(uuid4()), expected_policy_id=self.policy["operation_id"])
        self.assertNotEqual(different["operation_id"], self.policy["operation_id"])
        self.assertEqual(policies.current_policy(self.context, self.config, user=self.user)["operation_id"], different["operation_id"])
        changed_scope = goals.scope(self.binding, policy_operation_id=different["operation_id"], policy_digest=different["policy"]["digest"])
        goal = goals.new_goal(str(self.goal_operation.pk), changed_scope, now=self.now)
        choice = consent.seal(goal, user_id=self.user.pk, review_policy="auto", expected_points=6, confirmed=True, now=self.now)
        changed = SimpleNamespace(goal=goal, scope=changed_scope, consent=choice)
        self.assertEqual(subjects.from_goal(value), subjects.from_goal(changed))
        # Even a real, current private policy cannot be adopted through a money
        # reference: the original authoring goal still binds its original policy.
        with self.assertRaises(WebsiteAuthorityError): self.consume()
        self.assertEqual(self.spend.call_count, 1)

    def test_an_existing_held_receipt_does_not_reprice_or_debit_during_goal_retries(self):
        first = self.hold()
        self.price.return_value = 99
        self.assertEqual(self.hold(), first)
        self.assertEqual(first["binding"], _binding(subjects.from_goal(self.value())))
        self.assertEqual(first["cost_points"], 6)
        self.assertEqual(self.spend.call_count, 1)

    def test_owner_company_connection_generation_and_original_operation_remain_financially_bound(self):
        current = subjects.from_goal(self.value())
        for key, value in (("company_id", str(uuid4())), ("connection_id", str(uuid4())),
                ("generation", self.binding["generation"] + 1)):
            changed = subjects.make_subject(operation_id=str(self.goal_operation.pk), binding={**self.binding, key: value},
                owner_user_id=self.user.pk, publish_mode="auto", expected_points=6, accepted=True)
            self.assertNotEqual(request_key(current), request_key(changed))
            self.assertNotEqual(_binding(current), _binding(changed))
        other = subjects.make_subject(operation_id=str(uuid4()), binding=self.binding, owner_user_id=self.user.pk,
            publish_mode="auto", expected_points=6, accepted=True)
        self.assertNotEqual(request_key(current), request_key(other))
        with self.assertRaises(ValueError): subjects.make_subject(operation_id=str(self.goal_operation.pk), binding=self.binding,
            owner_user_id=self.user.pk + 1, publish_mode="auto", expected_points=6, accepted=True)

    def test_changed_mode_or_price_cannot_reuse_the_original_accepted_binding(self):
        before = subjects.from_goal(self.value())
        for mode, price in (("review", 6), ("auto", 7)):
            changed = subjects.make_subject(operation_id=str(self.goal_operation.pk), binding=self.binding,
                owner_user_id=self.user.pk, publish_mode=mode, expected_points=price, accepted=True)
            self.assertEqual(request_key(changed), request_key(before))  # One operation namespace.
            self.assertNotEqual(_binding(changed), _binding(before))  # Original accepted inputs stay immutable.

    def test_zero_requires_actual_confirmation_and_malformed_values_do_not_become_a_subject(self):
        args = {"operation_id": str(self.goal_operation.pk), "binding": self.binding, "owner_user_id": self.user.pk,
            "publish_mode": "auto", "expected_points": 0, "accepted": True}
        self.assertEqual(subjects.make_subject(**args)["consent"]["answers"]["billing"]["expected_points"], 0)
        for changes in ({"accepted": False}, {"accepted": 1}, {"expected_points": True}, {"expected_points": "0"},
                {"operation_id": "not-a-uuid"}, {"publish_mode": None}, {"expected_points": -1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError): subjects.make_subject(**{**args, **changes})

    def test_raw_financial_metadata_cannot_select_a_server_dispatch_or_bypass_current_policy(self):
        self.hold()
        self.goal_operation.state = "running"
        self.goal_operation.payload["lease_id"] = self.dispatch.lease_id
        metadata = subjects.from_goal(self.value())
        with self.assertRaises(WebsiteAuthorityError): billing.for_generation(metadata, context=self.context, user=self.user)
        self.assertEqual(self.spend.call_count, 1)

    def test_projection_copies_binding_and_does_not_expose_or_mutate_policy_proofs(self):
        value = self.value()
        before = deepcopy((value.goal, value.consent, value.scope))
        subject = subjects.from_goal(value)
        subject["scope"]["binding"]["generation"] += 1
        self.assertEqual((value.goal, value.consent, value.scope), before)
        self.assertNotIn("credential_operation_id", subject)
        self.assertNotIn("policy_digest", subject["scope"])


    def test_legacy_native_holds_keep_the_original_policy_bound_key_and_binding(self):
        self.goal_operation.payload.pop("financial_subject_version")
        value = self.value()
        self.assertEqual(value.financial_subject_version, 1)
        legacy = {**value.goal, "target": "first_article_live", "consent": value.consent}
        key = request_key(legacy)
        self.assertNotEqual(key, request_key(subjects.from_goal(value)))
        self.key = key
        self.ledger.reference_id = key
        self.ledger.idempotency_key = "content_factory:charge:" + key
        held = self.hold()
        self.assertEqual(held["client_request_id"], key)
        self.assertEqual(held["binding"], _binding(legacy))
        self.prepare_generation()
        self.price.return_value = 99
        (_payer, _ledger, cost), _payload = self.consume()
        self.assertEqual(cost, 6)
        self.assertEqual(self.spend.call_count, 1)
        self.assertEqual(self.goal_operation.receipt["first_article_billing"], held)

    def test_unknown_or_coerced_financial_format_never_falls_back_to_a_different_key(self):
        for version in (None, True, "2", 0, 3):
            self.goal_operation.payload["financial_subject_version"] = version
            with self.subTest(version=version), self.assertRaises(WebsiteAuthorityError): self.hold()
        self.spend.assert_not_called()
