"""P1.12 (IF-P1-02, G1): ``founderJourney`` in the company bootstrap and the founder profile.

The key is additive and present only while V2 is on. For each of the five
named legacy organisations, the bootstrap with every P1 flag on equals the
flags-off bootstrap apart from that key, whose cohort is legacy.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from community_chat import my_startup_views
from content_factory.founder_journey_testing import (
    ALL_P1_FLAGS_ON, LEGACY_CREATED_AT, P1_FLAG_NAMES, make_named_legacy_org, make_new_journey_org,
)
from founder_tools import views as founder_views
from founder_tools.models import VibeRaisingCompany

BOOTSTRAP_URL = "/api/v1/vibe-marketing/bootstrap/"
PROFILE_URL = "/api/v1/founder-tools/profile/"
FLAGS_OFF = {name: False for name in P1_FLAG_NAMES}
WITHOUT_V2 = {**ALL_P1_FLAGS_ON, "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": False}
NAMED = ("talathrive", "vyavos", "pawshare", "la_vie", "mlai")
# The owner of a legacy org is offered the opt-in once MY_STARTUP_FOUNDER_JOURNEY_OPT_IN_ENABLED is on.
LEGACY_JOURNEY = {"version": 1, "cohort": "legacy", "optInAvailable": True, "staffView": False,
                  "copyReadyBeforeVerification": False}
# Wall-clock fields that move between two reads of the same state.
_VOLATILE = {"generatedAt", "serverTime", "checkedAt", "fetchedAt", "elapsedMs", "durationMs", "now"}


def _unsigned(url):
    """A Google connect URL without its per-request signed connect ticket."""
    from urllib.parse import parse_qsl, urlencode, urlsplit
    parts = urlsplit(str(url or ""))
    query = urlencode([(key, value) for key, value in parse_qsl(parts.query) if key not in {"state", "ticket"}])
    return parts._replace(query=query).geturl()


def _differences(left, right, path=""):
    """Paths where two payloads differ, for a readable failure."""
    if isinstance(left, dict) and isinstance(right, dict):
        return [item for key in sorted(set(left) | set(right))
                for item in _differences(left.get(key), right.get(key), f"{path}.{key}")]
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        return [item for index, pair in enumerate(zip(left, right))
                for item in _differences(*pair, f"{path}[{index}]")]
    return [] if left == right else [path]


def _stable(value):
    if isinstance(value, dict):
        return {key: (_unsigned(item) if key == "connectUrl" else _stable(item))
                for key, item in value.items() if key not in _VOLATILE}
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


class _Client:
    def setUp(self):
        patcher = patch("founder_tools.services.reconcile_user_slack_id_from_email")
        patcher.start()
        self.addCleanup(patcher.stop)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def bootstrap(self, built, view="summary", settings=FLAGS_OFF):
        cache.clear()
        with override_settings(**settings):
            response = self.client_for(built.owner).get(
                BOOTSTRAP_URL, {"view": view, "company_id": str(built.company.pk)})
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return response.data


class FounderJourneyBootstrapTests(_Client, TestCase):
    def test_present_with_v2_on_in_both_views_and_absent_with_v2_off(self):
        built = make_new_journey_org("banksia.studio")
        for view in ("summary", "full"):
            with self.subTest(view):
                on = self.bootstrap(built, view, ALL_P1_FLAGS_ON)
                self.assertEqual(on["founderJourney"], {
                    "version": 1, "cohort": "new_journey", "optInAvailable": False, "staffView": False,
                    "copyReadyBeforeVerification": True})
                self.assertNotIn("founderJourney", self.bootstrap(built, view, WITHOUT_V2))
                self.assertNotIn("founderJourney", self.bootstrap(built, view, FLAGS_OFF))

    def test_a_cached_bootstrap_follows_the_kill_switch_on_the_next_read(self):
        built = make_new_journey_org("cached.studio")
        client = self.client_for(built.owner)
        cache.clear()
        with override_settings(**ALL_P1_FLAGS_ON, VIBE_MARKETING_BOOTSTRAP_CACHE_SECONDS=60):
            first = client.get(BOOTSTRAP_URL, {"company_id": str(built.company.pk)}).data
        with override_settings(**WITHOUT_V2, VIBE_MARKETING_BOOTSTRAP_CACHE_SECONDS=60):
            second = client.get(BOOTSTRAP_URL, {"company_id": str(built.company.pk)}).data
        self.assertIn("founderJourney", first)
        self.assertNotIn("founderJourney", second)

    def test_the_no_domain_bootstrap_carries_it_too(self):
        user = get_user_model().objects.create_user(email="nodomain@example.test", first_name="Ada")
        client = self.client_for(user)
        with override_settings(**ALL_P1_FLAGS_ON):
            created = client.post("/api/v1/founder-tools/companies/", {"name": "Ledger Mates", "createNew": True},
                                  format="json")
            self.assertEqual(created.status_code, 200, created.data)
            on = client.get(BOOTSTRAP_URL, {"company_id": created.data["id"]}).data
        with override_settings(**WITHOUT_V2):
            off = client.get(BOOTSTRAP_URL, {"company_id": created.data["id"]}).data
        self.assertEqual(on["founderJourney"]["cohort"], "new_journey")
        self.assertNotIn("founderJourney", off)
        self.assertEqual({key: value for key, value in on.items() if key != "founderJourney"}, off)

    def test_the_no_domain_bootstrap_reads_no_viewer_with_v2_off_and_the_request_viewer_with_it_on(self):
        from content_factory import vibe_marketing_views as views
        user = get_user_model().objects.create_user(email="viewer@example.test", first_name="Ada")
        client = self.client_for(user)
        with override_settings(**ALL_P1_FLAGS_ON):
            created = client.post("/api/v1/founder-tools/companies/", {"name": "Ledger Mates", "createNew": True},
                                  format="json")
        company = VibeRaisingCompany.objects.select_related("organization").get(pk=created.data["id"])
        staff = get_user_model().objects.create_user(email="staff@example.test", is_staff=True)
        with override_settings(**WITHOUT_V2), patch("content_factory.founder_journey_cohort.public_payload") as payload:
            views._serialize_bootstrap_without_domain(company)
        payload.assert_not_called()
        with override_settings(**ALL_P1_FLAGS_ON):
            as_staff = views._serialize_bootstrap_without_domain(company, user=staff)
            as_owner = views._serialize_bootstrap_without_domain(company)
        self.assertIs(as_staff["founderJourney"]["staffView"], True)
        self.assertIs(as_owner["founderJourney"]["staffView"], False)

    def assert_named_org_unchanged(self, name, marker):
        built = make_named_legacy_org(name, marker=marker)
        for view in ("summary", "full"):
            with self.subTest(view=view):
                before = _stable(self.bootstrap(built, view, FLAGS_OFF))
                after = _stable(self.bootstrap(built, view, ALL_P1_FLAGS_ON))
                without_v2 = _stable(self.bootstrap(built, view, WITHOUT_V2))
                self.assertNotIn("founderJourney", before)
                self.assertEqual(after.pop("founderJourney"), LEGACY_JOURNEY)
                self.assertEqual(_differences(after, before), [])
                self.assertEqual(_differences(without_v2, before), [])


def _named_org_test(name, marker):
    def test(self):
        self.assert_named_org_unchanged(name, marker)
    test.__doc__ = f"G1: {name} ({'report_apply marker' if marker else 'no marker'}) keeps its bootstrap."
    return test


for _name in NAMED:
    for _marker in (True, False):
        setattr(FounderJourneyBootstrapTests,
                f"test_named_legacy_bootstrap_unchanged_{_name}_{'marker' if _marker else 'no_marker'}",
                _named_org_test(_name, _marker))


class FounderJourneyProfileTests(_Client, TestCase):
    def test_profile_lists_every_new_journey_company_and_no_legacy_one(self):
        from organizations.models import Organization

        built = make_new_journey_org("first.studio")
        second = make_new_journey_org("second.studio", marker=False)  # launch window only
        VibeRaisingCompany.objects.filter(pk=second.company.pk).update(profile=built.company.profile)
        legacy_org = Organization.objects.create(domain="older.studio", name="Older")
        Organization.objects.filter(pk=legacy_org.pk).update(created_at=LEGACY_CREATED_AT)
        legacy = VibeRaisingCompany.objects.create(profile=built.company.profile, organization=legacy_org,
                                                   name="Older", domain="older.studio")
        client = self.client_for(built.owner)
        with override_settings(**ALL_P1_FLAGS_ON):
            on = client.get(PROFILE_URL).data
        with override_settings(**WITHOUT_V2):
            off = client.get(PROFILE_URL).data
        self.assertEqual(on["founderJourney"]["version"], 1)
        self.assertEqual(on["founderJourney"]["newStartupCohort"], "new_journey")
        self.assertEqual(on["founderJourney"]["companies"], {
            str(built.company.pk): "new_journey", str(second.company.pk): "new_journey"})
        self.assertNotIn(str(legacy.pk), on["founderJourney"]["companies"])
        self.assertNotIn("founderJourney", off)
        self.assertEqual({key: value for key, value in on.items() if key != "founderJourney"}, off)

    def test_named_legacy_owner_profile_lists_no_company(self):
        built = make_named_legacy_org("talathrive")
        with override_settings(**ALL_P1_FLAGS_ON):
            data = self.client_for(built.owner).get(PROFILE_URL).data
        self.assertEqual(data["founderJourney"]["companies"], {})

    def test_the_facade_inherits_the_profile_and_bootstrap_reads(self):
        self.assertTrue(issubclass(my_startup_views.ProfileView, founder_views.FounderToolsProfileView))
        self.assertIs(my_startup_views.ProfileView.get, founder_views.FounderToolsProfileView.get)
        self.assertIs(my_startup_views.BootstrapView.get,
                      my_startup_views.marketing.VibeMarketingBootstrapView.get)
