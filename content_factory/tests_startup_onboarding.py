"""New startup onboarding v3: the confirm_profile ask, the onboarding block and confirmProfile.

The block and the ask reach only clients that send ``?onboarding=3`` while
``MY_STARTUP_ONBOARDING_V3_ENABLED`` is on; every other journey body is
unchanged.
"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from founder_tools.models import VibeRaisingCompany
from startup_updates.models import StartupProfile

from . import founder_journey as journeys
from . import startup_first_profile as first_profile
from . import startup_onboarding as onboarding
from .founder_journey_contract import validate_journey
from .founder_journey_testing import make_named_legacy_org
from .tests_founder_journey_unit import FLAGS, JourneyFixture, make_run

LAUNCH = "2026-01-01T00:00:00+00:00"
ON = {**FLAGS, "MY_STARTUP_ONBOARDING_V3_ENABLED": True, "MY_STARTUP_ONBOARDING_V3_LAUNCHED_AT": LAUNCH}
COMPANIES_URL = "/api/v1/founder-tools/companies/"


class OnboardingJourneyTests(JourneyFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(**ON))

    def set_state(self, **values):
        def mutate(state):
            state.update(values)
            return state
        first_profile._update_state(self.org, mutate)
        self.built.config.refresh_from_db()

    def test_a_new_startup_is_asked_to_check_its_details(self):
        body = self.journey(onboarding=True)
        self.assertStep(body, "website", "your_turn", ask="confirm_profile")
        self.assertEqual((body["headline"], body["body"], body["action"]["label"]),
                         ("Confirm your startup's name and what you do",
                          "We've filled in what we could from your website. It takes about a minute.",
                          "Check details"))
        self.assertEqual(body["action"]["url"], f"/my-startup/start?company_id={self.company.pk}")
        self.assertEqual(body["onboarding"]["profile"], {"confirmed": False, "read": "none"})
        validate_journey(body)

    def test_without_the_parameter_or_the_setting_the_body_is_unchanged(self):
        plain = self.journey()
        self.assertNotIn("onboarding", plain)
        self.assertNotEqual((plain["action"] or {}).get("id"), "confirm_profile")
        with override_settings(MY_STARTUP_ONBOARDING_V3_ENABLED=False):
            off = self.journey(onboarding=True)
        self.assertEqual(off, plain)

    def test_confirmed_startup_is_not_asked_again_and_never_asked_its_customer(self):
        onboarding.confirm(self.org)
        self.built.config.refresh_from_db()
        body = self.journey(onboarding=True)
        self.assertNotEqual((body["action"] or {}).get("id"), "confirm_profile")
        self.assertNotEqual((body["action"] or {}).get("id"), "describe_customer")
        self.assertTrue(body["onboarding"]["profile"]["confirmed"])

    def test_an_old_startup_with_a_one_liner_is_left_alone(self):
        type(self.org).objects.filter(pk=self.org.pk).update(created_at=timezone.now() - timedelta(days=400))
        self.org.refresh_from_db()
        body = self.journey(onboarding=True)
        self.assertNotEqual((body["action"] or {}).get("id"), "confirm_profile")

    def test_an_old_startup_without_a_one_liner_is_asked(self):
        type(self.org).objects.filter(pk=self.org.pk).update(created_at=timezone.now() - timedelta(days=400))
        self.org.refresh_from_db()
        self.describe("")
        self.assertStep(self.journey(onboarding=True), "website", "your_turn", ask="confirm_profile")

    def test_a_started_article_setup_or_connection_skips_the_ask(self):
        for label, make in (("article run", lambda: make_run(self.org)), ("setup run", self.setup_run),
                            ("connection", self.connection)):
            with self.subTest(label):
                made = make()
                body = self.journey(onboarding=True)
                self.assertNotEqual((body["action"] or {}).get("id"), "confirm_profile")
                made.delete()

    def test_terms_come_first(self):
        with patch.object(journeys, "terms_out_of_date", return_value=True):
            body = self.journey(onboarding=True)
        self.assertEqual(body["action"]["id"], "agree_terms")
        self.assertFalse(body["onboarding"]["profile"]["confirmed"])

    def test_a_member_is_never_asked(self):
        member = get_user_model().objects.create_user(email="member@onboarding.example.test")
        body = journeys.project_founder_journey(self.org, user=member, company=self.company, onboarding=True)
        self.assertNotEqual((body["action"] or {}).get("id"), "confirm_profile")
        self.assertTrue(body["onboarding"]["profile"]["confirmed"])

    def test_a_legacy_startup_is_unchanged(self):
        legacy = make_named_legacy_org("pawshare", domain="pawshare-onboarding.example.test")
        body = journeys.project_founder_journey(legacy.organization, user=legacy.owner, company=legacy.company,
                                                onboarding=True)
        self.assertNotIn("onboarding", body)

    def test_block_reports_the_read_the_site_builder_and_researched_terms(self):
        self.set_state(runId="read-1")
        self.assertEqual(self.journey(onboarding=True)["onboarding"]["profile"]["read"], "running")
        self.set_state(appliedRunId="read-1", siteProfile={"platform": "wix"}, seedKeywordsSource="fallback")
        self.org.seed_keywords = ["physio booking", "clinic software", "physio app", "rehab", "fifth"]
        self.org.save(update_fields=["seed_keywords"])
        block = self.journey(onboarding=True)["onboarding"]
        self.assertEqual(block["profile"]["read"], "done")
        self.assertEqual(block["website"], {"githubLikely": "unlikely", "platformLabel": "Wix"})
        self.assertEqual(block["searchTerms"], [])
        self.assertEqual(block["articleCost"], 12)
        self.assertEqual(block["balance"], 0)
        self.set_state(seedKeywordsSource="research", siteProfile={"platform": "nextjs"})
        block = self.journey(onboarding=True)["onboarding"]
        self.assertEqual(block["searchTerms"], ["physio booking", "clinic software", "physio app", "rehab"])
        self.assertEqual(block["website"], {"githubLikely": "likely", "platformLabel": None})

    def test_ideas_come_from_the_free_topic_batch(self):
        self.pool.return_value = [{"id": "t1", "title": "How physios cut no-shows", "opportunityScore": 5},
                                  {"id": "t2", "title": "Booking apps compared", "opportunityScore": 3}]
        self.assertEqual(self.journey(onboarding=True)["onboarding"]["ideas"], [])
        self.set_state(topicBatchRunId="topics-1")
        self.assertEqual(self.journey(onboarding=True)["onboarding"]["ideas"],
                         ["How physios cut no-shows", "Booking apps compared"])

    def test_confirm_stops_a_late_read_overwriting_the_name_or_one_liner(self):
        self.set_state(derivedName=self.company.name, fallbackValues={"shortDescription": self.one_liner})
        onboarding.confirm(self.org)
        state = first_profile.read_state(self.org)
        self.assertTrue(state["confirmedAt"])
        self.assertIsNone(state["derivedName"])
        self.assertEqual(state["fallbackValues"], {})
        self.assertFalse(first_profile._is_open(self.one_liner, "shortDescription", state))


@override_settings(**ON)
class ConfirmProfileSaveTests(JourneyFixture, TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.enterContext(patch("founder_tools.services.reconcile_user_slack_id_from_email"))
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def save(self, **body):
        return self.client.post(COMPANIES_URL, {"companyId": str(self.company.pk), "name": self.company.name,
                                                "confirmProfile": True, **body}, format="json")

    def test_owner_confirms(self):
        response = self.save(shortDescription="Booking software for physio clinics")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(first_profile.read_state(self.org).get("confirmedAt"))

    def test_an_empty_description_is_refused_and_nothing_is_saved(self):
        StartupProfile.objects.filter(organization=self.org).update(short_description="")
        response = self.save(name="Renamed", shortDescription="")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["reason"], "description_required")
        self.assertFalse(first_profile.read_state(self.org).get("confirmedAt"))
        self.assertNotEqual(VibeRaisingCompany.objects.get(pk=self.company.pk).name, "Renamed")

    def test_setting_off_ignores_the_key(self):
        with override_settings(MY_STARTUP_ONBOARDING_V3_ENABLED=False):
            response = self.save()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(first_profile.read_state(self.org).get("confirmedAt"))
