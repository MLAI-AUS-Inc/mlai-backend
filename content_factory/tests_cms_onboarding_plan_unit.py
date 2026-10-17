"""Initial native preparation preserves its original financial and time identity."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import cms_onboarding_plan as plans
from . import cms_first_article_subject as subjects
from . import tests_cms_onboarding_offer_unit as fixtures
from .channel_artifact_contract import digest
from .website_contract import WebsiteAuthorityError


@override_settings(**fixtures.CmsOnboardingOfferTests._overridden_settings)
class CmsOnboardingPlanTests(SimpleTestCase):
    def setUp(self):
        fixtures.CmsOnboardingOfferTests.setUp(self)
        self.offer = fixtures.CmsOnboardingOfferTests.read(self)
        self.binding = {"schema_version": 1, "channel": "cms", "company_id": self.offer["scope"]["company_id"],
            "organization_id": "7", "owner_user_id": "1", "connection_id": str(uuid4()), "generation": 1,
            **{key: self.offer[key] for key in ("platform", "public_origin", "api_origin")}}
        self.data = {"binding": self.binding, "operation_id": str(uuid4()), "root_operation_id": str(uuid4()),
            "credential_operation_id": str(uuid4()), "request_id": str(uuid4()), "user_id": self.user.pk,
            "answers": {"review_policy": "auto", "expected_cost_points": 6,
                "publication_confirmed": True, "native_defaults_confirmed": True, "billing_confirmed": True}, "now": self.now}

    def prepare(self, **changes): return plans.prepare(self.offer, **{**self.data, **changes})

    def test_initial_plan_uses_the_same_native_root_key_choices_price_and_original_acceptance(self):
        value = self.prepare()
        self.assertEqual(plans.validate(value), value)
        intent = value["native_intent"]
        self.assertEqual((intent["operation_id"], intent["request_id"], intent["credential_operation_id"], intent["accepted_at"]),
            (self.data["root_operation_id"], self.data["request_id"], self.data["credential_operation_id"], value["accepted_at"]))
        self.assertEqual(intent["answers"], self.data["answers"])
        subject = subjects.from_setup_intent(intent, expected_binding=self.binding)
        self.assertEqual((subject["goal_id"], subject["target"], subject["consent"]["answers"]["billing"]),
            (self.data["root_operation_id"], "first_article_live", {"expected_points": 6, "accepted": True}))
        self.assertIsNone(intent["quote"]["policy"])
        self.assertFalse({"credential", "identity", "lease_id", "milestones", "source", "spend_ledger_id"} & set(value))

    def test_review_and_free_choices_remain_explicit_and_never_reprice(self):
        self.offer["first_article_points"] = 0
        self.offer["digest"] = digest({key: item for key, item in self.offer.items() if key != "digest"})
        value = self.prepare(answers={**self.data["answers"], "review_policy": "review", "expected_cost_points": 0})
        subject = subjects.from_setup_intent(value["native_intent"], expected_binding=self.binding)
        self.assertEqual(subject["consent"]["answers"], {"publish_mode": "review", "billing": {"expected_points": 0, "accepted": True}})

    def test_expired_display_offer_cannot_become_a_fresh_acceptance(self):
        with self.assertRaises(WebsiteAuthorityError): self.prepare(now=self.now + timedelta(minutes=15))
        value = self.prepare()
        value["accepted_at"] = (self.now + timedelta(minutes=15)).isoformat()
        value["digest"] = digest({key: item for key, item in value.items() if key != "digest"})
        with self.assertRaises(WebsiteAuthorityError): plans.validate(value)

    def test_new_owner_site_scope_or_aliased_resource_ids_cannot_prepare_the_original_intent(self):
        for changes in ({"binding": {**self.binding, "company_id": str(uuid4())}}, {"binding": {**self.binding, "organization_id": "8"}},
                {"binding": {**self.binding, "api_origin": "https://foreign.example.com/"}}, {"binding": {**self.binding, "generation": 2}},
                {"user_id": 2}, {"operation_id": self.data["root_operation_id"]}, {"credential_operation_id": self.binding["connection_id"]}):
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): self.prepare(**changes)

    def test_coerced_extra_or_unconfirmed_choice_is_not_native_consent(self):
        for changes in ({"expected_cost_points": True}, {"expected_cost_points": 0}, {"billing_confirmed": 1},
                {"publication_confirmed": False}, {"review_policy": "approved"}, {"lease_id": str(uuid4())}):
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): self.prepare(answers={**self.data["answers"], **changes})

    def test_closed_historical_plan_cannot_extend_time_or_replace_the_native_subject(self):
        original = self.prepare()
        for key, change in (("version", True), ("expires_at", (self.now + timedelta(days=2)).isoformat()),
                ("native_intent", {**original["native_intent"], "operation_id": str(uuid4())}), ("lease_id", str(uuid4()))):
            value = deepcopy(original); value[key] = change
            value["digest"] = digest({key: item for key, item in value.items() if key != "digest"})
            with self.subTest(key=key), self.assertRaises(WebsiteAuthorityError): plans.validate(value)
