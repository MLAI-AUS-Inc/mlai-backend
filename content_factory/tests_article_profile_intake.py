"""The founder can fill the sparse facts before separately approving an article."""
from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import resolve
from rest_framework.test import APIClient
from roo.models import Ledger
from startup_updates.models import StartupProfile

from community_chat import my_startup_journey_views as views
from .article_profile_intake import KEY
from .editorial_catalog import review_payload
from .founder_journey_contract import _schema, _validate
from .tests_content_first_article import CardFixture


class ArticleProfileIntakeTests(CardFixture, TestCase):
    one_liner = ""
    facts = {"oneLiner": "We help teams document decisions.", "targetAudience": "Small software teams",
             "readerTask": "Try writing a one-page decision record"}

    def setUp(self):
        super().setUp()
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        StartupProfile.objects.get_or_create(organization=self.org)
        self.url = "/api/v1/my-startup/first-article"
        self.params = f"company_id={self.company.pk}"

    def hint(self):
        return self.api.get(f"{self.url}/?{self.params}&profile_intake=1").data

    def save(self, payload):
        return self.api.post(f"{self.url}/profile-intake/?{self.params}", payload, format="json")

    def test_full_negotiated_flow_preserves_source_and_actual_admitted_reader_goal(self):
        for suffix in ("", "/"):
            self.assertIs(resolve(self.url + "/profile-intake" + suffix).func.view_class, views.FirstArticleProfileIntakeView)
        old = self.api.get(f"{self.url}/?{self.params}").data
        self.assertNotIn("profileIntake", old)
        hint = self.hint()
        self.assertEqual(hint["profileIntake"]["missingFields"], ["oneLiner", "targetAudience", "readerTask"])
        definitions = _schema()["$defs"]
        _validate(hint, definitions["FirstArticleCard"], definitions)
        self.site_profile(primaryCallToAction={"label": "Buy our plan", "url": f"https://{self.org.domain}/buy"})
        before = Ledger.objects.count()
        first = self.save({"readerTask": self.facts["readerTask"]})
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data["profileIntake"]["missingFields"], ["oneLiner", "targetAudience"])
        second = self.save({key: self.facts[key] for key in ("oneLiner", "targetAudience")})
        self.assertEqual(second.status_code, 200, second.data)
        self.assertNotIn("profileIntake", second.data)
        self.assertEqual(second.data["audience"]["status"], "draft")
        self.assertIsNone(second.data["callToAction"])
        provenance = deepcopy(self.strategy()[KEY])
        for key, value in self.facts.items():
            answer = provenance["answers"][key]
            self.assertEqual((answer["value"], answer["source"], answer["userId"]), (value, "founder", str(self.owner.pk)))
        saved = StartupProfile.objects.get(organization=self.org)
        self.assertEqual((saved.short_description, saved.target_audience), (self.facts["oneLiner"], self.facts["targetAudience"]))
        self.assertEqual(self.save(self.facts).status_code, 200)
        self.assertEqual(self.strategy()[KEY], provenance)
        self.assertFalse(self.runs().exists())
        self.assertEqual(Ledger.objects.count(), before)
        self.assertEqual(self.save({"targetAudience": "Different readers"}).status_code, 409)
        self.assertEqual(self.strategy()[KEY], provenance)
        result = self.press()
        self.assertIn(result.status_code, (200, 201), result.data)
        brief = self.runs().get().run_request["editorial_brief"]
        self.assertEqual((brief["reader_task"], brief["conversion_intent"], brief["offer_id"]),
                         (self.facts["readerTask"], "none", None))
        self.assertFalse(review_payload(self.strategy())["cta_options"])

    def test_existing_fact_and_approved_offer_survive_intake(self):
        profile = StartupProfile.objects.get(organization=self.org)
        profile.short_description = self.facts["oneLiner"]
        profile.target_audience = self.facts["targetAudience"]
        profile.save()
        self.site_profile(primaryCallToAction={"label": "Book a demo", "url": f"https://{self.org.domain}/demo"})
        started = self.press()
        self.assertIn(started.status_code, (200, 201), started.data)
        catalog = deepcopy(self.strategy()["editorial_catalog"])
        self.assertTrue(catalog["cta_options"])
        profile.short_description = ""
        profile.save(update_fields=["short_description"])
        self.assertEqual(self.hint()["profileIntake"]["missingFields"], ["oneLiner"])
        self.assertEqual(self.save({"oneLiner": "A founder supplied description"}).status_code, 200)
        self.assertEqual(self.strategy()["editorial_catalog"], catalog)
        self.assertEqual(self.save({"readerTask": "Approve a new commercial offer"}).status_code, 409)

    def test_unauthorized_or_invalid_inputs_create_no_facts(self):
        with patch("community_chat.my_startup_views.user_may_use_organization", return_value=True), \
                patch("founder_tools.services.organization_owner_user_id", return_value=self.owner.pk + 1000):
            self.assertNotIn("profileIntake", self.hint())
            self.assertEqual(self.save(self.facts).status_code, 403)
        for payload in ({}, {"readerTask": "a"}, {"readerTask": "x" * 281}, {"offerId": "attack"}):
            self.assertEqual(self.save(payload).status_code, 400)
        self.store(editorial_catalog={"version": "unreadable"})
        self.assertNotIn("profileIntake", self.hint())
        self.assertEqual(self.save(self.facts).status_code, 409)
        self.assertNotIn(KEY, self.strategy())
        with override_settings(ARTICLES_FIRST_ARTICLE_CARD_ENABLED=False):
            self.assertEqual(self.save(self.facts).status_code, 404)
