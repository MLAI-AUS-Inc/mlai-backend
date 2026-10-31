"""P1.5 and P1.6 (IF-P1-06): the one-field company create.

The new keys (Idempotency-Key, firstProfile, an omitted name, timezone, a
create without a website) are honoured only while MY_STARTUP_ONE_FIELD_START_ENABLED
is on and a startup added now joins the new journey. Otherwise the legacy
create runs unchanged.
"""
import hashlib
import threading
import time
from datetime import timedelta
from unittest import skipIf
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import close_old_connections, connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from content_factory.founder_journey_cohort import FIRST_PROFILE_KEY, MARKER_KEY, read_marker
from content_factory.founder_journey_testing import ALL_P1_FLAGS_ON
from content_factory.models import OrganizationContentConfig
from content_factory.startup_first_profile import derive_company_name
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from founder_tools import one_field_start
from founder_tools.one_field_start import acquire_create_lock, normalize_website
from founder_tools.profile_fields import profile_details
from founder_tools.services import normalize_company_linkedin_url
from startup_updates.models import StartupProfile

COMPANIES_URL = "/api/v1/founder-tools/companies/"
FLAGS_ON = ALL_P1_FLAGS_ON
WITHOUT_ONE_FIELD = {**ALL_P1_FLAGS_ON, "MY_STARTUP_ONE_FIELD_START_ENABLED": False}
WITHOUT_V2 = {**ALL_P1_FLAGS_ON, "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": False}
NOT_LAUNCHED = {**ALL_P1_FLAGS_ON, "MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT": ""}
LEGACY_SETTINGS = (("one-field flag off", WITHOUT_ONE_FIELD), ("V2 off", WITHOUT_V2),
                   ("new startups legacy", NOT_LAUNCHED))
NO_WEBSITE = {"createNew": True, "firstProfile": True, "name": "Ledger Mates",
              "shortDescription": "Bookkeeping for tradies"}


def _user(email="one-field@example.test", **extra):
    return get_user_model().objects.create_user(email=email, first_name="Ada", last_name="Lovelace",
                                                role="participant", **extra)


class _Base:
    def setUp(self):
        patcher = patch("founder_tools.services.reconcile_user_slack_id_from_email")
        patcher.start()
        self.addCleanup(patcher.stop)
        cache.clear()  # create locks are keyed by user id, which tests reuse
        self.addCleanup(cache.clear)
        self.user = _user()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def post(self, body, key=None, client=None):
        headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        return (client or self.client).post(COMPANIES_URL, body, format="json", **headers)


@override_settings(**FLAGS_ON)
class OneFieldCreateTests(_Base, TestCase):
    def test_one_key_creates_one_company_then_replays_it(self):
        first = self.post({"createNew": True, "firstProfile": True, "domain": "banksia.studio",
                           "timezone": "Australia/Melbourne"}, key="press-1")
        self.assertEqual(first.status_code, 201, first.data)
        replay = self.post({"createNew": True, "firstProfile": True, "domain": "banksia.studio",
                            "timezone": "Australia/Melbourne"}, key="press-1")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data, first.data)
        self.assertEqual(VibeRaisingCompany.objects.count(), 1)
        self.assertEqual(first.data["founderJourney"], {"version": 1, "cohort": "new_journey"})
        company = VibeRaisingCompany.objects.select_related("organization").get()
        marker = read_marker(company.organization)
        self.assertEqual(marker["assignedBy"], "create")
        self.assertEqual(marker["createKeySha256"], hashlib.sha256(b"press-1").hexdigest())
        self.assertEqual(marker["createdCompanyId"], str(company.pk))

    def test_a_different_key_creates_a_second_company(self):
        self.assertEqual(self.post(NO_WEBSITE, key="press-1").status_code, 201)
        second = self.post(NO_WEBSITE, key="press-2")
        self.assertEqual(second.status_code, 201, second.data)
        self.assertEqual(VibeRaisingCompany.objects.count(), 2)

    def test_a_replay_after_24_hours_is_a_new_create(self):
        first = self.post(NO_WEBSITE, key="press-1")
        company = VibeRaisingCompany.objects.get(pk=first.data["id"])
        config = OrganizationContentConfig.objects.get(organization_id=company.organization_id)
        strategy = dict(config.pillar_strategy)
        strategy[MARKER_KEY] = {**strategy[MARKER_KEY],
                                "assignedAt": (timezone.now() - timedelta(hours=25)).isoformat()}
        config.pillar_strategy = strategy
        config.save()
        again = self.post(NO_WEBSITE, key="press-1")
        self.assertEqual(again.status_code, 201, again.data)
        self.assertNotEqual(again.data["id"], first.data["id"])
        self.assertEqual(VibeRaisingCompany.objects.count(), 2)

    def test_name_is_derived_from_the_website_and_kept_for_research(self):
        response = self.post({"createNew": True, "firstProfile": True,
                              "domain": "HTTPS://www.Banksia-Studio.com.au/about?ref=ad"}, key="k")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["name"], "Banksia Studio")
        self.assertEqual(response.data["domain"], "banksia-studio.com.au")
        company = VibeRaisingCompany.objects.select_related("organization").get()
        config = OrganizationContentConfig.objects.get(organization=company.organization)
        self.assertEqual(config.pillar_strategy[FIRST_PROFILE_KEY]["derivedName"], "Banksia Studio")

    def test_a_typed_name_is_not_marked_as_derived(self):
        response = self.post({"createNew": True, "firstProfile": True, "domain": "banksia.studio",
                              "name": "Banksia Gardens"}, key="k")
        self.assertEqual(response.data["name"], "Banksia Gardens")
        company = VibeRaisingCompany.objects.get()
        config = OrganizationContentConfig.objects.get(organization_id=company.organization_id)
        self.assertIsNone(config.pillar_strategy[FIRST_PROFILE_KEY]["derivedName"])

    def test_timezone_is_stored_when_valid_and_ignored_otherwise(self):
        self.post({"createNew": True, "firstProfile": True, "domain": "valid-zone.test",
                   "timezone": "Australia/Perth"}, key="a")
        bad = self.post({"createNew": True, "firstProfile": True, "domain": "bad-zone.test",
                         "timezone": "Mars/Olympus_Mons"}, key="b")
        self.assertEqual(bad.status_code, 201, bad.data)
        zones = {config.organization.domain: config.default_timezone
                 for config in OrganizationContentConfig.objects.select_related("organization")}
        self.assertEqual(zones["valid-zone.test"], "Australia/Perth")
        self.assertEqual(zones["bad-zone.test"], "")

    def test_no_website_needs_a_one_sentence_description(self):
        missing = self.post({"createNew": True, "firstProfile": True, "name": "Ledger Mates"}, key="k")
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.data, {"detail": "Tell us what your startup does in one sentence.",
                                        "reason": "description_required"})
        self.assertEqual(VibeRaisingCompany.objects.count(), 0)
        too_short = self.post({**NO_WEBSITE, "shortDescription": "ok"}, key="k2")
        self.assertEqual(too_short.status_code, 400)

    def test_no_website_makes_the_invalid_placeholder_org(self):
        response = self.post(NO_WEBSITE, key="k")
        self.assertEqual(response.status_code, 201, response.data)
        company = VibeRaisingCompany.objects.select_related("organization").get()
        self.assertEqual(company.organization.domain, f"startup-{company.pk}.invalid")
        self.assertEqual(StartupProfile.objects.get(organization=company.organization).short_description,
                         "Bookkeeping for tradies")
        self.assertEqual(response.data["founderJourney"]["cohort"], "new_journey")
        # It never gets an autofill run, so its keywords are seeded at create.
        self.assertTrue(company.organization.seed_keywords)
        self.assertIn("ledger mates", company.organization.seed_keywords)

    def test_without_the_no_website_flag_a_name_only_create_is_todays(self):
        with override_settings(MY_STARTUP_NO_WEBSITE_ARTICLES_ENABLED=False):
            response = self.post({"createNew": True, "firstProfile": True, "name": "Ledger Mates"}, key="k")
        self.assertEqual(response.status_code, 201, response.data)
        company = VibeRaisingCompany.objects.select_related("organization").get()
        self.assertEqual(company.organization.domain, f"startup-{company.pk}.invalid")

    def test_a_create_bound_to_an_older_organisation_still_replays(self):
        # The org predates launch, so it stays legacy and carries no create
        # marker: the key is remembered for the replay instead.
        from founder_tools.services import PREVIOUS_OWNER_KEY
        from organizations.models import Organization
        older = Organization.objects.create(name="Older", domain="older.test")
        Organization.objects.filter(pk=older.pk).update(created_at=timezone.now() - timedelta(days=30))
        # The founder deleted their startup here before launch and takes it back.
        OrganizationContentConfig.objects.create(
            organization=older, pillar_strategy={PREVIOUS_OWNER_KEY: {"user_id": self.user.pk}})
        body = {"createNew": True, "firstProfile": True, "domain": "older.test"}
        first = self.post(body, key="press-1")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(first.data["founderJourney"]["cohort"], "legacy")
        self.assertEqual(VibeRaisingCompany.objects.get().organization_id, older.pk)
        replay = self.post(body, key="press-1")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data, first.data)
        self.assertEqual(VibeRaisingCompany.objects.count(), 1)

    def test_founder_role_and_founder_one_are_seeded(self):
        VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_INVESTOR)
        response = self.post({"createNew": True, "firstProfile": True, "domain": "seeded.test"}, key="k")
        self.assertEqual(response.status_code, 201, response.data)
        profile = VibeRaisingProfile.objects.get(user=self.user)
        self.assertEqual(profile.role, VibeRaisingProfile.ROLE_FOUNDER)
        company = VibeRaisingCompany.objects.select_related("organization").get()
        self.assertEqual(profile.active_company_id, company.pk)
        self.assertEqual(profile_details(company.organization)["founderProfiles"],
                         [{"name": "Ada Lovelace", "linkedinUrl": ""}])
        config = OrganizationContentConfig.objects.get(organization=company.organization)
        self.assertEqual(config.default_author_id, f"member-{self.user.pk}")

    def test_linkedin_is_normalised_and_an_unusable_value_is_dropped(self):
        saved = self.post({"createNew": True, "firstProfile": True, "domain": "linked.test",
                           "companyLinkedInUrl": "linkedin.com/company/x"}, key="a")
        self.assertEqual(saved.status_code, 201, saved.data)
        self.assertEqual(saved.data["companyLinkedInUrl"], "https://www.linkedin.com/company/x")
        dropped = self.post({"createNew": True, "firstProfile": True, "domain": "unlinked.test",
                             "companyLinkedInUrl": "not a linkedin page"}, key="b")
        self.assertEqual(dropped.status_code, 201, dropped.data)
        self.assertEqual(dropped.data["companyLinkedInUrl"], "")

    def test_the_domain_claimed_409_is_unchanged(self):
        other = APIClient()
        other.force_authenticate(user=_user("owner@example.test"))
        self.assertEqual(other.post(COMPANIES_URL, {"name": "Claimed", "domain": "claimed.test"},
                                    format="json").status_code, 200)
        with patch("founder_tools.views.queue_domain_claim_alert") as alert:
            response = self.post({"createNew": True, "firstProfile": True, "domain": "claimed.test"}, key="k")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "company_domain_claimed")
        self.assertTrue(response.data["canContinueWithoutDomain"])
        alert.assert_called_once()
        self.assertEqual(VibeRaisingCompany.objects.filter(profile__user=self.user).count(), 0)

    def test_without_a_key_each_press_is_a_create(self):
        self.assertEqual(self.post(NO_WEBSITE).status_code, 201)
        self.assertEqual(self.post(NO_WEBSITE).status_code, 201)
        self.assertEqual(VibeRaisingCompany.objects.count(), 2)


class LegacyCreateTests(_Base, TestCase):
    """The new keys are ignored, and the response equals today's, unless the one-field path is on."""

    def test_the_legacy_create_is_unchanged_for_every_off_switch(self):
        with override_settings(**{name: False for name in FLAGS_ON if name.endswith("_ENABLED")}):
            baseline = self.post({"name": "Baseline", "domain": "baseline.test", "createNew": True})
        self.assertEqual(baseline.status_code, 200)
        for label, flags in LEGACY_SETTINGS:
            with self.subTest(label), override_settings(**flags):
                domain = f"{label.replace(' ', '-')}.test"
                response = self.post({"name": "Legacy", "domain": domain, "createNew": True, "firstProfile": True,
                                      "timezone": "Mars/Olympus_Mons"}, key=f"key-{label}")
                self.assertEqual(response.status_code, 200, response.data)
                self.assertNotIn("founderJourney", response.data)
                self.assertEqual(set(response.data), set(baseline.data))
                company = VibeRaisingCompany.objects.select_related("organization").get(pk=response.data["id"])
                config = OrganizationContentConfig.objects.get(organization=company.organization)
                self.assertNotIn(FIRST_PROFILE_KEY, config.pillar_strategy or {})
                marker = read_marker(company.organization)
                self.assertTrue(marker is None or marker.get("createKeySha256") is None)
                # The key is ignored: a second press is today's duplicate 409.
                duplicate = self.post({"name": "Legacy", "domain": domain, "createNew": True,
                                       "firstProfile": True}, key=f"key-{label}")
                self.assertEqual(duplicate.status_code, 409)
                self.assertEqual(duplicate.data["code"], "duplicate_company_domain")
                # No website and no name is still today's serializer error, not the new one.
                no_name = self.post({"createNew": True, "firstProfile": True}, key=f"other-{label}")
                self.assertEqual(no_name.status_code, 400)
                self.assertIn("name", no_name.data)

    @override_settings(**FLAGS_ON)
    def test_without_first_profile_the_create_is_unchanged(self):
        response = self.post({"name": "Plain", "domain": "plain.test", "createNew": True}, key="k")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("founderJourney", response.data)

    def test_legacy_linkedin_validation_still_accepts_a_bare_company_url(self):
        self.assertEqual(normalize_company_linkedin_url("linkedin.com/company/x"),
                         "https://www.linkedin.com/company/x")
        response = self.post({"name": "Linked", "domain": "linked-legacy.test",
                              "companyLinkedInUrl": "linkedin.com/company/x"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["companyLinkedInUrl"], "https://www.linkedin.com/company/x")


class NameAndWebsiteTests(TestCase):
    def test_derive_company_name(self):
        for domain, expected in (
            ("banksia.studio", "Banksia"),
            ("banksia-studio.com.au", "Banksia Studio"),
            ("www.ledger_mates.co.nz", "Ledger Mates"),
            ("app.banksia.io", "Banksia"),
            ("https://banksia.studio/about", "Banksia"),
            ("", ""),
        ):
            with self.subTest(domain):
                self.assertEqual(derive_company_name(domain), expected)

    def test_normalize_website(self):
        for raw, expected in (
            ("HTTPS://www.Banksia.studio/about?x=1#top", "banksia.studio"),
            ("banksia.studio:443", "banksia.studio"),
            ("  banksia.studio.  ", "banksia.studio"),
            ("", ""),
        ):
            with self.subTest(raw):
                self.assertEqual(normalize_website(raw), expected)


def _sha(key):
    return hashlib.sha256(key.encode()).hexdigest()


@override_settings(**FLAGS_ON)
class CreateLockTests(_Base, TestCase):
    """The cross-worker lock, without threads: another request holds the key."""

    BODY = {"createNew": True, "firstProfile": True, "domain": "locked.test"}

    def test_a_press_while_the_first_is_saving_replays_it_once_saved(self):
        first = self.post(self.BODY, key="press-1")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertTrue(acquire_create_lock(self.user.pk, _sha("press-1")))
        replay = self.post(self.BODY, key="press-1")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], first.data["id"])
        self.assertEqual(VibeRaisingCompany.objects.count(), 1)

    def test_a_press_that_outwaits_the_first_gets_in_progress(self):
        self.assertTrue(acquire_create_lock(self.user.pk, _sha("press-2")))
        with patch.object(one_field_start, "REPLAY_WAIT_SECONDS", 0.05), \
                patch.object(one_field_start, "REPLAY_POLL_SECONDS", 0.01):
            response = self.post(self.BODY, key="press-2")
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data, one_field_start.CREATE_IN_PROGRESS)
        self.assertEqual(VibeRaisingCompany.objects.count(), 0)

    def test_a_press_after_the_first_gave_up_creates(self):
        key_sha = _sha("press-3")
        self.assertTrue(acquire_create_lock(self.user.pk, key_sha))
        with patch.object(one_field_start.time, "sleep",
                          side_effect=lambda _seconds: one_field_start.release_create_lock(self.user.pk, key_sha)):
            response = self.post(self.BODY, key="press-3")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(VibeRaisingCompany.objects.count(), 1)


@skipIf(connection.vendor == "sqlite",
        "SQLite's shared-cache test database raises 'table is locked' across threads")
@override_settings(**FLAGS_ON)
class ConcurrentCreateTests(_Base, TransactionTestCase):
    def test_two_concurrent_posts_with_one_key_create_one_company(self):
        from founder_tools import services
        original = services.seed_new_company_founder_and_author

        def slow_seed(**kwargs):
            time.sleep(0.4)
            return original(**kwargs)

        barrier = threading.Barrier(2)
        results, errors = [], []

        def worker():
            close_old_connections()
            client = APIClient()
            client.force_authenticate(user=self.user)
            try:
                barrier.wait(timeout=5)
                response = self.post({"createNew": True, "firstProfile": True, "domain": "race.test"},
                                     key="same-press", client=client)
                results.append((response.status_code, response.data.get("id")))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        with patch("founder_tools.views.seed_new_company_founder_and_author", side_effect=slow_seed):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(code for code, _ in results), [200, 201], results)
        self.assertEqual(len({company_id for _, company_id in results}), 1)
        self.assertEqual(VibeRaisingCompany.objects.count(), 1)
