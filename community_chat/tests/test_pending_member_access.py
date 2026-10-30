"""P1.3 decision 9: a member awaiting review keeps My startup, never billing or purchases."""

import re
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import URLPattern, URLResolver, get_resolver, resolve, reverse
from django.utils import timezone
from rest_framework.test import APIClient

from community_chat.models import CommunityChatDevice, CommunityMemberProfile
from community_chat.tests.test_account_profiles import credentials_for
from content_factory.company_billing import CompanyBillingView
from content_factory.founder_journey_testing import ALL_P1_FLAGS_ON, make_named_legacy_org
from founder_tools.my_startup.api import MyStartupViewMixin
from founder_tools.my_startup.purchases import (
    StartupPurchaseCheckoutView, StartupPurchaseCreateView, StartupPurchaseView,
)
from roo.views import CurrentUserPurchaseView

PREFIX = "/api/v1/my-startup/"
ORIGIN = "https://chat.mlai.au"
ON = dict(MY_STARTUP_PENDING_MEMBER_ACCESS_ENABLED=True)
REFUSAL = {"error": "onboarding_required", "detail": "Complete your community application before joining MLAI Chat."}
GUARDED = (CompanyBillingView, CurrentUserPurchaseView, StartupPurchaseCreateView, StartupPurchaseView,
           StartupPurchaseCheckoutView)
CONVERTER_VALUES = {"uuid": str(uuid.uuid4()), "int": "1", "str": "x", "slug": "x", "path": "x"}


def _walk(patterns, prefix=""):
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            yield from _walk(pattern.url_patterns, prefix + str(pattern.pattern))
        elif isinstance(pattern, URLPattern):
            yield prefix + str(pattern.pattern), pattern.callback


def _concrete(route):
    return "/" + re.sub(r"<(?:(\w+):)?\w+>", lambda match: CONVERTER_VALUES[match.group(1) or "str"], route)


def guarded_my_startup_paths():
    """Every reachable path whose resolved view is a billing or purchase view of My startup."""
    paths = {}
    for route, callback in _walk(get_resolver().url_patterns):
        if not route.startswith(PREFIX.lstrip("/")):
            continue
        view_class = getattr(callback, "view_class", None)
        if view_class is None or not issubclass(view_class, GUARDED):
            continue
        path = _concrete(route)
        resolved = getattr(resolve(path).func, "view_class", None)
        if resolved is not None and issubclass(resolved, GUARDED) and issubclass(resolved, MyStartupViewMixin):
            paths[path] = resolved
    return paths


@override_settings(
    COMMUNITY_CHAT_SIGNUP_ENABLED=True,
    COMMUNITY_CHAT_ALLOWED_ORIGINS=[ORIGIN, "mlaichat://callback"],
)
class PendingMemberAccessTests(TestCase):
    def setUp(self):
        cache.clear()
        for target, options in (
            ("founder_tools.services.reconcile_user_slack_id_from_email", {}),
            ("requests.sessions.Session.request", {"side_effect": AssertionError("Unexpected external HTTP")}),
        ):
            mock = patch(target, **options)
            mock.start()
            self.addCleanup(mock.stop)

    def member(self, status, email=None):
        user = get_user_model().objects.create_user(email=email or f"{status}-{uuid.uuid4().hex[:6]}@example.com",
                                                    email_verified_at=timezone.now())
        CommunityMemberProfile.objects.create(user=user, status=status, first_name="Priya", last_name="",
                                              adult_confirmed_at=timezone.now(), policy_version="v")
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {credentials_for(user).access_token}")
        return user, client

    def read(self, client):
        return client.get(PREFIX + "founder-tools/companies/")

    def call_guarded(self, client, path):
        method = "post" if path.endswith(("purchases/", "checkout/")) or "checkout" in path else "get"
        return getattr(client, method)(path, {}, format="json")

    def test_routes_cover_the_billing_facade_and_all_three_purchase_views(self):
        resolved = set(guarded_my_startup_paths().values())
        for view in (CompanyBillingView, StartupPurchaseCreateView, StartupPurchaseView, StartupPurchaseCheckoutView):
            self.assertTrue(any(issubclass(item, view) for item in resolved), view.__name__)
        facade = resolve(PREFIX + "vibe-marketing/company-billing/").func.view_class
        self.assertTrue(issubclass(facade, CompanyBillingView))

    @override_settings(**ON)
    def test_pending_member_reads_my_startup_but_never_billing_or_purchases(self):
        _, client = self.member(CommunityMemberProfile.Status.PENDING)
        self.assertEqual(self.read(client).status_code, 200)
        paths = guarded_my_startup_paths()
        self.assertGreaterEqual(len(paths), 4)
        for path in paths:
            with self.subTest(path=path):
                response = self.call_guarded(client, path)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.data, REFUSAL)

    @override_settings(**ON)
    def test_pending_member_still_cannot_enrol_in_the_relay(self):
        _, client = self.member(CommunityMemberProfile.Status.PENDING)
        self.assertEqual(client.get(reverse("community_chat_session")).status_code, 403)

    @override_settings(**ON)
    def test_approved_members_still_reach_billing_and_purchases(self):
        _, client = self.member(CommunityMemberProfile.Status.APPROVED)
        self.assertEqual(self.read(client).status_code, 200)
        for path in guarded_my_startup_paths():
            with self.subTest(path=path):
                response = self.call_guarded(client, path)
                self.assertNotEqual(response.status_code, 403, response.data)

    @override_settings(**ON)
    def test_incomplete_rejected_and_suspended_members_are_refused(self):
        for status in (CommunityMemberProfile.Status.INCOMPLETE, CommunityMemberProfile.Status.REJECTED,
                       CommunityMemberProfile.Status.SUSPENDED):
            with self.subTest(status=status):
                _, client = self.member(status)
                response = self.read(client)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.data, REFUSAL)

    def test_flag_off_pending_member_is_refused_as_today(self):
        _, client = self.member(CommunityMemberProfile.Status.PENDING)
        response = self.read(client)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data, REFUSAL)
        for path in guarded_my_startup_paths():
            with self.subTest(path=path):
                self.assertEqual(self.call_guarded(client, path).status_code, 403)

    def test_legacy_founder_is_unchanged_with_every_flag_on(self):
        legacy = make_named_legacy_org("vyavos")
        CommunityChatDevice.objects.create(user=legacy.owner, public_key="d" * 64, status="verified",
                                           verified_at=timezone.now())
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {credentials_for(legacy.owner).access_token}")
        responses = {}
        for flags in ({}, ALL_P1_FLAGS_ON):
            with override_settings(**flags):
                read = self.read(client)
                billing = client.get(PREFIX + "vibe-marketing/company-billing/",
                                     {"company_id": str(legacy.company.pk)})
                responses[bool(flags)] = (read.status_code, read.data, billing.status_code)
        self.assertEqual(responses[False], responses[True])
        self.assertEqual(responses[True][0], 200)
        self.assertNotEqual(responses[True][2], 403)
