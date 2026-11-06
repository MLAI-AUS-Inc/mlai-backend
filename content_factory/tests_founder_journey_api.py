"""P1.1: the journey endpoint and the owner opt-in, over HTTP (IF-P1-03, IF-P1-05).

The journey replaces the dashboard's parallel reads with one poll, so it must
be cheap to repeat (ETag and 304), stay under the poll bucket for a whole
session, and leave every legacy startup on today's pages.
"""
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat import my_startup_journey_views as journey_views
from community_chat.throttles import StartupScopedThrottle

from . import founder_journey as journeys
from . import founder_journey_cohort as cohorts
from . import vibe_marketing_views as views
from .founder_journey_testing import (ALL_P1_FLAGS_ON, NAMED_LEGACY_ORGS, make_history_free_org,
                                      make_named_legacy_org, make_new_journey_org)
from .tests_founder_journey_unit import FREE

FLAGS = {**ALL_P1_FLAGS_ON, **FREE}
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "journey-api"},
          "watt_session": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "journey-watt"}}


class ApiFixture:
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(CACHES=LOCMEM))
        cache.clear()
        self.addCleanup(cache.clear)
        self.factory = APIRequestFactory()
        self.enterContext(patch.object(views, "get_content_factory_article_cost_points", return_value=12))
        self.enterContext(patch.object(views, "_roo_points_balance_for_user", return_value=0))
        self.enterContext(patch.object(views, "_topic_selection_candidate_pool", return_value=[]))
        self.allow = patch.object(StartupScopedThrottle, "allow_request", return_value=True)
        self.allow.start()
        self.addCleanup(patch.stopall)

    def call(self, view, user, company, *, method="get", data=None, headers=None):
        request = getattr(self.factory, method)(f"/?company_id={company.pk}", data=data or {}, format="json",
                                                **(headers or {}))
        force_authenticate(request, user=user, token=None)
        response = view.as_view()(request)
        if hasattr(response, "render"):
            response.render()
        return response

    def get_journey(self, built, **headers):
        return self.call(journey_views.FounderJourneyView, built.owner, built.company, headers=headers)


class JourneyEndpointTests(ApiFixture, TestCase):
    def test_flag_off_is_not_found(self):
        built = make_new_journey_org()
        response = self.get_journey(built)
        self.assertEqual((response.status_code, response.data), (404, {"detail": "Not found."}))

    @override_settings(**FLAGS)
    def test_the_five_named_legacy_orgs_short_circuit_with_every_flag_on(self):
        for name in NAMED_LEGACY_ORGS:
            for marker in (True, False):
                with self.subTest(name=name, marker=marker):
                    # The real domain for the marked variant; the unmarked one
                    # needs its own (domains are unique) and stays legacy by age.
                    domain = None if marker else f"{uuid4().hex[:6]}-{NAMED_LEGACY_ORGS[name]}"
                    built = make_named_legacy_org(name, marker=marker, domain=domain)
                    with patch("content_factory.first_article_entitlement.entitlement_state") as expensive:
                        response = self.get_journey(built)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.data, journeys.legacy_payload(built.company.pk))
                    self.assertEqual(response.data["stage"], "legacy")
                    expensive.assert_not_called()

    @override_settings(**FLAGS)
    def test_a_history_free_org_from_before_launch_is_legacy(self):
        built = make_history_free_org()
        self.assertEqual(self.get_journey(built).data["stage"], "legacy")

    @override_settings(**FLAGS)
    def test_private_cache_and_owned_company_only(self):
        built = make_new_journey_org()
        response = self.get_journey(built)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response.data["cohort"], "new_journey")
        stranger = make_new_journey_org()
        self.assertEqual(self.call(journey_views.FounderJourneyView, stranger.owner, built.company).status_code, 404)

    @override_settings(**FLAGS)
    def test_etag_is_stable_and_if_none_match_gives_an_empty_304(self):
        built = make_new_journey_org()
        first, second = self.get_journey(built), self.get_journey(built)
        etag = first.data["etag"]
        self.assertRegex(etag, r"^[0-9a-f]{32}$")
        self.assertEqual(first["ETag"], f'"{etag}"')
        self.assertEqual(second.data["etag"], etag)
        self.assertEqual(etag, journeys.etag_for(first.data))
        for header in (f'"{etag}"', f'W/"{etag}"'):
            with self.subTest(header=header):
                response = self.get_journey(built, HTTP_IF_NONE_MATCH=header)
                self.assertEqual((response.status_code, response.content, response["ETag"]), (304, b"", f'"{etag}"'))
        changed = self.get_journey(built, HTTP_IF_NONE_MATCH='"0000"')
        self.assertEqual(changed.status_code, 200)

    @override_settings(**FLAGS)
    def test_the_route_resolves_with_and_without_a_slash(self):
        from django.urls import resolve
        for path in ("/api/v1/my-startup/journey", "/api/v1/my-startup/journey/"):
            self.assertIs(resolve(path).func.view_class, journey_views.FounderJourneyView)

    @override_settings(**FLAGS)
    def test_a_ten_minute_session_never_hits_the_poll_bucket(self):
        self.allow.stop()
        built = make_new_journey_org()
        clock = [10_000.0]
        throttled, etag, elapsed, polls = 0, None, 0.0, 0
        with patch.object(StartupScopedThrottle, "timer", lambda _self: clock[0]):
            while elapsed < 600:
                headers = {"HTTP_IF_NONE_MATCH": f'"{etag}"'} if etag else {}
                response = self.get_journey(built, **headers)
                polls += 1
                if response.status_code == 429:
                    throttled += 1
                    wait_ms = 5000
                else:
                    self.assertIn(response.status_code, (200, 304))
                    if response.status_code == 200:
                        etag, wait_ms = response.data["etag"], response.data["pollAfterMs"]
                # Fastest cadence the client may use (working), whatever the state says.
                wait_ms = min(wait_ms, journeys.POLL_AFTER_MS["working"])
                clock[0] += wait_ms / 1000
                elapsed += wait_ms / 1000
            self.assertEqual(throttled, 0)
            self.assertGreaterEqual(polls, 120)
            # The bucket is real: a burst far faster than pollAfterMs is refused.
            burst = [self.get_journey(built).status_code for _ in range(130)]
        self.assertIn(429, burst)


class OptInTests(ApiFixture, TestCase):
    def opt_in(self, built, user=None):
        return self.call(journey_views.FounderJourneyOptInView, user or built.owner, built.company, method="post")

    def test_flag_off_is_not_found(self):
        built = make_history_free_org()
        self.assertEqual(self.opt_in(built).status_code, 404)

    @override_settings(**FLAGS)
    def test_owner_opts_in_once_then_409(self):
        built = make_history_free_org()
        response = self.opt_in(built)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["founderJourney"]["cohort"], "new_journey")
        self.assertTrue(cohorts.is_new_journey(built.organization))
        again = self.opt_in(built)
        self.assertEqual((again.status_code, again.data),
                         (409, {"detail": "This isn't available for your startup yet.", "reason": "not_available"}))

    @override_settings(**{**FLAGS, "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": False})
    def test_unavailable_with_v2_off(self):
        built = make_history_free_org()
        response = self.opt_in(built)
        self.assertEqual((response.status_code, response.data["reason"]), (409, "not_available"))
        self.assertFalse(cohorts.read_marker(built.organization))

    @override_settings(**FLAGS)
    def test_a_disabled_org_is_unavailable(self):
        built = make_history_free_org()
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_DISABLED_ORGANIZATIONS=built.organization.domain):
            self.assertEqual(self.opt_in(built).status_code, 409)

    @override_settings(**FLAGS)
    def test_a_non_owner_is_refused(self):
        built = make_history_free_org()
        other = make_history_free_org()
        # MyStartupAccess hides other founders' startups (404); the view's own
        # owner rule answers 403 should that scoping ever widen.
        self.assertEqual(self.opt_in(built, user=other.owner).status_code, 404)
        with patch("community_chat.my_startup_views.user_may_use_organization", return_value=True), \
                patch("founder_tools.services.organization_owner_user_id", return_value=other.owner.pk + 1000):
            response = self.opt_in(built)
        self.assertEqual((response.status_code, response.data),
                         (403, {"detail": "Only the startup owner can do this.", "reason": "owner_only"}))
        self.assertFalse(cohorts.is_new_journey(built.organization))
