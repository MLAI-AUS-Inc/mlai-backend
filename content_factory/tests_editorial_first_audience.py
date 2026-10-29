"""POST editorial-catalog/first-audience/: the Write press approves one sentence.

Real ORM, real owner locks and the real mutate_catalog_response path against a
throwaway test database. No network IO is allowed.
"""
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from content_factory.editorial_catalog import (
    approve_catalog, article_brief_for_catalog, catalog_payload, review_payload, update_catalog,
)
from content_factory.editorial_contract import (
    ArticleEditorialBrief, AudienceOption, normalize_cta_options, resolve_editorial_brief,
)
from content_factory.editorial_first_audience import country_from_location
from content_factory.models import OrganizationContentConfig
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization

LEGACY_URL = "/api/v1/vibe-marketing/editorial-catalog/first-audience/"
STARTUP_URL = "/api/v1/my-startup/vibe-marketing/editorial-catalog/first-audience/"
SENTENCE = "Small cafe owners in Melbourne who want more weekday customers"
NOW = datetime(2026, 10, 29, 1, 0, tzinfo=timezone.utc)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class EditorialFirstAudienceTests(TestCase):
    def setUp(self):
        for target, options in (
            ("founder_tools.services.reconcile_user_slack_id_from_email", {}),
            ("requests.sessions.Session.request", {"side_effect": AssertionError("Unexpected external HTTP request")}),
        ):
            mock = patch(target, **options)
            mock.start()
            self.addCleanup(mock.stop)
        cache.clear()
        self.addCleanup(cache.clear)
        users = get_user_model().objects
        self.owner = users.create_user(email="first-audience-owner@example.test", role="participant")
        self.profile = VibeRaisingProfile.objects.create(user=self.owner, role=VibeRaisingProfile.ROLE_FOUNDER)
        self.organization = Organization.objects.create(name="Bean There", domain="beanthere.test")
        self.company = VibeRaisingCompany.objects.create(
            profile=self.profile, organization=self.organization, name="Bean There",
            domain="beanthere.test", location="Melbourne, Australia",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def post(self, body, url=LEGACY_URL, client=None, company=None):
        company = company or self.company
        return (client or self.client).post(f"{url}?company_id={company.pk}", body, format="json")

    def strategy(self):
        config = OrganizationContentConfig.objects.filter(organization=self.organization).first()
        return config.pillar_strategy if config else {}

    def store(self, strategy):
        OrganizationContentConfig.objects.update_or_create(
            organization=self.organization, defaults={"pillar_strategy": strategy})

    def assert_brief_admissible(self, brief):
        strategy = self.strategy()
        self.assertEqual(article_brief_for_catalog(strategy, {"editorial_brief": brief}), brief)
        current = catalog_payload(strategy)
        resolve_editorial_brief(
            ArticleEditorialBrief.model_validate(brief),
            [AudienceOption.model_validate(item) for item in current["audience_options"]],
            normalize_cta_options(current["cta_options"]),
        )

    def test_one_sentence_with_zero_profiles_is_an_owner_approved_no_offer_audience(self):
        with self.assertLogs("content_factory.editorial_first_audience", level="INFO") as logs:
            response = self.post({"description": f"  {SENTENCE} ", "expected_editorial_catalog_version": 0})

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["approval_method"], "first_article_press")
        self.assertEqual(response.data["audience_version"], 1)
        self.assertTrue(any("editorial_first_audience_approved" in line and f"user:{self.owner.pk}" in line
                            for line in logs.output))
        catalog = self.strategy()["editorial_catalog"]
        self.assertEqual(catalog["version"], 1, "one owner Save is one catalogue revision")
        [audience] = catalog["audience_options"]
        self.assertEqual(audience["id"], response.data["audience_id"])
        self.assertEqual(audience["status"], "approved")
        self.assertEqual(audience["approved_by"], f"user:{self.owner.pk}")
        self.assertTrue(audience["approved_at"])
        self.assertTrue(audience["allow_no_offer"])
        self.assertEqual(audience["description"], SENTENCE)
        self.assertEqual(audience["name"], "Small cafe owners in Melbourne who")
        self.assertEqual(audience["reader_task"], f"Understand {SENTENCE[0].lower()}{SENTENCE[1:]}")
        receipt = catalog["approval_receipts"]["audience"][audience["id"]]
        self.assertEqual(receipt["approved_by"], f"user:{self.owner.pk}")
        [history] = catalog["approval_history"]
        self.assertEqual((history["kind"], history["id"], history["approved_by"]),
                         ("audience", audience["id"], f"user:{self.owner.pk}"))

        brief = response.data["editorial_brief"]
        self.assertEqual(brief, {
            "audience_id": audience["id"], "audience_version": 1, "conversion_intent": "none",
            "offer_id": None, "offer_version": None, "no_offer_reason": "No approved call to action yet.",
            "country": "AU", "reader_task": audience["reader_task"],
            "distinct_contribution": "Practical guidance from Bean There",
            "acceptance_criteria": ["Answers the reader's question in plain English"],
        })
        self.assert_brief_admissible(brief)
        self.assertEqual(response.data["catalog"]["editorial_catalog_version"], 1)

    def test_replay_is_idempotent_and_does_not_bump_the_catalogue_again(self):
        first = self.post({"description": SENTENCE, "expected_editorial_catalog_version": 0})
        self.assertEqual(first.status_code, 201, first.data)
        before = deepcopy(self.strategy())

        replay = self.post({"description": SENTENCE, "expected_editorial_catalog_version": 0})

        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["audience_id"], first.data["audience_id"])
        self.assertEqual(replay.data["editorial_brief"], first.data["editorial_brief"])
        self.assertEqual(self.strategy(), before)

    def test_stale_catalogue_version_is_a_conflict(self):
        self.store(update_catalog({}, {"expected_editorial_catalog_version": 0, "audience_options": [
            {"id": "draft-only", "reader_task": "Learn", "status": "draft"}]}))

        response = self.post({"description": SENTENCE, "expected_editorial_catalog_version": 0})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "editorial_catalog_conflict")
        self.assertIn("error", response.data)
        self.assertEqual(len(self.strategy()["editorial_catalog"]["audience_options"]), 1)

    def test_existing_drafts_are_kept_and_the_new_id_is_unique(self):
        self.store(update_catalog({}, {"expected_editorial_catalog_version": 0, "audience_options": [
            {"id": "cafe-owners", "reader_task": "Learn", "status": "draft"}]}))

        response = self.post({"description": SENTENCE, "name": "Cafe owners", "expected_editorial_catalog_version": 1})

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["audience_id"], "cafe-owners-2")
        options = {item["id"]: item for item in catalog_payload(self.strategy())["audience_options"]}
        self.assertEqual(options["cafe-owners"]["status"], "draft")
        self.assertEqual(options["cafe-owners-2"]["status"], "approved")
        self.assertEqual(self.strategy()["editorial_catalog"]["version"], 2)
        self.assert_brief_admissible(response.data["editorial_brief"])

    def test_existing_approved_audience_is_returned_unchanged(self):
        strategy = update_catalog({}, {"expected_editorial_catalog_version": 0, "audience_options": [
            {"id": "builders", "reader_task": "Ship tested work", "allow_no_offer": True}]})
        strategy = approve_catalog(strategy, {"expected_editorial_catalog_version": 1,
                                              "entries": review_payload(strategy)["review_entries"]},
                                   actor_id="user:earlier-owner", approved_at=NOW)
        self.store(strategy)

        response = self.post({"description": SENTENCE, "expected_editorial_catalog_version": 2})

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["audience_id"], "builders")
        self.assertEqual(self.strategy(), strategy)
        self.assert_brief_admissible(response.data["editorial_brief"])

    def test_existing_offer_only_audience_uses_its_single_offer_or_no_brief(self):
        strategy = update_catalog({}, {
            "expected_editorial_catalog_version": 0,
            "audience_options": [{"id": "builders", "reader_task": "Ship tested work"}],
            "cta_options": [{"id": "studio", "title": "Apply", "body": "Apply now", "button_text": "Apply",
                             "button_href": "/apply", "audience_ids": ["builders"], "countries": ["AU"]}],
        })
        strategy = approve_catalog(strategy, {"expected_editorial_catalog_version": 1,
                                              "entries": review_payload(strategy)["review_entries"]},
                                   actor_id="user:earlier-owner", approved_at=NOW)
        self.store(strategy)

        response = self.post({"description": SENTENCE, "expected_editorial_catalog_version": 2})

        self.assertEqual(response.status_code, 200, response.data)
        brief = response.data["editorial_brief"]
        self.assertEqual((brief["conversion_intent"], brief["offer_id"], brief["offer_version"], brief["no_offer_reason"]),
                         ("offer", "studio", 1, None))
        self.assert_brief_admissible(brief)

        self.company.location = "Auckland, New Zealand"
        self.company.save(update_fields=["location"])
        response = self.post({"description": SENTENCE, "expected_editorial_catalog_version": 2})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["editorial_brief"], "no approved offer covers NZ")

    def test_non_owners_and_unknown_companies_are_refused(self):
        investor = get_user_model().objects.create_user(email="first-audience-investor@example.test", role="participant")
        VibeRaisingProfile.objects.create(user=investor, role=VibeRaisingProfile.ROLE_INVESTOR)
        client = APIClient()
        client.force_authenticate(user=investor)
        response = self.post({"description": SENTENCE, "expected_editorial_catalog_version": 0}, client=client)
        self.assertEqual(response.status_code, 403)
        self.assertIn("error", response.data)

        stranger = get_user_model().objects.create_user(email="first-audience-stranger@example.test", role="participant")
        VibeRaisingProfile.objects.create(user=stranger, role=VibeRaisingProfile.ROLE_FOUNDER)
        client.force_authenticate(user=stranger)
        response = self.post({"description": SENTENCE, "expected_editorial_catalog_version": 0}, client=client)
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.data)
        self.assertEqual(self.strategy(), {})

    def test_invalid_input_is_refused_without_a_save(self):
        for body in (
            {"description": "hi", "expected_editorial_catalog_version": 0},
            {"description": "x" * 281, "expected_editorial_catalog_version": 0},
            {"description": SENTENCE, "expected_editorial_catalog_version": "0"},
            {"description": SENTENCE, "name": "n" * 81, "expected_editorial_catalog_version": 0},
        ):
            with self.subTest(body=body):
                response = self.post(body)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.data)
        self.assertEqual(self.strategy(), {})

    def test_served_to_chat_through_the_my_startup_alias(self):
        response = self.post({"description": SENTENCE, "expected_editorial_catalog_version": 0}, url=STARTUP_URL)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(self.strategy()["editorial_catalog"]["audience_options"][0]["status"], "approved")


class CountryFromLocationTests(TestCase):
    def test_free_text_locations_map_to_markets(self):
        for location, expected in (
            ("", "AU"), ("Melbourne, Australia", "AU"), ("Sydney NSW", "AU"), ("Newcastle, New South Wales", "AU"),
            ("Auckland, New Zealand", "NZ"), ("London, UK", "GB"), ("uk", "GB"), ("US", "US"), ("SA", "AU"),
            ("Made by us in Brisbane", "AU"), ("Somewhere unknown", "AU"), ("Singapore", "SG"),
            # A country or province named anywhere beats a city shared with Australia.
            ("Victoria, BC, Canada", "CA"), ("Victoria, BC", "CA"), ("Perth, Scotland", "GB"),
            ("Sydney, Nova Scotia", "CA"), ("Melbourne, Victoria", "AU"), ("Cardiff, Wales", "GB"),
            ("Belfast, Northern Ireland", "GB"), ("Wellington NSW", "AU"),
        ):
            with self.subTest(location=location):
                self.assertEqual(country_from_location(location), expected)
